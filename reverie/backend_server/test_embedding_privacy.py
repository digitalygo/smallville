"""
Isolated unit tests for per-persona embedding routing and privacy gating.

These tests never touch the network: the OpenAI client factory is mocked
and every assertion runs against in-memory fakes. They prove:

  1. ``get_embedding`` resolves provider/model from the persona context
     (scratch fields > per-persona env map > global defaults).
  2. A persona's provider override is never bypassed by the global default.
  3. Unknown providers fail closed (ValueError) before any API call.
  4. Every active embedding caller in the cognitive modules threads
     ``persona=persona`` (source-level AST check).
  5. Sensitive normal-run prints (memories, plans, insights, schedules,
     prompts, LLM outputs) are gated behind ``debug``/``verbose``
     (source-level AST check).

Run from the repo root:

    uv run python -m unittest discover -s reverie/backend_server -p "test_*.py" -v
"""
from __future__ import annotations

import ast
import os
import sys
import unittest
from unittest import mock

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from persona.prompt_template import gpt_structure  # noqa: E402


class _FakeScratch:
    """Minimal scratch stand-in exposing only the LLM config fields."""

    def __init__(self, name, provider=None, model=None):
        self.name = name
        self.llm_provider = provider
        self.llm_model = model


class _FakePersona:
    """Minimal persona stand-in: only ``scratch`` is used by model resolution."""

    def __init__(self, name="Alice", provider=None, model=None):
        self.scratch = _FakeScratch(name, provider, model)


class _FakeEmbeddingData:
    def __init__(self, embedding):
        self.embedding = embedding


class _FakeEmbeddingResponse:
    def __init__(self, embedding):
        self.data = [_FakeEmbeddingData(embedding)]


class _FakeEmbeddings:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeEmbeddingResponse([0.1, 0.2, 0.3])


class _FakeClient:
    def __init__(self):
        self.embeddings = _FakeEmbeddings()


class EmbeddingRoutingTests(unittest.TestCase):
    """Per-persona provider/model routing for embeddings."""

    def setUp(self):
        self._clients = {}
        self._providers_requested = []

        def fake_get_client(provider):
            self._providers_requested.append(provider)
            if provider not in self._clients:
                self._clients[provider] = _FakeClient()
            return self._clients[provider]

        self._client_patcher = mock.patch.object(
            gpt_structure, "_get_client", side_effect=fake_get_client
        )
        self._client_patcher.start()
        self._env_patcher = mock.patch.multiple(
            gpt_structure,
            llm_provider="openai",
            llm_model=None,
            llm_persona_models={},
            llm_embedding_model=None,
            llm_embedding_models={},
        )
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()
        self._client_patcher.stop()

    def _last_embedding_call(self):
        """Return the (provider, model, text) of the most recent embedding."""
        provider = self._providers_requested[-1]
        client = self._clients[provider]
        call = client.embeddings.calls[-1]
        return provider, call["model"], call["input"][0]

    def test_persona_scratch_provider_routes_embedding(self):
        """A persona pinned to openrouter must embed via openrouter."""
        persona = _FakePersona(name="Alice", provider="openrouter")
        gpt_structure.get_embedding("Alice is painting", persona=persona)

        provider, model, text = self._last_embedding_call()
        self.assertEqual(provider, "openrouter")
        self.assertEqual(model, "openai/text-embedding-ada-002")
        self.assertEqual(text, "Alice is painting")

    def test_no_global_provider_bypass(self):
        """Global openai default must not override a persona's openrouter pin."""
        persona = _FakePersona(name="Bob", provider="openrouter")
        gpt_structure.get_embedding("Bob is reading", persona=persona)

        self.assertEqual(self._providers_requested, ["openrouter"])
        self.assertNotIn("openai", self._providers_requested)

    def test_global_default_used_when_persona_unset(self):
        """Without persona config, the global provider is used (documented)."""
        gpt_structure.get_embedding("plain text")

        provider, model, _ = self._last_embedding_call()
        self.assertEqual(provider, "openai")
        self.assertEqual(model, "text-embedding-ada-002")

    def test_scratch_provider_beats_env_map(self):
        """Scratch provider has highest precedence over the env map."""
        gpt_structure.llm_persona_models = {"Alice": "openrouter:anthropic/claude-3.5-sonnet"}
        persona = _FakePersona(name="Alice", provider="openai")
        gpt_structure.get_embedding("Alice is cooking", persona=persona)

        provider, model, _ = self._last_embedding_call()
        self.assertEqual(provider, "openai")
        self.assertEqual(model, "text-embedding-ada-002")

    def test_env_map_used_when_scratch_unset(self):
        """Per-persona env map applies when scratch leaves provider unset."""
        gpt_structure.llm_persona_models = {"Carol": "openrouter:anthropic/claude-3.5-sonnet"}
        persona = _FakePersona(name="Carol", provider=None)
        gpt_structure.get_embedding("Carol is gardening", persona=persona)

        provider, model, _ = self._last_embedding_call()
        self.assertEqual(provider, "openrouter")
        self.assertEqual(model, "openai/text-embedding-ada-002")

    def test_explicit_model_keeps_persona_provider(self):
        """An explicit model is honored but provider still comes from persona."""
        persona = _FakePersona(name="Dave", provider="openrouter")
        gpt_structure.get_embedding(
            "Dave is running", model="custom/embed-model", persona=persona
        )

        provider, model, _ = self._last_embedding_call()
        self.assertEqual(provider, "openrouter")
        self.assertEqual(model, "custom/embed-model")

    def test_unknown_provider_fails_closed(self):
        """An invalid persona provider raises before any API call."""
        persona = _FakePersona(name="Eve", provider="anthropic")
        with self.assertRaises(ValueError):
            gpt_structure.get_embedding("Eve is thinking", persona=persona)
        self.assertEqual(self._providers_requested, [])

    def test_unknown_env_map_prefix_is_bare_model_not_provider(self):
        """An unknown-prefix env value is a bare model: provider stays global.

        Only ``openai:`` / ``openrouter:`` prefixes are parsed as provider
        splits; anything else is a model slug and must not change routing.
        """
        gpt_structure.llm_persona_models = {"Frank": "anthropic:claude-3.5-sonnet"}
        persona = _FakePersona(name="Frank", provider=None)
        gpt_structure.get_embedding("Frank is walking", persona=persona)

        provider, model, _ = self._last_embedding_call()
        self.assertEqual(provider, "openai")
        self.assertEqual(model, "text-embedding-ada-002")

    def test_openrouter_embedding_sends_encoding_format_float(self):
        """OpenRouter embedding requests must request float vectors."""
        persona = _FakePersona(name="Gina", provider="openrouter")
        gpt_structure.get_embedding("Gina is sketching", persona=persona)

        provider = self._providers_requested[-1]
        call = self._clients[provider].embeddings.calls[-1]
        self.assertEqual(provider, "openrouter")
        self.assertEqual(call["model"], "openai/text-embedding-ada-002")
        self.assertEqual(call["input"], ["Gina is sketching"])
        self.assertEqual(call["encoding_format"], "float")

    def test_openai_embedding_has_no_openrouter_only_fields(self):
        """OpenAI embedding requests keep the plain text contract."""
        gpt_structure.get_embedding("plain text")

        provider = self._providers_requested[-1]
        call = self._clients[provider].embeddings.calls[-1]
        self.assertEqual(provider, "openai")
        self.assertNotIn("encoding_format", call)


_COGNITIVE_MODULES = [
    "perceive.py",
    "retrieve.py",
    "plan.py",
    "converse.py",
    "reflect.py",
    "execute.py",
]

_MODULE_DIR = os.path.join(_BACKEND_DIR, "persona", "cognitive_modules")


def _module_sources():
    for name in _COGNITIVE_MODULES:
        path = os.path.join(_MODULE_DIR, name)
        with open(path, "r", encoding="utf-8") as f:
            yield name, path, f.read()


class EmbeddingCallerSourceTests(unittest.TestCase):
    """Every active embedding caller must thread ``persona=persona``."""

    def test_all_embedding_callers_pass_persona(self):
        offenders = []
        for name, path, source in _module_sources():
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Name) and func.id == "get_embedding"):
                    continue
                keywords = {kw.arg for kw in node.keywords}
                if "persona" not in keywords:
                    offenders.append(f"{name}:{node.lineno}")
        self.assertEqual(
            offenders, [],
            "get_embedding calls missing persona=persona: " + ", ".join(offenders),
        )

    def test_embedding_calls_exist_in_active_modules(self):
        """Sanity check: the scan actually sees embedding calls."""
        total = 0
        for name, path, source in _module_sources():
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if node.func.id == "get_embedding":
                        total += 1
        self.assertGreaterEqual(total, 9)


class SensitivePrintSourceTests(unittest.TestCase):
    """Sensitive normal-run prints must be gated behind debug/verbose."""

    _SENSITIVE_PRINTS = [
        ("retrieve.py", "embedding_key"),
        ("retrieve.py", "recency_w"),
        ("reflect.py", "importance_trigger_curr"),
        ("reflect.py", "importance_trigger_max"),
        ("reflect.py", "print (ret)"),
        ("execute.py", "aldhfoaf"),
    ]

    @staticmethod
    def _is_gated(tree, node):
        """True if ``node`` sits inside an ``if debug`` / ``if debug or verbose``."""
        for parent in ast.walk(tree):
            if not isinstance(parent, ast.If):
                continue
            test = parent.test
            is_debug = isinstance(test, ast.Name) and test.id == "debug"
            is_debug_or_verbose = (
                isinstance(test, ast.BoolOp)
                and isinstance(test.op, ast.Or)
                and any(
                    isinstance(v, ast.Name) and v.id in ("debug", "verbose")
                    for v in test.values
                )
            )
            if not (is_debug or is_debug_or_verbose):
                continue
            for container in (parent.body, parent.orelse):
                for stmt in container:
                    for child in ast.walk(stmt):
                        if child is node:
                            return True
        return False

    def test_sensitive_prints_are_gated(self):
        offenders = []
        for module, needle in self._SENSITIVE_PRINTS:
            path = os.path.join(_MODULE_DIR, module)
            with open(path, "r", encoding="utf-8") as f:
                source = f.read()
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Expr):
                    continue
                value = node.value
                if not isinstance(value, ast.Call):
                    continue
                if not (isinstance(value.func, ast.Name) and value.func.id == "print"):
                    continue
                printed = ast.get_source_segment(source, value)
                if printed and needle in printed and not self._is_gated(tree, node):
                    offenders.append(f"{module}:{node.lineno} ({needle!r})")
        self.assertEqual(
            offenders, [],
            "ungated sensitive prints: " + ", ".join(offenders),
        )

    def test_no_ungated_content_prints_in_retrieve(self):
        """new_retrieve must not print memory keys/scores on the normal path."""
        path = os.path.join(_MODULE_DIR, "retrieve.py")
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Expr):
                continue
            value = node.value
            if not (isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "print"):
                continue
            printed = ast.get_source_segment(source, value)
            if printed and "embedding_key" in printed:
                self.assertTrue(
                    self._is_gated(tree, node),
                    f"ungated print of embedding_key at line {node.lineno}",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
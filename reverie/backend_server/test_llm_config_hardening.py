"""
Isolated unit tests for LLM configuration hardening.

Covers:

  1. Malformed ``LLM_PERSONA_MODELS`` JSON fails fast with a clear
     configuration error instead of silently becoming an empty map.
  2. Explicit embedding-model configuration (global ``LLM_EMBEDDING_MODEL``
     and per-provider ``LLM_EMBEDDING_MODELS``) with safe defaults and no
     reuse of the chat model.
  3. OpenRouter attribution headers are attached to the OpenRouter client.
  4. Per-persona chat model/provider selection precedence.
  5. Provider fail-closed behavior: unknown providers raise before any API call.

These tests never touch the network: the OpenAI client factory is mocked
and every assertion runs against in-memory fakes.

Run from the repo root:

    uv run python -m unittest discover -s reverie/backend_server -p "test_*.py" -v
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest import mock

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import utils  # noqa: E402
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


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeCompletion:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeChatCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeCompletion("ok")


class _FakeChat:
    def __init__(self):
        self.completions = _FakeChatCompletions()


class _FakeEmbeddingData:
    def __init__(self, embedding):
        self.embedding = embedding


class _FakeEmbeddingResponse:
    def __init__(self, embedding):
        self.data = [_FakeEmbeddingData(embedding)]


class _FakeEmbeddings:
    def __init__(self):
        self.calls = []

    def create(self, input, model):
        self.calls.append({"input": input, "model": model})
        return _FakeEmbeddingResponse([0.1, 0.2, 0.3])


class _FakeClient:
    def __init__(self):
        self.chat = _FakeChat()
        self.embeddings = _FakeEmbeddings()


class PersonaModelsConfigTests(unittest.TestCase):
    """``_load_json_map`` fails fast with a clear error on malformed input."""

    def test_empty_value_yields_empty_map(self):
        self.assertEqual(utils._load_json_map("", "LLM_PERSONA_MODELS"), {})
        self.assertEqual(utils._load_json_map("   ", "LLM_PERSONA_MODELS"), {})

    def test_valid_map_parses(self):
        raw = (
            '{"Isabella Rodriguez": "openrouter:anthropic/claude-3.5-sonnet",'
            ' "Maria Lopez": "gpt-4o"}'
        )
        parsed = utils._load_json_map(raw, "LLM_PERSONA_MODELS")
        self.assertEqual(
            parsed["Isabella Rodriguez"], "openrouter:anthropic/claude-3.5-sonnet"
        )
        self.assertEqual(parsed["Maria Lopez"], "gpt-4o")

    def test_malformed_json_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            utils._load_json_map("{not valid json", "LLM_PERSONA_MODELS")
        message = str(ctx.exception)
        self.assertIn("LLM_PERSONA_MODELS", message)
        self.assertIn("Invalid JSON", message)

    def test_non_object_json_raises_value_error(self):
        with self.assertRaises(ValueError):
            utils._load_json_map("[1, 2, 3]", "LLM_PERSONA_MODELS")

    def test_non_string_values_raise_value_error(self):
        with self.assertRaises(ValueError):
            utils._load_json_map('{"Alice": 42}', "LLM_PERSONA_MODELS")

    def test_embedding_models_map_parses(self):
        raw = (
            '{"openai": "text-embedding-3-small",'
            ' "openrouter": "openai/text-embedding-3-small"}'
        )
        parsed = utils._load_json_map(raw, "LLM_EMBEDDING_MODELS")
        self.assertEqual(parsed["openai"], "text-embedding-3-small")
        self.assertEqual(parsed["openrouter"], "openai/text-embedding-3-small")

    def test_import_fails_fast_on_malformed_map(self):
        """A malformed LLM_PERSONA_MODELS must abort the import, not silently empty."""
        script = (
            "import os, sys; "
            "sys.path.insert(0, %r); "
            "import utils"
        ) % _BACKEND_DIR
        env = dict(os.environ)
        env["LLM_PERSONA_MODELS"] = "{not valid json"
        result = subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LLM_PERSONA_MODELS", result.stderr)
        self.assertIn("Invalid JSON", result.stderr)


class EmbeddingModelConfigTests(unittest.TestCase):
    """Explicit embedding-model configuration with safe defaults."""

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
        provider = self._providers_requested[-1]
        client = self._clients[provider]
        call = client.embeddings.calls[-1]
        return provider, call["model"], call["input"][0]

    def test_global_embedding_model_used_when_set(self):
        gpt_structure.llm_embedding_model = "text-embedding-3-small"
        gpt_structure.get_embedding("hello")

        provider, model, _ = self._last_embedding_call()
        self.assertEqual(provider, "openai")
        self.assertEqual(model, "text-embedding-3-small")

    def test_per_provider_embedding_model_used(self):
        gpt_structure.llm_embedding_models = {"openai": "text-embedding-3-large"}
        gpt_structure.get_embedding("hello")

        provider, model, _ = self._last_embedding_call()
        self.assertEqual(provider, "openai")
        self.assertEqual(model, "text-embedding-3-large")

    def test_per_provider_beats_global(self):
        gpt_structure.llm_embedding_model = "text-embedding-3-small"
        gpt_structure.llm_embedding_models = {"openai": "text-embedding-3-large"}
        gpt_structure.get_embedding("hello")

        provider, model, _ = self._last_embedding_call()
        self.assertEqual(model, "text-embedding-3-large")

    def test_explicit_model_beats_config(self):
        gpt_structure.llm_embedding_model = "text-embedding-3-small"
        gpt_structure.get_embedding("hello", model="custom/embed")

        provider, model, _ = self._last_embedding_call()
        self.assertEqual(model, "custom/embed")

    def test_chat_model_never_reused_as_embedding_model(self):
        """LLM_MODEL (chat) must never leak into embedding selection."""
        gpt_structure.llm_model = "gpt-4o"
        gpt_structure.llm_embedding_model = None
        gpt_structure.llm_embedding_models = {}
        gpt_structure.get_embedding("hello")

        provider, model, _ = self._last_embedding_call()
        self.assertEqual(model, "text-embedding-ada-002")
        self.assertNotEqual(model, "gpt-4o")

    def test_openrouter_default_embedding_model(self):
        gpt_structure.llm_provider = "openrouter"
        gpt_structure.get_embedding("hello")

        provider, model, _ = self._last_embedding_call()
        self.assertEqual(provider, "openrouter")
        self.assertEqual(model, "openai/text-embedding-ada-002")


class OpenRouterAttributionTests(unittest.TestCase):
    """OpenRouter clients must carry the configured attribution headers."""

    def setUp(self):
        self._clients_patcher = mock.patch.object(gpt_structure, "_clients", {})
        self._clients_patcher.start()
        self.addCleanup(self._clients_patcher.stop)

    def _capture_openai(self):
        captured = {}

        def fake_openai(**kwargs):
            captured["kwargs"] = kwargs
            return _FakeClient()

        patcher = mock.patch.object(
            gpt_structure, "OpenAI", side_effect=fake_openai
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return captured

    def test_openrouter_client_sends_attribution_headers(self):
        captured = self._capture_openai()
        with mock.patch.multiple(
            gpt_structure,
            openrouter_referer="https://example.com/app",
            openrouter_title="my-app",
            openrouter_categories="game,roleplay",
        ):
            gpt_structure._get_client("openrouter")

        kwargs = captured["kwargs"]
        self.assertEqual(kwargs["base_url"], "https://openrouter.ai/api/v1")
        headers = kwargs["default_headers"]
        self.assertEqual(headers["HTTP-Referer"], "https://example.com/app")
        self.assertEqual(headers["X-Title"], "my-app")
        self.assertEqual(headers["X-OpenRouter-Title"], "my-app")
        self.assertEqual(headers["X-OpenRouter-Categories"], "game,roleplay")

    def test_openai_client_has_no_openrouter_headers(self):
        captured = self._capture_openai()
        gpt_structure._get_client("openai")

        kwargs = captured["kwargs"]
        self.assertNotIn("base_url", kwargs)
        self.assertNotIn("default_headers", kwargs)


class ChatModelSelectionTests(unittest.TestCase):
    """Per-persona chat model/provider selection precedence."""

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
        )
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()
        self._client_patcher.stop()

    def test_global_defaults_when_nothing_set(self):
        provider, model = gpt_structure._resolve_model()
        self.assertEqual(provider, "openai")
        self.assertEqual(model, "gpt-4o")

    def test_scratch_provider_and_model_win(self):
        persona = _FakePersona(
            name="Alice", provider="openrouter", model="anthropic/claude-3.5-sonnet"
        )
        provider, model = gpt_structure._resolve_model(persona)
        self.assertEqual(provider, "openrouter")
        self.assertEqual(model, "anthropic/claude-3.5-sonnet")

    def test_env_map_sets_provider_and_model_when_scratch_unset(self):
        gpt_structure.llm_persona_models = {
            "Alice": "openrouter:anthropic/claude-3.5-sonnet"
        }
        persona = _FakePersona(name="Alice", provider=None, model=None)
        provider, model = gpt_structure._resolve_model(persona)
        self.assertEqual(provider, "openrouter")
        self.assertEqual(model, "anthropic/claude-3.5-sonnet")

    def test_env_map_bare_model_uses_global_provider(self):
        gpt_structure.llm_persona_models = {"Alice": "gpt-4o-mini"}
        persona = _FakePersona(name="Alice", provider=None, model=None)
        provider, model = gpt_structure._resolve_model(persona)
        self.assertEqual(provider, "openai")
        self.assertEqual(model, "gpt-4o-mini")

    def test_scratch_provider_beats_env_map_provider(self):
        gpt_structure.llm_persona_models = {
            "Alice": "openrouter:anthropic/claude-3.5-sonnet"
        }
        persona = _FakePersona(name="Alice", provider="openai", model=None)
        provider, model = gpt_structure._resolve_model(persona)
        self.assertEqual(provider, "openai")
        self.assertEqual(model, "anthropic/claude-3.5-sonnet")

    def test_scratch_model_beats_env_map_model(self):
        gpt_structure.llm_persona_models = {
            "Alice": "openrouter:anthropic/claude-3.5-sonnet"
        }
        persona = _FakePersona(name="Alice", provider=None, model="gpt-4o")
        provider, model = gpt_structure._resolve_model(persona)
        self.assertEqual(provider, "openrouter")
        self.assertEqual(model, "gpt-4o")

    def test_chat_request_uses_resolved_model(self):
        persona = _FakePersona(
            name="Alice", provider="openrouter", model="anthropic/claude-3.5-sonnet"
        )
        gpt_structure.ChatGPT_request("hello", persona=persona)

        provider = self._providers_requested[-1]
        call = self._clients[provider].chat.completions.calls[-1]
        self.assertEqual(provider, "openrouter")
        self.assertEqual(call["model"], "anthropic/claude-3.5-sonnet")


class ProviderFailClosedTests(unittest.TestCase):
    """Unknown providers must raise before any API call."""

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
        )
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()
        self._client_patcher.stop()

    def test_invalid_scratch_provider_raises_before_api_call(self):
        persona = _FakePersona(name="Eve", provider="anthropic")
        with self.assertRaises(ValueError):
            gpt_structure.ChatGPT_request("hello", persona=persona)
        self.assertEqual(self._providers_requested, [])

    def test_invalid_global_provider_raises(self):
        with mock.patch.object(gpt_structure, "llm_provider", "anthropic"):
            with self.assertRaises(ValueError):
                gpt_structure._resolve_model()
        self.assertEqual(self._providers_requested, [])

    def test_get_client_fails_closed_on_unknown_provider(self):
        """The real _get_client rejects unknown providers before building a client."""
        self._client_patcher.stop()
        try:
            with mock.patch.object(gpt_structure, "OpenAI") as fake_openai:
                with self.assertRaises(ValueError):
                    gpt_structure._get_client("anthropic")
            fake_openai.assert_not_called()
        finally:
            self._client_patcher.start()

    def test_unknown_env_map_prefix_is_bare_model_not_provider(self):
        """Unknown-prefix env values are bare models; provider stays global."""
        gpt_structure.llm_persona_models = {"Eve": "anthropic:claude-3.5-sonnet"}
        persona = _FakePersona(name="Eve", provider=None)
        provider, model = gpt_structure._resolve_model(persona)
        self.assertEqual(provider, "openai")
        self.assertEqual(model, "anthropic:claude-3.5-sonnet")


if __name__ == "__main__":
    unittest.main(verbosity=2)

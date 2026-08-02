"""
Isolated unit tests for LLM provider failure handling.

Covers:

  1. One bounded exponential retry helper for every chat and embedding
     wrapper: fail-fail-success retries, backoff schedule, and sanitized
     ``LLMProviderError`` after exhaustion.
  2. ``KeyboardInterrupt`` / ``SystemExit`` propagate immediately, never
     retried or wrapped.
  3. Provider failures never reach validators or cleanup: safe response
     wrappers re-raise ``LLMProviderError`` instead of validating sentinel
     strings, while parse/validator/cleanup errors stay model-output retries.
  4. OpenAI-compatible clients are built with an explicit timeout and
     ``max_retries=0`` so the single bounded retry policy owns retries.
  5. Retry configuration contract: defaults 3 / 1.0 / 60.0, integer >= 1,
     finite float >= 0, finite float > 0, and malformed values fail fast at
     import. Non-finite values (inf, -inf, nan) are rejected with a
     sanitized error naming only the variable and its range.

These tests never touch the network: the client factory and sleeps are
mocked and every assertion runs against in-memory fakes.

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

LLMProviderError = utils.LLMProviderError


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeCompletion:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FlakyChatCompletions:
    """Chat completions fake that fails ``failures`` times, then pops responses."""

    def __init__(self, failures=0, responses=None, exc=None):
        self.failures = failures
        self.responses = list(responses) if responses else None
        self.exc = exc if exc is not None else RuntimeError("connection refused")
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        if self.responses is not None:
            return _FakeCompletion(self.responses.pop(0))
        return _FakeCompletion("ok")


class _FakeChat:
    def __init__(self):
        self.completions = _FlakyChatCompletions()


class _FakeEmbeddingData:
    def __init__(self, embedding):
        self.embedding = embedding


class _FakeEmbeddingResponse:
    def __init__(self, embedding):
        self.data = [_FakeEmbeddingData(embedding)]


class _FlakyEmbeddings:
    """Embeddings fake that fails ``failures`` times before succeeding."""

    def __init__(self, failures=0, exc=None):
        self.failures = failures
        self.exc = exc if exc is not None else RuntimeError("network down")
        self.calls = 0

    def create(self, input, model):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        return _FakeEmbeddingResponse([0.1, 0.2, 0.3])


class _FakeClient:
    def __init__(self):
        self.chat = _FakeChat()
        self.embeddings = _FlakyEmbeddings()


class _RetryHarness(unittest.TestCase):
    """Patches the client factory, retry config, and sleeps per test."""

    def setUp(self):
        self._clients = {}

        def fake_get_client(provider):
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
            llm_api_retries=2,
            llm_api_retry_base_seconds=1.0,
            llm_api_timeout_seconds=60.0,
        )
        self._env_patcher.start()
        self._sleep_patcher = mock.patch.object(
            gpt_structure, "temp_sleep", mock.Mock()
        )
        self._sleep_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()
        self._env_patcher.stop()
        self._client_patcher.stop()

    def _client(self, provider="openai"):
        if provider not in self._clients:
            self._clients[provider] = _FakeClient()
        return self._clients[provider]

    def _set_chat(self, failures=0, responses=None, exc=None):
        flaky = _FlakyChatCompletions(failures, responses, exc)
        self._client().chat.completions = flaky
        return flaky

    def _set_embeddings(self, failures=0, exc=None):
        flaky = _FlakyEmbeddings(failures, exc)
        self._client().embeddings = flaky
        return flaky

    def _sleep_delays(self):
        return [call[0][0] for call in gpt_structure.temp_sleep.call_args_list]


class ChatRequestRetryTests(_RetryHarness):
    """Every chat wrapper retries fail-fail-success and raises sanitized errors."""

    def test_chat_request_fail_fail_success_retries_then_returns(self):
        flaky = self._set_chat(failures=2)
        out = gpt_structure.ChatGPT_request("hello")
        self.assertEqual(out, "ok")
        self.assertEqual(flaky.calls, 3)
        self.assertEqual(self._sleep_delays(), [1.0, 2.0])

    def test_chat_single_request_fail_fail_success_retries(self):
        flaky = self._set_chat(failures=2)
        out = gpt_structure.ChatGPT_single_request("hello")
        self.assertEqual(out, "ok")
        self.assertEqual(flaky.calls, 3)

    def test_gpt4_request_fail_fail_success_retries(self):
        flaky = self._set_chat(failures=2)
        out = gpt_structure.GPT4_request("hello")
        self.assertEqual(out, "ok")
        self.assertEqual(flaky.calls, 3)

    def test_gpt_request_fail_fail_success_retries(self):
        flaky = self._set_chat(failures=2)
        gpt_param = {
            "engine": "text-davinci-003",
            "max_tokens": 5,
            "temperature": 0,
            "top_p": 1,
            "frequency_penalty": 0,
            "presence_penalty": 0,
        }
        out = gpt_structure.GPT_request("hello", gpt_param)
        self.assertEqual(out, "ok")
        self.assertEqual(flaky.calls, 3)

    def test_exhaustion_raises_sanitized_llm_provider_error(self):
        flaky = self._set_chat(failures=10)
        with self.assertRaises(LLMProviderError) as ctx:
            gpt_structure.ChatGPT_request("hello")
        exc = ctx.exception
        self.assertEqual(exc.provider, "openai")
        self.assertEqual(exc.operation, "chat")
        self.assertEqual(exc.attempts, 3)
        self.assertEqual(flaky.calls, 3)
        message = str(exc)
        self.assertIn("openai", message)
        self.assertIn("chat", message)
        self.assertIn("3 attempts", message)
        self.assertNotIn("connection refused", message)

    def test_gpt_request_exhaustion_raises_sanitized_error(self):
        self._set_chat(failures=10)
        gpt_param = {"engine": "text-davinci-003", "max_tokens": 5}
        with self.assertRaises(LLMProviderError) as ctx:
            gpt_structure.GPT_request("hello", gpt_param)
        self.assertEqual(ctx.exception.operation, "chat")
        self.assertEqual(ctx.exception.attempts, 3)

    def test_error_message_never_contains_prompt_key_or_body(self):
        self._set_chat(
            failures=10,
            exc=RuntimeError("sk-super-secret-key in raw <response body>"),
        )
        prompt = "My secret prompt with plan details"
        with self.assertRaises(LLMProviderError) as ctx:
            gpt_structure.ChatGPT_request(prompt)
        message = str(ctx.exception)
        self.assertNotIn("sk-super-secret", message)
        self.assertNotIn("<response body>", message)
        self.assertNotIn("secret prompt", message)

    def test_keyboard_interrupt_propagates_without_retry(self):
        flaky = self._set_chat(failures=10, exc=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            gpt_structure.ChatGPT_request("hello")
        self.assertEqual(flaky.calls, 1)
        gpt_structure.temp_sleep.assert_not_called()

    def test_system_exit_propagates_without_retry(self):
        flaky = self._set_chat(failures=10, exc=SystemExit(3))
        with self.assertRaises(SystemExit) as ctx:
            gpt_structure.ChatGPT_request("hello")
        self.assertEqual(ctx.exception.code, 3)
        self.assertEqual(flaky.calls, 1)
        gpt_structure.temp_sleep.assert_not_called()


class EmbeddingRetryTests(_RetryHarness):
    """``get_embedding`` shares the same bounded retry policy."""

    def test_embedding_fail_fail_success_retries_then_returns(self):
        flaky = self._set_embeddings(failures=2)
        vec = gpt_structure.get_embedding("hello")
        self.assertEqual(vec, [0.1, 0.2, 0.3])
        self.assertEqual(flaky.calls, 3)
        self.assertEqual(self._sleep_delays(), [1.0, 2.0])

    def test_embedding_exhaustion_raises_sanitized_error(self):
        flaky = self._set_embeddings(failures=10)
        with self.assertRaises(LLMProviderError) as ctx:
            gpt_structure.get_embedding("hello")
        exc = ctx.exception
        self.assertEqual(exc.provider, "openai")
        self.assertEqual(exc.operation, "embedding")
        self.assertEqual(exc.attempts, 3)
        self.assertEqual(flaky.calls, 3)
        self.assertNotIn("network down", str(exc))


class SafeWrapperProviderFailureTests(_RetryHarness):
    """Provider failures re-raise and never reach validators or cleanup."""

    def _call_wrapper(self, wrapper, validate, cleanup):
        if wrapper is gpt_structure.ChatGPT_safe_generate_response_OLD:
            return wrapper("prompt", 3, "error", validate, cleanup)
        return wrapper(
            "prompt", "example", "special", 3, "error", validate, cleanup
        )

    def test_safe_wrappers_reraise_and_never_validate_sentinel(self):
        wrappers = (
            gpt_structure.ChatGPT_safe_generate_response,
            gpt_structure.GPT4_safe_generate_response,
            gpt_structure.ChatGPT_safe_generate_response_OLD,
        )
        for wrapper in wrappers:
            with self.subTest(wrapper=wrapper.__name__):
                self._set_chat(failures=10)
                validated = []

                def validate(resp, prompt=""):
                    validated.append(resp)
                    return True

                def cleanup(resp, prompt=""):
                    return resp

                with self.assertRaises(LLMProviderError):
                    self._call_wrapper(wrapper, validate, cleanup)
                self.assertEqual(
                    validated, [],
                    f"{wrapper.__name__} must not validate provider output",
                )

    def test_parse_errors_are_wrapper_model_output_retries(self):
        self._set_chat(responses=["not json", '{"output": "valid"}'])
        validated = []

        def validate(resp, prompt=""):
            validated.append(resp)
            return True

        def cleanup(resp, prompt=""):
            return resp.strip()

        out = gpt_structure.ChatGPT_safe_generate_response(
            "p", "ex", "sp", 3, "error", validate, cleanup
        )
        self.assertEqual(out, "valid")
        self.assertEqual(self._client().chat.completions.calls, 2)
        self.assertEqual(validated, ["valid"])
        gpt_structure.temp_sleep.assert_not_called()

    def test_validator_rejection_retries_with_new_provider_call(self):
        self._set_chat(responses=['{"output": "bad"}', '{"output": "good"}'])
        validated = []

        def validate(resp, prompt=""):
            validated.append(resp)
            return resp == "good"

        out = gpt_structure.ChatGPT_safe_generate_response(
            "p", "ex", "sp", 3, "error", validate, lambda r, prompt="": r
        )
        self.assertEqual(out, "good")
        self.assertEqual(self._client().chat.completions.calls, 2)
        self.assertEqual(validated, ["bad", "good"])

    def test_cleanup_error_retries_separately_from_provider_retries(self):
        self._set_chat(responses=['{"output": "x"}', '{"output": "x"}'])
        cleanup_calls = []

        def cleanup(resp, prompt=""):
            cleanup_calls.append(resp)
            if len(cleanup_calls) == 1:
                raise ValueError("cleanup exploded")
            return "cleaned"

        out = gpt_structure.ChatGPT_safe_generate_response(
            "p", "ex", "sp", 3, "error", lambda r, prompt="": True, cleanup
        )
        self.assertEqual(out, "cleaned")
        self.assertEqual(self._client().chat.completions.calls, 2)
        self.assertEqual(len(cleanup_calls), 2)

    def test_safe_wrapper_success_path_unchanged(self):
        self._set_chat(responses=['{"output": "ok"}'])
        out = gpt_structure.ChatGPT_safe_generate_response(
            "p", "ex", "sp", 3, "error", lambda r, prompt="": True,
            lambda r, prompt="": r,
        )
        self.assertEqual(out, "ok")


class ClientRetryConfigTests(unittest.TestCase):
    """Clients carry an explicit timeout and zero SDK retries."""

    def test_clients_use_explicit_timeout_and_zero_sdk_retries(self):
        captured = {}

        def fake_openai(**kwargs):
            captured["kwargs"] = kwargs
            return mock.Mock()

        with mock.patch.object(gpt_structure, "_clients", {}):
            with mock.patch.object(
                gpt_structure, "OpenAI", side_effect=fake_openai
            ):
                gpt_structure._get_client("openai")
        self.assertEqual(
            captured["kwargs"]["timeout"], gpt_structure.llm_api_timeout_seconds
        )
        self.assertEqual(captured["kwargs"]["max_retries"], 0)

        captured.clear()
        with mock.patch.object(gpt_structure, "_clients", {}):
            with mock.patch.object(
                gpt_structure, "OpenAI", side_effect=fake_openai
            ):
                gpt_structure._get_client("openrouter")
        self.assertEqual(
            captured["kwargs"]["timeout"], gpt_structure.llm_api_timeout_seconds
        )
        self.assertEqual(captured["kwargs"]["max_retries"], 0)


class SharedErrorTests(unittest.TestCase):
    """One ``LLMProviderError`` type shared across modules without cycles."""

    def test_llm_provider_error_is_single_shared_type(self):
        import reverie as reverie_mod

        self.assertIs(utils.LLMProviderError, gpt_structure.LLMProviderError)
        self.assertIs(reverie_mod.LLMProviderError, utils.LLMProviderError)
        err = utils.LLMProviderError("openai", "chat", 4)
        self.assertEqual(err.provider, "openai")
        self.assertEqual(err.operation, "chat")
        self.assertEqual(err.attempts, 4)
        message = str(err)
        self.assertIn("openai", message)
        self.assertIn("chat", message)
        self.assertIn("4 attempts", message)

    def test_llm_provider_error_is_exception(self):
        self.assertTrue(issubclass(utils.LLMProviderError, Exception))


class RetryConfigParsingTests(unittest.TestCase):
    """Retry config parsers enforce the contract and fail fast."""

    def test_defaults_when_unset(self):
        self.assertEqual(utils._parse_llm_api_retries(""), 3)
        self.assertEqual(utils._parse_llm_api_retries("   "), 3)
        self.assertEqual(utils._parse_llm_api_retry_base_seconds(""), 1.0)
        self.assertEqual(utils._parse_llm_api_timeout_seconds(""), 60.0)

    def test_valid_values_parse(self):
        self.assertEqual(utils._parse_llm_api_retries("5"), 5)
        self.assertEqual(utils._parse_llm_api_retry_base_seconds("0.5"), 0.5)
        self.assertEqual(utils._parse_llm_api_retry_base_seconds("0"), 0.0)
        self.assertEqual(utils._parse_llm_api_timeout_seconds("30"), 30.0)

    def test_invalid_retries_fail_fast(self):
        for raw in ("abc", "0", "-1", "1.5"):
            with self.assertRaises(ValueError) as ctx:
                utils._parse_llm_api_retries(raw)
            self.assertIn("LLM_API_RETRIES", str(ctx.exception))

    def test_invalid_base_seconds_fail_fast(self):
        for raw in ("abc", "-1", "-0.5"):
            with self.assertRaises(ValueError) as ctx:
                utils._parse_llm_api_retry_base_seconds(raw)
            self.assertIn("LLM_API_RETRY_BASE_SECONDS", str(ctx.exception))

    def test_invalid_timeout_fail_fast(self):
        for raw in ("abc", "0", "-5"):
            with self.assertRaises(ValueError) as ctx:
                utils._parse_llm_api_timeout_seconds(raw)
            self.assertIn("LLM_API_TIMEOUT_SECONDS", str(ctx.exception))

    def test_non_finite_base_seconds_fail_fast(self):
        for raw in ("inf", "-inf", "nan"):
            with self.assertRaises(ValueError) as ctx:
                utils._parse_llm_api_retry_base_seconds(raw)
            message = str(ctx.exception)
            self.assertIn("LLM_API_RETRY_BASE_SECONDS", message)
            self.assertIn("finite", message)
            self.assertIn(">= 0", message)
            self.assertNotIn(f"{raw!r}", message)

    def test_non_finite_timeout_fail_fast(self):
        for raw in ("inf", "-inf", "nan"):
            with self.assertRaises(ValueError) as ctx:
                utils._parse_llm_api_timeout_seconds(raw)
            message = str(ctx.exception)
            self.assertIn("LLM_API_TIMEOUT_SECONDS", message)
            self.assertIn("finite", message)
            self.assertIn("> 0", message)
            self.assertNotIn(f"{raw!r}", message)

    def test_malformed_retries_never_echo_raw_value(self):
        raw = "sk-invalid-retries-9f2a"
        with self.assertRaises(ValueError) as ctx:
            utils._parse_llm_api_retries(raw)
        message = str(ctx.exception)
        self.assertIn("LLM_API_RETRIES", message)
        self.assertIn("at least 1", message)
        self.assertNotIn(raw, message)
        self.assertNotIn(f"{raw!r}", message)

    def test_below_range_retries_never_echo_raw_value(self):
        raw = "-91337"
        with self.assertRaises(ValueError) as ctx:
            utils._parse_llm_api_retries(raw)
        message = str(ctx.exception)
        self.assertIn("LLM_API_RETRIES", message)
        self.assertIn("at least 1", message)
        self.assertNotIn(raw, message)
        self.assertNotIn(f"{raw!r}", message)

    def test_malformed_base_seconds_never_echo_raw_value(self):
        raw = "sk-invalid-base-7c1e"
        with self.assertRaises(ValueError) as ctx:
            utils._parse_llm_api_retry_base_seconds(raw)
        message = str(ctx.exception)
        self.assertIn("LLM_API_RETRY_BASE_SECONDS", message)
        self.assertIn("at least 0", message)
        self.assertNotIn(raw, message)
        self.assertNotIn(f"{raw!r}", message)

    def test_below_range_base_seconds_never_echo_raw_value(self):
        raw = "-0.91337"
        with self.assertRaises(ValueError) as ctx:
            utils._parse_llm_api_retry_base_seconds(raw)
        message = str(ctx.exception)
        self.assertIn("LLM_API_RETRY_BASE_SECONDS", message)
        self.assertIn(">= 0", message)
        self.assertNotIn(raw, message)
        self.assertNotIn(f"{raw!r}", message)

    def test_malformed_timeout_never_echo_raw_value(self):
        raw = "sk-invalid-timeout-5b3d"
        with self.assertRaises(ValueError) as ctx:
            utils._parse_llm_api_timeout_seconds(raw)
        message = str(ctx.exception)
        self.assertIn("LLM_API_TIMEOUT_SECONDS", message)
        self.assertIn("> 0", message)
        self.assertNotIn(raw, message)
        self.assertNotIn(f"{raw!r}", message)

    def test_below_range_timeout_never_echo_raw_value(self):
        raw = "-91337"
        with self.assertRaises(ValueError) as ctx:
            utils._parse_llm_api_timeout_seconds(raw)
        message = str(ctx.exception)
        self.assertIn("LLM_API_TIMEOUT_SECONDS", message)
        self.assertIn("> 0", message)
        self.assertNotIn(raw, message)
        self.assertNotIn(f"{raw!r}", message)


class RetryConfigImportTests(unittest.TestCase):
    """Malformed retry config aborts the import with a clear message."""

    def _import_utils(self, env):
        script = (
            "import sys; sys.path.insert(0, %r); import utils; "
            "print(utils.llm_api_retries, utils.llm_api_retry_base_seconds, "
            "utils.llm_api_timeout_seconds)"
        ) % _BACKEND_DIR
        return subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_defaults_when_env_unset(self):
        env = dict(os.environ)
        for name in (
            "LLM_API_RETRIES",
            "LLM_API_RETRY_BASE_SECONDS",
            "LLM_API_TIMEOUT_SECONDS",
        ):
            env.pop(name, None)
        result = self._import_utils(env)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "3 1.0 60.0")

    def test_valid_overrides_apply(self):
        env = dict(os.environ)
        env["LLM_API_RETRIES"] = "5"
        env["LLM_API_RETRY_BASE_SECONDS"] = "0.5"
        env["LLM_API_TIMEOUT_SECONDS"] = "30"
        result = self._import_utils(env)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "5 0.5 30.0")

    def test_malformed_retries_fail_fast(self):
        env = dict(os.environ)
        env["LLM_API_RETRIES"] = "abc"
        result = self._import_utils(env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LLM_API_RETRIES", result.stderr)

    def test_malformed_base_seconds_fail_fast(self):
        env = dict(os.environ)
        env["LLM_API_RETRY_BASE_SECONDS"] = "abc"
        result = self._import_utils(env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LLM_API_RETRY_BASE_SECONDS", result.stderr)

    def test_malformed_timeout_fail_fast(self):
        env = dict(os.environ)
        env["LLM_API_TIMEOUT_SECONDS"] = "0"
        result = self._import_utils(env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LLM_API_TIMEOUT_SECONDS", result.stderr)

    def test_non_finite_base_seconds_fail_fast_import(self):
        for raw in ("inf", "-inf", "nan"):
            env = dict(os.environ)
            env["LLM_API_RETRY_BASE_SECONDS"] = raw
            result = self._import_utils(env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("LLM_API_RETRY_BASE_SECONDS", result.stderr)
            self.assertIn("finite", result.stderr)
            self.assertNotIn(f"{raw!r}", result.stderr)

    def test_non_finite_timeout_fail_fast_import(self):
        for raw in ("inf", "-inf", "nan"):
            env = dict(os.environ)
            env["LLM_API_TIMEOUT_SECONDS"] = raw
            result = self._import_utils(env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("LLM_API_TIMEOUT_SECONDS", result.stderr)
            self.assertIn("finite", result.stderr)
            self.assertNotIn(f"{raw!r}", result.stderr)

    def test_malformed_retries_never_echo_raw_value_on_import(self):
        raw = "sk-invalid-retries-9f2a"
        env = dict(os.environ)
        env["LLM_API_RETRIES"] = raw
        result = self._import_utils(env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LLM_API_RETRIES", result.stderr)
        self.assertNotIn(raw, result.stderr)
        self.assertNotIn(f"{raw!r}", result.stderr)

    def test_malformed_base_seconds_never_echo_raw_value_on_import(self):
        raw = "sk-invalid-base-7c1e"
        env = dict(os.environ)
        env["LLM_API_RETRY_BASE_SECONDS"] = raw
        result = self._import_utils(env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LLM_API_RETRY_BASE_SECONDS", result.stderr)
        self.assertNotIn(raw, result.stderr)
        self.assertNotIn(f"{raw!r}", result.stderr)

    def test_malformed_timeout_never_echo_raw_value_on_import(self):
        raw = "sk-invalid-timeout-5b3d"
        env = dict(os.environ)
        env["LLM_API_TIMEOUT_SECONDS"] = raw
        result = self._import_utils(env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LLM_API_TIMEOUT_SECONDS", result.stderr)
        self.assertNotIn(raw, result.stderr)
        self.assertNotIn(f"{raw!r}", result.stderr)

    def test_below_range_retries_never_echo_raw_value_on_import(self):
        raw = "-91337"
        env = dict(os.environ)
        env["LLM_API_RETRIES"] = raw
        result = self._import_utils(env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LLM_API_RETRIES", result.stderr)
        self.assertNotIn(raw, result.stderr)

    def test_below_range_base_seconds_never_echo_raw_value_on_import(self):
        raw = "-91337"
        env = dict(os.environ)
        env["LLM_API_RETRY_BASE_SECONDS"] = raw
        result = self._import_utils(env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LLM_API_RETRY_BASE_SECONDS", result.stderr)
        self.assertNotIn(raw, result.stderr)

    def test_below_range_timeout_never_echo_raw_value_on_import(self):
        raw = "-91337"
        env = dict(os.environ)
        env["LLM_API_TIMEOUT_SECONDS"] = raw
        result = self._import_utils(env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LLM_API_TIMEOUT_SECONDS", result.stderr)
        self.assertNotIn(raw, result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

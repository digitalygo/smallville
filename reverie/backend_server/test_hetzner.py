"""Offline provider contract, shared dispatch gate, and retry coverage."""

import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import utils
from persona.prompt_template import gpt_structure as gpt
from provider_gate import SharedRequestGate, retry_after_seconds


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.waits = []

    def time(self):
        return self.now

    def sleep(self, seconds=0.1):
        self.waits.append(seconds)
        self.now += seconds


def gate_worker(directory, ready, output):
    ready.wait(5)
    with SharedRequestGate(directory, "synthetic-test-key", 0.03).slot():
        output.put(time.time())
        time.sleep(0.01)


class SharedGateTests(unittest.TestCase):
    def test_no_burst_after_idle_and_new_instances_share_budget(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory:
            starts = []
            for index in range(24):
                if index == 12:
                    clock.now += 600
                gate = SharedRequestGate(
                    directory, "synthetic-key", 6.1, clock.time, clock.sleep
                )
                with gate.slot():
                    starts.append(clock.now)
            for start in starts:
                self.assertLessEqual(
                    sum(start <= other < start + 60 for other in starts), 10
                )
            self.assertGreaterEqual(min(b - a for a, b in pairwise(starts)), 6.1 - 1e-9)
            self.assertNotIn(
                "synthetic-key",
                "".join(p.name + p.read_text() for p in Path(directory).iterdir()),
            )

    def test_concurrent_processes_serialize_dispatch(self):
        ctx = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            ready, output = ctx.Event(), ctx.Queue()
            workers = [
                ctx.Process(target=gate_worker, args=(directory, ready, output))
                for _ in range(4)
            ]
            try:
                for worker in workers:
                    worker.start()
                ready.set()
                starts = sorted(output.get(timeout=15) for _ in workers)
                for worker in workers:
                    worker.join(10)
                    self.assertEqual(worker.exitcode, 0)
                self.assertGreaterEqual(min(b - a for a, b in pairwise(starts)), 0.035)
            finally:
                for worker in workers:
                    if worker.is_alive():
                        worker.kill()
                        worker.join()
                output.close()

    def test_retry_after_cooldown_persists_for_other_workers(self):
        clock = Clock()
        failure = RuntimeError("synthetic failure")
        failure.response = SimpleNamespace(headers={"Retry-After": "90"})
        with tempfile.TemporaryDirectory() as directory:
            gate = SharedRequestGate(directory, "test", 6.1, clock.time, clock.sleep)
            with self.assertRaises(RuntimeError), gate.slot():
                raise failure
            with SharedRequestGate(
                directory, "test", 6.1, clock.time, clock.sleep
            ).slot():
                self.assertEqual(clock.now, 1090)
            self.assertEqual(clock.waits, [60, 30])

    def test_separate_keys_have_separate_budgets_and_corrupt_state_fails_closed(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory:
            for key in ("one", "two"):
                with SharedRequestGate(
                    directory, key, 6.1, clock.time, clock.sleep
                ).slot():
                    self.assertEqual(clock.now, 1000)
            gate = SharedRequestGate(directory, "one", 6.1, clock.time, clock.sleep)
            (Path(directory) / (gate.fingerprint + ".json")).write_text(
                '{"next_start":true}'
            )
            with self.assertRaises(ValueError), gate.slot():
                self.fail("corrupt state dispatched a request")

    def test_retry_after_seconds_and_http_date(self):
        failure = SimpleNamespace(
            response=SimpleNamespace(headers={"retry-after": "12"})
        )
        self.assertEqual(retry_after_seconds(failure), 12)
        failure.response.headers["retry-after"] = "Thu, 01 Jan 1970 00:01:40 GMT"
        self.assertEqual(retry_after_seconds(failure, now=90), 10)
        for raw in ("nan", "inf", "nonsense"):
            failure.response.headers["retry-after"] = raw
            self.assertIsNone(retry_after_seconds(failure))

    def test_429_without_retry_after_defers_the_shared_key_for_a_minute(self):
        clock = Clock()
        failure = RuntimeError("synthetic throttling")
        failure.status_code = 429
        with tempfile.TemporaryDirectory() as directory:
            gate = SharedRequestGate(directory, "test", 6.1, clock.time, clock.sleep)
            with self.assertRaises(RuntimeError), gate.slot():
                raise failure
            with gate.slot():
                self.assertEqual(clock.now, 1060)

    def test_non_regular_gate_file_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            gate = SharedRequestGate(directory, "test", 6.1)
            os.mkfifo(Path(directory) / (gate.fingerprint + ".json"))
            with self.assertRaises(ValueError), gate.slot():
                self.fail("non-regular gate dispatched a request")


class HetznerProviderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.patches = [
            patch.object(gpt, "hetzner_api_key", "synthetic-key"),
            patch.object(gpt, "hetzner_rate_limit_dir", self.directory.name),
            patch.object(gpt, "llm_provider", "hetzner"),
            patch.object(gpt, "llm_model", None),
            patch.object(gpt, "llm_persona_models", {}),
            patch.object(gpt, "llm_embedding_provider", None),
            patch.object(gpt, "llm_reasoning_effort", None),
            patch.object(gpt, "llm_api_retries", 2),
            patch.object(gpt, "temp_sleep", self.clock.sleep),
            patch.object(gpt, "_clients", {}),
            patch.object(
                gpt,
                "SharedRequestGate",
                side_effect=lambda directory, key, interval: SharedRequestGate(
                    directory, key, interval, self.clock.time, self.clock.sleep
                ),
            ),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.directory.cleanup)

    def test_exact_model_client_endpoint_and_no_sdk_retries(self):
        with patch.object(gpt, "OpenAI") as factory:
            gpt._get_client("hetzner")
        self.assertEqual(gpt._resolve_model(), ("hetzner", "Qwen3.8-27B"))
        self.assertEqual(
            factory.call_args.kwargs["base_url"], "https://inference.hetzner.com/api/v1"
        )
        self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
        self.assertNotIn("default_headers", factory.call_args.kwargs)
        persona = SimpleNamespace(
            scratch=SimpleNamespace(name="Ada", llm_provider=None, llm_model=None)
        )
        with patch.object(gpt, "llm_persona_models", {"Ada": "hetzner:Qwen3.8-27B"}):
            self.assertEqual(gpt._resolve_model(persona), ("hetzner", "Qwen3.8-27B"))

    def test_all_chat_wrappers_and_catalog_use_the_same_attempt_gate(self):
        starts = []
        client = Mock()
        client.chat.completions.create.side_effect = lambda **kw: (
            starts.append((self.clock.now, kw)),
            SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
            ),
        )[1]
        client.models.list.side_effect = lambda: (
            starts.append((self.clock.now, None)),
            SimpleNamespace(data=[SimpleNamespace(id="Qwen3.8-27B")]),
        )[1]
        with patch.object(gpt, "_get_client", return_value=client):
            gpt.verify_hetzner_models({"Qwen3.8-27B"})
            for call in (
                gpt.ChatGPT_request,
                gpt.ChatGPT_single_request,
                gpt.GPT4_request,
            ):
                self.assertEqual(call("Synthetic resident takes a walk."), "ok")
            self.assertEqual(
                gpt.GPT_request("Synthetic resident", {"temperature": 0}), "ok"
            )
        self.assertGreaterEqual(
            min(b[0] - a[0] for a, b in pairwise(starts)), 6.1 - 1e-9
        )
        for _, kwargs in starts[1:]:
            self.assertEqual(kwargs["model"], "Qwen3.8-27B")

    def test_429_attempts_share_budget_and_honor_retry_after(self):
        failure = RuntimeError("private response must stay out of error")
        failure.status_code = 429
        failure.response = SimpleNamespace(headers={"Retry-After": "20"})
        starts = []

        def call():
            starts.append(self.clock.now)
            if len(starts) < 3:
                raise failure
            return "ok"

        self.assertEqual(gpt._call_with_retries("hetzner", "chat", call), "ok")
        self.assertEqual(len(starts), 3)
        self.assertTrue(all(b - a >= 20 for a, b in pairwise(starts)))

    def test_auth_error_has_one_attempt_and_sanitized_error(self):
        failure = RuntimeError("private body synthetic-key")
        failure.status_code = 401
        call = Mock(side_effect=failure)
        with self.assertRaises(utils.LLMProviderError) as raised:
            gpt._call_with_retries("hetzner", "chat", call)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(raised.exception.attempts, 1)
        self.assertNotIn("synthetic-key", str(raised.exception))

    def test_missing_catalog_model_never_substitutes(self):
        client = Mock()
        client.models.list.return_value = SimpleNamespace(
            data=[SimpleNamespace(id="other")]
        )
        with (
            patch.object(gpt, "_get_client", return_value=client),
            self.assertRaisesRegex(ValueError, "unavailable"),
        ):
            gpt.verify_hetzner_models({"Qwen3.8-27B"})
        client.chat.completions.create.assert_not_called()

    def test_malformed_catalog_is_bounded_and_sanitized(self):
        client = Mock()
        client.models.list.return_value = SimpleNamespace(data=[SimpleNamespace()])
        with (
            patch.object(gpt, "_get_client", return_value=client),
            self.assertRaises(utils.LLMProviderError),
        ):
            gpt.verify_hetzner_models({"Qwen3.8-27B"})
        self.assertEqual(client.models.list.call_count, 3)

    def test_startup_refuses_implicit_embeddings_and_checks_catalog_once(self):
        import reverie

        with patch.object(gpt, "verify_hetzner_models") as catalog:
            with self.assertRaisesRegex(ValueError, "LLM_EMBEDDING_PROVIDER"):
                reverie._validate_provider_keys({})
            catalog.assert_not_called()
            with (
                patch.object(gpt, "llm_embedding_provider", "openrouter"),
                patch.object(reverie, "hetzner_api_key", "synthetic"),
                patch.object(reverie, "openrouter_api_key", "synthetic"),
                patch.object(reverie, "llm_reasoning_effort", None),
            ):
                reverie._validate_provider_keys({})
            catalog.assert_called_once_with({"Qwen3.8-27B"})

    def test_embeddings_require_explicit_route_without_contacting_a_provider(self):
        with (
            patch.object(gpt, "_get_client") as client,
            self.assertRaisesRegex(ValueError, "LLM_EMBEDDING_PROVIDER"),
        ):
            gpt.get_embedding("Synthetic memory")
        client.assert_not_called()
        with patch.object(gpt, "llm_embedding_provider", "openrouter"):
            self.assertEqual(gpt._resolve_embedding_provider(), "openrouter")

    def test_rpm_and_reasoning_configuration_fail_closed(self):
        for raw in ("0", "11", "abc", "nan", "1.5"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                utils._parse_hetzner_rpm(raw)
        self.assertEqual(utils._parse_hetzner_rpm(""), 10)
        self.assertEqual(utils.reasoning_effort_kwargs("hetzner", None), {})
        with self.assertRaisesRegex(ValueError, "unset"):
            utils.reasoning_effort_kwargs("hetzner", "high")

"""Replay, checkpoint, and persona state tests for the Smallville Django frontend.

Covers replay step bounds, checkpoint pointer authority, and persona
state rendering from checkpoint, legacy, and compressed storage."""

import gc
import json
import os
import shutil
import tempfile
import time
import unittest
import warnings

from pathlib import Path
from unittest.mock import patch

from django.test import RequestFactory, TestCase
from django.contrib.auth.models import User

from translator import views
from translator.test_helpers import (
    _remove_runtime_markers,
    _write_complete_checkpoint,
    _write_valid_bootstrap,
)


class ReplayCheckpointAuthorityTests(TestCase):
    """replay treats a committed checkpoint pointer as fully authoritative."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client.force_login(self.user)
        self.sim_code = "test_replay_checkpoint"
        self.sim_dir = Path("storage") / self.sim_code
        self.env_dir = self.sim_dir / "environment"
        self.personas_dir = self.sim_dir / "personas"
        self.checkpoints = self.sim_dir / "reverie" / "checkpoints"
        self.env_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(Path("storage") / self.sim_code, ignore_errors=True)

    def _positions(self):
        return {
            "Isabella Rodriguez": (1, 1),
            "Maria Lopez": (2, 2),
            "Klaus Mueller": (3, 3),
        }

    def _write_env(self, step, positions):
        env = {name: {"x": x, "y": y} for name, (x, y) in positions.items()}
        (self.env_dir / f"{step}.json").write_text(json.dumps(env), encoding="utf-8")

    def _init_pos(self, response):
        return {
            name: (x, y) for name, x, y in response.context["persona_init_pos"]
        }

    def _three_personas(self):
        return [
            ("Isabella Rodriguez", "checkpoint"),
            ("Maria Lopez", "maria-checkpoint"),
            ("Klaus Mueller", "klaus-checkpoint"),
        ]

    def test_corrupt_pointer_404_no_legacy_fallback(self):
        for name in ("Isabella Rodriguez", "Maria Lopez", "Klaus Mueller"):
            (self.personas_dir / name).mkdir(parents=True, exist_ok=True)
        self._write_env(0, self._positions())
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        (self.checkpoints / "pointer.json").write_text(
            "{not valid json", encoding="utf-8"
        )
        response = self.client.get(f"/replay/{self.sim_code}/0/")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Isabella Rodriguez", response.content.decode())

    def test_null_pointer_404_no_legacy_fallback(self):
        for name in ("Isabella Rodriguez", "Maria Lopez", "Klaus Mueller"):
            (self.personas_dir / name).mkdir(parents=True, exist_ok=True)
        self._write_env(0, self._positions())
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        (self.checkpoints / "pointer.json").write_text("null", encoding="utf-8")
        response = self.client.get(f"/replay/{self.sim_code}/0/")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Isabella Rodriguez", response.content.decode())

    def test_incomplete_pointer_404_no_legacy_fallback(self):
        for name in ("Isabella Rodriguez", "Maria Lopez", "Klaus Mueller"):
            (self.personas_dir / name).mkdir(parents=True, exist_ok=True)
        self._write_env(0, self._positions())
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        (self.checkpoints / "pointer.json").write_text(
            json.dumps({"version": 1, "generation": "0000000099"}),
            encoding="utf-8",
        )
        response = self.client.get(f"/replay/{self.sim_code}/0/")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Isabella Rodriguez", response.content.decode())

    def test_checkpoint_persona_names_supersede_stale_legacy(self):
        (self.personas_dir / "Stale Persona").mkdir(parents=True, exist_ok=True)
        self._write_env(0, self._positions())
        _write_complete_checkpoint(self.sim_dir, "0000000001", self._three_personas())
        response = self.client.get(f"/replay/{self.sim_code}/0/")
        self.assertEqual(response.status_code, 200)
        names = {name for name, _ in response.context["persona_names"]}
        self.assertEqual(
            names, {"Isabella Rodriguez", "Maria Lopez", "Klaus Mueller"}
        )
        self.assertEqual(self._init_pos(response)["Isabella Rodriguez"], (1, 1))

    def test_authoritative_maze_on_replay(self):
        meta = {
            "persona_names": [
                "Isabella Rodriguez",
                "Maria Lopez",
                "Klaus Mueller",
            ],
            "maze_name": "the_ville",
        }
        _write_complete_checkpoint(self.sim_dir, "0000000001", self._three_personas(), meta=meta)
        self._write_env(0, self._positions())
        self._write_env(
            1,
            {
                "Isabella Rodriguez": (140, 1),
                "Maria Lopez": (2, 2),
                "Klaus Mueller": (3, 3),
            },
        )
        response = self.client.get(f"/replay/{self.sim_code}/1/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["initial_phase"], "process")
        self.assertEqual(self._init_pos(response)["Isabella Rodriguez"], (1, 1))
        self.assertFalse((self.sim_dir / "reverie" / "meta.json").exists())

    def test_valid_complete_checkpoint_passes(self):
        _write_complete_checkpoint(self.sim_dir, "0000000001", self._three_personas())
        self._write_env(0, self._positions())
        response = self.client.get(f"/replay/{self.sim_code}/0/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["initial_phase"], "update")
        names = {name for name, _ in response.context["persona_names"]}
        self.assertEqual(
            names, {"Isabella Rodriguez", "Maria Lopez", "Klaus Mueller"}
        )
        self.assertEqual(self._init_pos(response)["Isabella Rodriguez"], (1, 1))


class ReplayStepBoundsTests(TestCase):
    """Authenticated replay rejects steps beyond the authoritative maximum."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client.force_login(self.user)
        self.sim_code = "test_replay_bounds"
        self.sim_dir = Path("storage") / self.sim_code
        self.env_dir = self.sim_dir / "environment"
        self.personas_dir = self.sim_dir / "personas"
        for name in ("Isabella Rodriguez", "Maria Lopez", "Klaus Mueller"):
            (self.personas_dir / name).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(Path("storage") / self.sim_code, ignore_errors=True)

    def _write_env(self, step):
        env = {
            "Isabella Rodriguez": {"x": 1, "y": 1},
            "Maria Lopez": {"x": 2, "y": 2},
            "Klaus Mueller": {"x": 3, "y": 3},
        }
        self.env_dir.mkdir(parents=True, exist_ok=True)
        (self.env_dir / f"{step}.json").write_text(json.dumps(env), encoding="utf-8")

    def test_replay_huge_step_bounded_scan_and_quick_controlled_response(self):
        self._write_env(0)
        self._write_env(5)
        scanned = []
        real_listdir = views.os.listdir

        def counting_listdir(path):
            scanned.append(path)
            return real_listdir(path)

        started = time.monotonic()
        with patch("translator.views.os.listdir", side_effect=counting_listdir):
            response = self.client.get(f"/replay/{self.sim_code}/9999999999/")
        elapsed = time.monotonic() - started
        self.assertIn(response.status_code, (400, 404))
        self.assertLess(elapsed, 5.0)
        self.assertEqual(len(scanned), 1)
        self.assertIsInstance(scanned[0], int)

    def test_replay_step_beyond_max_never_falls_back(self):
        self._write_env(0)
        self._write_env(5)
        response = self.client.get(f"/replay/{self.sim_code}/6/")
        self.assertIn(response.status_code, (400, 404))
        self.assertNotIn("Isabella Rodriguez", response.content.decode())

    def test_replay_movement_runs_without_backend_to_completion(self):
        self._write_env(0)
        self._write_env(1)
        movement_dir = self.sim_dir / "movement"
        movement_dir.mkdir(parents=True, exist_ok=True)
        movements = {
            0: {"persona": {"Isabella Rodriguez": {"movement": [1, 1]}}, "meta": {}},
            1: {"persona": {"Isabella Rodriguez": {"movement": [2, 2]}}, "meta": {}},
            2: {"persona": {"Isabella Rodriguez": {"movement": [3, 3]}}, "meta": {}},
        }
        for step in (0, 2):
            (movement_dir / f"{step}.json").write_text(
                json.dumps(movements[step]), encoding="utf-8"
            )
        _remove_runtime_markers()
        with patch(
            "translator.views._backend_lease_held",
            side_effect=AssertionError("replay must not probe backend liveness"),
        ):
            response = self.client.get(f"/replay/{self.sim_code}/0/")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["mode"], "replay")
            self.assertEqual(response.context["initial_phase"], "update")
            response = self.client.post(
                "/replay_movement/",
                data=json.dumps({"sim_code": self.sim_code, "step": 1}),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json(), {"<step>": -1, "status": "pending"})
            (movement_dir / "1.json").write_text(
                json.dumps(movements[1]), encoding="utf-8"
            )
            for step in movements:
                response = self.client.post(
                    "/replay_movement/",
                    data=json.dumps({"sim_code": self.sim_code, "step": step}),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["<step>"], step)
            response = self.client.post(
                "/replay_movement/",
                data=json.dumps({"sim_code": self.sim_code, "step": 3}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"<step>": 3, "status": "complete"})
        self.assertFalse((Path("temp_storage") / "curr_step.json").exists())
        self.assertFalse((Path("temp_storage") / "curr_sim_code.json").exists())


class ReplayPersonaStateTests(TestCase):
    """replay_persona_state treats the checkpoint pointer as authoritative.

    A valid pointer's complete generation supplies the persona bootstrap
    memory: meta.json must be a JSON object with valid nonempty unique safe
    persona_names, the generation tree must be real directories, and every
    declared persona must have complete bootstrap memory files. Anything
    less fails controlled without stale legacy fallback. The legacy layout
    is read only when no pointer exists, and compressed_storage only when
    there is no live storage or pointer. Malformed, traversal, or
    incomplete pointers fail controlled instead of silently showing stale
    legacy state, and every JSON read is descriptor-safe so symlinks can
    never surface external content.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client.force_login(self.user)
        self.sim_code = "test_persona_state"
        self.sim_dir = Path("storage") / self.sim_code
        self.persona_dir = self.sim_dir / "personas" / "Isabella Rodriguez"
        self.legacy_memory = self.persona_dir / "bootstrap_memory"
        self.checkpoints = self.sim_dir / "reverie" / "checkpoints"
        self.compressed_memory = (
            Path("compressed_storage")
            / self.sim_code
            / "personas"
            / "Isabella Rodriguez"
            / "bootstrap_memory"
        )

    def tearDown(self):
        shutil.rmtree(Path("storage") / self.sim_code, ignore_errors=True)
        shutil.rmtree(
            Path("compressed_storage") / self.sim_code, ignore_errors=True
        )

    def _url(self, persona="Isabella_Rodriguez"):
        return f"/replay_persona_state/{self.sim_code}/5/{persona}/"

    def _write_bootstrap(self, memory: Path, marker: str):
        _write_valid_bootstrap(memory, marker)

    def _write_legacy(self, marker="legacy"):
        self._write_bootstrap(self.legacy_memory, marker)

    def _write_checkpoint(
        self,
        generation="0000000001",
        marker="checkpoint",
        personas=None,
        meta=None,
    ):
        gen_dir = self.checkpoints / generation
        effective = (
            personas if personas is not None else [("Isabella Rodriguez", marker)]
        )
        for name, person_marker in effective:
            self._write_bootstrap(
                gen_dir / "personas" / name / "bootstrap_memory",
                person_marker,
            )
        (gen_dir / "meta.json").write_text(
            json.dumps(
                meta
                if meta is not None
                else {"persona_names": [name for name, _ in effective]}
            ),
            encoding="utf-8",
        )
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        (self.checkpoints / "pointer.json").write_text(
            json.dumps({"version": 1, "generation": generation}),
            encoding="utf-8",
        )

    def _write_compressed(self, marker="compressed"):
        self._write_bootstrap(self.compressed_memory, marker)

    def _assert_marker(self, response, marker):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["scratch"], {"name": marker})
        self.assertEqual(response.context["spatial"], {"spatial": marker})
        self.assertEqual(
            response.context["a_mem_event"][0]["description"], f"event {marker}"
        )
        self.assertEqual(response.context["persona_name"], "Isabella Rodriguez")
        self.assertEqual(response.context["step"], 5)

    def test_checkpoint_pointer_authoritative_over_legacy(self):
        self._write_legacy("legacy")
        self._write_checkpoint("0000000001", "checkpoint")
        self._assert_marker(self.client.get(self._url()), "checkpoint")

    def test_checkpoint_latest_generation_read_not_stale_prior(self):
        self._write_checkpoint("0000000001", "older")
        self._write_checkpoint("0000000002", "newer")
        self._assert_marker(self.client.get(self._url()), "newer")

    def test_legacy_layout_without_pointer(self):
        self._write_legacy("legacy")
        response = self.client.get(self._url())
        self._assert_marker(response, "legacy")

    def test_compressed_storage_fallback_without_live_storage(self):
        self._write_compressed("compressed")
        response = self.client.get(self._url())
        self._assert_marker(response, "compressed")

    def test_malformed_pointer_fails_controlled_not_legacy(self):
        self._write_legacy("legacy")
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        (self.checkpoints / "pointer.json").write_text(
            "{not valid json", encoding="utf-8"
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 500)
        self.assertIn("pointer", response.json()["error"])

    def test_null_list_scalar_pointer_fails_controlled_not_legacy_or_compressed(self):
        self._write_legacy("legacy")
        self._write_compressed("compressed")
        for payload in ("null", "[]", '"generation"', "42"):
            with self.subTest(payload=payload):
                self.checkpoints.mkdir(parents=True, exist_ok=True)
                (self.checkpoints / "pointer.json").write_text(
                    payload, encoding="utf-8"
                )
                response = self.client.get(self._url())
                self.assertEqual(response.status_code, 500)
                self.assertIn("pointer", response.json()["error"])
                self.assertNotIn("legacy", response.content.decode())
                self.assertNotIn("compressed", response.content.decode())

    def test_traversal_generation_pointer_fails_controlled(self):
        self._write_legacy("legacy")
        for generation in ("../..", "..", "../../etc", "0/../.."):
            with self.subTest(generation=generation):
                self.checkpoints.mkdir(parents=True, exist_ok=True)
                (self.checkpoints / "pointer.json").write_text(
                    json.dumps({"version": 1, "generation": generation}),
                    encoding="utf-8",
                )
                response = self.client.get(self._url())
                self.assertEqual(response.status_code, 500)
                self.assertIn("generation", response.json()["error"])

    def test_unsupported_pointer_version_fails_controlled(self):
        self._write_legacy("legacy")
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        (self.checkpoints / "pointer.json").write_text(
            json.dumps({"version": 99, "generation": "0000000001"}),
            encoding="utf-8",
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 500)
        self.assertIn("version", response.json()["error"])

    def test_missing_pointed_generation_fails_controlled(self):
        self._write_legacy("legacy")
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        (self.checkpoints / "pointer.json").write_text(
            json.dumps({"version": 1, "generation": "0000000099"}),
            encoding="utf-8",
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_incomplete_pointed_persona_fails_controlled(self):
        self._write_legacy("legacy")
        gen_dir = self.checkpoints / "0000000001"
        partial = gen_dir / "personas" / "Isabella Rodriguez" / "bootstrap_memory"
        partial.mkdir(parents=True, exist_ok=True)
        (partial / "scratch.json").write_text(
            json.dumps({"name": "partial"}), encoding="utf-8"
        )
        (partial / "spatial_memory.json").write_text(
            json.dumps({"spatial": "partial"}), encoding="utf-8"
        )
        (gen_dir / "meta.json").write_text(
            json.dumps({"persona_names": ["Isabella Rodriguez"]}),
            encoding="utf-8",
        )
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        (self.checkpoints / "pointer.json").write_text(
            json.dumps({"version": 1, "generation": "0000000001"}),
            encoding="utf-8",
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_partial_legacy_dir_does_not_fall_back_to_compressed(self):
        self.legacy_memory.mkdir(parents=True, exist_ok=True)
        (self.legacy_memory / "scratch.json").write_text(
            json.dumps({"name": "partial"}), encoding="utf-8"
        )
        self._write_compressed("compressed")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def _assert_live_root_blocks_compressed(self, response, opened):
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("compressed_storage", opened)

    def _recording_open(self, opened):
        real_open = views.os.open

        def recording_open(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        return recording_open

    def test_empty_live_root_404_no_compressed_fallback(self):
        self.sim_dir.mkdir(parents=True, exist_ok=True)
        self._write_compressed("compressed")
        opened = []
        with patch(
            "translator.views.os.open", side_effect=self._recording_open(opened)
        ):
            response = self.client.get(self._url())
        self._assert_live_root_blocks_compressed(response, opened)

    def test_live_root_missing_personas_404_no_compressed_fallback(self):
        self.sim_dir.mkdir(parents=True, exist_ok=True)
        (self.sim_dir / "environment").mkdir(parents=True, exist_ok=True)
        self._write_compressed("compressed")
        opened = []
        with patch(
            "translator.views.os.open", side_effect=self._recording_open(opened)
        ):
            response = self.client.get(self._url())
        self._assert_live_root_blocks_compressed(response, opened)

    def test_live_root_missing_persona_404_no_compressed_fallback(self):
        (self.sim_dir / "personas").mkdir(parents=True, exist_ok=True)
        self._write_compressed("compressed")
        opened = []
        with patch(
            "translator.views.os.open", side_effect=self._recording_open(opened)
        ):
            response = self.client.get(self._url())
        self._assert_live_root_blocks_compressed(response, opened)

    def test_live_root_missing_bootstrap_404_no_compressed_fallback(self):
        (self.sim_dir / "personas" / "Isabella Rodriguez").mkdir(
            parents=True, exist_ok=True
        )
        self._write_compressed("compressed")
        opened = []
        with patch(
            "translator.views.os.open", side_effect=self._recording_open(opened)
        ):
            response = self.client.get(self._url())
        self._assert_live_root_blocks_compressed(response, opened)

    def test_live_root_partial_bootstrap_404_no_compressed_fallback(self):
        memory = self.legacy_memory
        memory.mkdir(parents=True, exist_ok=True)
        (memory / "scratch.json").write_text(
            json.dumps({"name": "partial"}), encoding="utf-8"
        )
        self._write_compressed("compressed")
        opened = []
        with patch(
            "translator.views.os.open", side_effect=self._recording_open(opened)
        ):
            response = self.client.get(self._url())
        self._assert_live_root_blocks_compressed(response, opened)

    def test_checkpoint_partial_does_not_fall_back_to_legacy(self):
        self._write_legacy("legacy")
        gen_dir = self.checkpoints / "0000000001"
        (gen_dir / "personas" / "Isabella Rodriguez" / "bootstrap_memory").mkdir(
            parents=True, exist_ok=True
        )
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        (self.checkpoints / "pointer.json").write_text(
            json.dumps({"version": 1, "generation": "0000000001"}),
            encoding="utf-8",
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_legacy_symlink_scratch_refused(self):
        external_dir = Path(tempfile.mkdtemp())
        victim = external_dir / "victim.json"
        victim.write_text(json.dumps({"name": "external"}), encoding="utf-8")
        self._write_bootstrap(self.legacy_memory, "legacy")
        (self.legacy_memory / "scratch.json").unlink()
        (self.legacy_memory / "scratch.json").symlink_to(victim)
        opened = []
        real_open = views.os.open

        def recording_open(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        try:
            with patch("translator.views.os.open", side_effect=recording_open):
                response = self.client.get(self._url())
        finally:
            shutil.rmtree(external_dir, ignore_errors=True)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(str(victim), opened)

    def test_pointer_symlink_fails_controlled(self):
        self._write_legacy("legacy")
        external_dir = Path(tempfile.mkdtemp())
        victim = external_dir / "victim.json"
        victim.write_text(json.dumps({"version": 1, "generation": "0000000001"}))
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        (self.checkpoints / "pointer.json").symlink_to(victim)
        opened = []
        real_open = views.os.open

        def recording_open(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        try:
            with patch("translator.views.os.open", side_effect=recording_open):
                response = self.client.get(self._url())
        finally:
            shutil.rmtree(external_dir, ignore_errors=True)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(str(victim), opened)

    def test_checkpoint_persona_symlink_refused(self):
        self._write_checkpoint("0000000001", "checkpoint")
        external_dir = Path(tempfile.mkdtemp())
        victim = external_dir / "victim.json"
        victim.write_text(json.dumps({"name": "external"}), encoding="utf-8")
        gen_memory = (
            self.checkpoints
            / "0000000001"
            / "personas"
            / "Isabella Rodriguez"
            / "bootstrap_memory"
        )
        (gen_memory / "scratch.json").unlink()
        (gen_memory / "scratch.json").symlink_to(victim)
        opened = []
        real_open = views.os.open

        def recording_open(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        try:
            with patch("translator.views.os.open", side_effect=recording_open):
                response = self.client.get(self._url())
        finally:
            shutil.rmtree(external_dir, ignore_errors=True)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(str(victim), opened)

    def test_generation_missing_meta_fails_controlled(self):
        self._write_legacy("legacy")
        gen_dir = self.checkpoints / "0000000001"
        self._write_bootstrap(
            gen_dir / "personas" / "Isabella Rodriguez" / "bootstrap_memory",
            "checkpoint",
        )
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        (self.checkpoints / "pointer.json").write_text(
            json.dumps({"version": 1, "generation": "0000000001"}),
            encoding="utf-8",
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_malformed_meta_json_fails_controlled(self):
        self._write_legacy("legacy")
        gen_dir = self.checkpoints / "0000000001"
        self._write_bootstrap(
            gen_dir / "personas" / "Isabella Rodriguez" / "bootstrap_memory",
            "checkpoint",
        )
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        (gen_dir / "meta.json").write_text("{not valid json", encoding="utf-8")
        (self.checkpoints / "pointer.json").write_text(
            json.dumps({"version": 1, "generation": "0000000001"}),
            encoding="utf-8",
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_invalid_meta_persona_names_fail_controlled(self):
        self._write_legacy("legacy")
        bad_metas = [
            ["Isabella Rodriguez"],
            {"persona_names": "Isabella Rodriguez"},
            {},
            {"persona_names": []},
            {"persona_names": [None]},
            {"persona_names": [{"name": "Isabella Rodriguez"}]},
            {"persona_names": [["Isabella Rodriguez"]]},
            {"persona_names": ["Isabella Rodriguez", "Isabella Rodriguez"]},
            {"persona_names": ["../escape"]},
            {"persona_names": ["Isabella/Rodriguez"]},
        ]
        for bad_meta in bad_metas:
            with self.subTest(meta=bad_meta):
                gen_dir = self.checkpoints / "0000000001"
                shutil.rmtree(gen_dir, ignore_errors=True)
                self._write_bootstrap(
                    gen_dir / "personas" / "Isabella Rodriguez" / "bootstrap_memory",
                    "checkpoint",
                )
                (gen_dir / "meta.json").write_text(
                    json.dumps(bad_meta), encoding="utf-8"
                )
                self.checkpoints.mkdir(parents=True, exist_ok=True)
                (self.checkpoints / "pointer.json").write_text(
                    json.dumps({"version": 1, "generation": "0000000001"}),
                    encoding="utf-8",
                )
                response = self.client.get(self._url())
                self.assertEqual(response.status_code, 404)

    def test_missing_other_declared_persona_fails_controlled(self):
        self._write_legacy("legacy")
        self._write_checkpoint(
            "0000000001",
            personas=[("Isabella Rodriguez", "checkpoint")],
            meta={"persona_names": ["Isabella Rodriguez", "Maria Lopez"]},
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_requested_persona_not_declared_fails_controlled(self):
        self._write_legacy("legacy")
        self._write_checkpoint("0000000001", "checkpoint")
        response = self.client.get(self._url(persona="Klaus_Mueller"))
        self.assertEqual(response.status_code, 404)

    def test_checkpoint_symlink_tree_fails_controlled(self):
        self._write_legacy("legacy")
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        (self.checkpoints / "pointer.json").write_text(
            json.dumps({"version": 1, "generation": "0000000001"}),
            encoding="utf-8",
        )
        external_dir = Path(tempfile.mkdtemp())
        try:
            external_gen = external_dir / "generation"
            self._write_bootstrap(
                external_gen / "personas" / "Isabella Rodriguez" / "bootstrap_memory",
                "external",
            )
            (external_gen / "meta.json").write_text(
                json.dumps({"persona_names": ["Isabella Rodriguez"]}),
                encoding="utf-8",
            )
            victim = external_dir / "victim.json"
            victim.write_text(json.dumps({"name": "external"}), encoding="utf-8")
            real_open = views.os.open

            def _remove(path):
                if path.is_symlink():
                    path.unlink()
                else:
                    shutil.rmtree(path, ignore_errors=True)

            def build_case(kind):
                gen_dir = self.checkpoints / "0000000001"
                _remove(gen_dir)
                memory = (
                    gen_dir / "personas" / "Isabella Rodriguez" / "bootstrap_memory"
                )
                self._write_bootstrap(memory, "checkpoint")
                (gen_dir / "meta.json").write_text(
                    json.dumps({"persona_names": ["Isabella Rodriguez"]}),
                    encoding="utf-8",
                )
                if kind == "generation_dir":
                    _remove(gen_dir)
                    gen_dir.symlink_to(external_gen)
                elif kind == "personas_dir":
                    _remove(gen_dir / "personas")
                    (gen_dir / "personas").symlink_to(external_gen / "personas")
                elif kind == "persona_dir":
                    _remove(gen_dir / "personas" / "Isabella Rodriguez")
                    (gen_dir / "personas" / "Isabella Rodriguez").symlink_to(
                        external_gen / "personas" / "Isabella Rodriguez"
                    )
                elif kind == "bootstrap_dir":
                    _remove(memory)
                    memory.symlink_to(
                        external_gen
                        / "personas"
                        / "Isabella Rodriguez"
                        / "bootstrap_memory"
                    )
                elif kind == "associative_dir":
                    _remove(memory / "associative_memory")
                    (memory / "associative_memory").symlink_to(
                        external_gen
                        / "personas"
                        / "Isabella Rodriguez"
                        / "bootstrap_memory"
                        / "associative_memory"
                    )
                elif kind == "scratch_file":
                    (memory / "scratch.json").unlink()
                    (memory / "scratch.json").symlink_to(victim)
                elif kind == "spatial_file":
                    (memory / "spatial_memory.json").unlink()
                    (memory / "spatial_memory.json").symlink_to(victim)
                elif kind == "nodes_file":
                    (memory / "associative_memory" / "nodes.json").unlink()
                    (memory / "associative_memory" / "nodes.json").symlink_to(victim)

            for kind in (
                "generation_dir",
                "personas_dir",
                "persona_dir",
                "bootstrap_dir",
                "associative_dir",
                "scratch_file",
                "spatial_file",
                "nodes_file",
            ):
                with self.subTest(kind=kind):
                    build_case(kind)
                    opened = []

                    def recording_open(path, *args, **kwargs):
                        opened.append(str(path))
                        return real_open(path, *args, **kwargs)

                    with patch("translator.views.os.open", side_effect=recording_open):
                        response = self.client.get(self._url())
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn(str(external_gen), opened)
                    self.assertNotIn(str(victim), opened)
        finally:
            shutil.rmtree(external_dir, ignore_errors=True)

    def test_complete_two_persona_checkpoint_reads_requested_state(self):
        self._write_legacy("legacy")
        self._write_checkpoint(
            "0000000001",
            personas=[
                ("Isabella Rodriguez", "checkpoint"),
                ("Maria Lopez", "maria-checkpoint"),
            ],
        )
        response = self.client.get(self._url())
        self._assert_marker(response, "checkpoint")
        response = self.client.get(self._url(persona="Maria_Lopez"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["scratch"], {"name": "maria-checkpoint"})
        self.assertEqual(response.context["spatial"], {"spatial": "maria-checkpoint"})
        self.assertEqual(response.context["persona_name"], "Maria Lopez")
        self.assertEqual(
            response.context["a_mem_event"][0]["description"],
            "event maria-checkpoint",
        )
        self.assertEqual(response.context["step"], 5)

    def _checkpoint_memory(self, generation="0000000001"):
        return (
            self.checkpoints
            / generation
            / "personas"
            / "Isabella Rodriguez"
            / "bootstrap_memory"
        )

    def _corrupt_shape(self, relative, payload):
        memory = self._checkpoint_memory()
        target = memory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")

    def test_malformed_scratch_shape_fails_controlled(self):
        self._write_legacy("legacy")
        self._write_checkpoint("0000000001", "checkpoint")
        self._corrupt_shape("scratch.json", ["not", "an", "object"])
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_malformed_spatial_shape_fails_controlled(self):
        self._write_legacy("legacy")
        self._write_checkpoint("0000000001", "checkpoint")
        self._corrupt_shape("spatial_memory.json", "not-an-object")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_malformed_embeddings_shape_fails_controlled(self):
        self._write_legacy("legacy")
        self._write_checkpoint("0000000001", "checkpoint")
        self._corrupt_shape("associative_memory/embeddings.json", [1, 2, 3])
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_malformed_kw_strength_shape_fails_controlled(self):
        self._write_legacy("legacy")
        self._write_checkpoint("0000000001", "checkpoint")
        self._corrupt_shape("associative_memory/kw_strength.json", "broken")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_malformed_nodes_json_fails_controlled(self):
        self._write_legacy("legacy")
        self._write_checkpoint("0000000001", "checkpoint")
        (self._checkpoint_memory() / "associative_memory" / "nodes.json").write_text(
            "{not valid json", encoding="utf-8"
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_nodes_not_object_fails_controlled(self):
        self._write_legacy("legacy")
        self._write_checkpoint("0000000001", "checkpoint")
        self._corrupt_shape("associative_memory/nodes.json", ["node_1"])
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_noncontiguous_node_keys_fails_controlled(self):
        self._write_legacy("legacy")
        self._write_checkpoint("0000000001", "checkpoint")
        self._corrupt_shape(
            "associative_memory/nodes.json",
            {
                "node_1": {
                    "type": "event",
                    "node_count": 1,
                    "created": "2023-01-01 00:00:00",
                    "description": "first",
                    "subject": "s",
                    "predicate": "p",
                    "object": "o",
                },
                "node_3": {
                    "type": "event",
                    "node_count": 2,
                    "created": "2023-01-01 00:00:00",
                    "description": "gap",
                    "subject": "s",
                    "predicate": "p",
                    "object": "o",
                },
            },
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_node_unsupported_type_fails_controlled(self):
        self._write_legacy("legacy")
        self._write_checkpoint("0000000001", "checkpoint")
        self._corrupt_shape(
            "associative_memory/nodes.json",
            {
                "node_1": {
                    "type": "dream",
                    "node_count": 1,
                    "created": "2023-01-01 00:00:00",
                    "description": "bad type",
                }
            },
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_node_missing_template_field_fails_controlled(self):
        self._write_legacy("legacy")
        self._write_checkpoint("0000000001", "checkpoint")
        self._corrupt_shape(
            "associative_memory/nodes.json",
            {
                "node_1": {
                    "type": "event",
                    "node_count": 1,
                    "created": "2023-01-01 00:00:00",
                }
            },
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_node_missing_thought_fields_fails_controlled(self):
        self._write_legacy("legacy")
        self._write_checkpoint("0000000001", "checkpoint")
        self._corrupt_shape(
            "associative_memory/nodes.json",
            {
                "node_1": {
                    "type": "thought",
                    "node_count": 1,
                    "created": "2023-01-01 00:00:00",
                    "description": "thought without depth",
                }
            },
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_chat_and_thought_nodes_render(self):
        self._write_legacy("legacy")
        self._write_checkpoint("0000000001", "checkpoint")
        self._corrupt_shape(
            "associative_memory/nodes.json",
            {
                "node_1": {
                    "type": "event",
                    "node_count": 1,
                    "created": "2023-01-01 00:00:00",
                    "description": "event one",
                    "subject": "s",
                    "predicate": "p",
                    "object": "o",
                },
                "node_2": {
                    "type": "chat",
                    "node_count": 2,
                    "created": "2023-01-01 00:00:00",
                    "description": "chat one",
                    "filling": [["Isabella Rodriguez", "hello"]],
                },
                "node_3": {
                    "type": "thought",
                    "node_count": 3,
                    "created": "2023-01-01 00:00:00",
                    "description": "thought one",
                    "depth": 2,
                    "filling": ["evidence"],
                },
            },
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [node["description"] for node in response.context["a_mem_event"]],
            ["event one"],
        )
        self.assertEqual(
            [node["description"] for node in response.context["a_mem_chat"]],
            ["chat one"],
        )
        self.assertEqual(
            [node["description"] for node in response.context["a_mem_thought"]],
            ["thought one"],
        )
        self.assertEqual(
            response.context["a_mem_thought"][0]["filling"], ["evidence"]
        )

    def test_checkpoint_dir_without_pointer_keeps_legacy(self):
        self._write_legacy("legacy")
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        response = self.client.get(self._url())
        self._assert_marker(response, "legacy")

    def test_unsafe_persona_name_returns_400(self):
        request = RequestFactory().get(self._url())
        request.user = self.user
        for bad in ("..", ".", "a/b", "a\\b", "a\x00b"):
            with self.subTest(persona=bad):
                response = views.replay_persona_state(
                    request, self.sim_code, "5", bad
                )
                self.assertEqual(response.status_code, 400)

    def test_safe_persona_name_helper(self):
        self.assertTrue(views._safe_persona_name("Isabella Rodriguez"))
        self.assertTrue(views._safe_persona_name("Isabella_Rodriguez"))
        for bad in ("", ".", "..", "a/b", "a\\b", "a\x00b", "a\x7fb"):
            with self.subTest(name=bad):
                self.assertFalse(views._safe_persona_name(bad))


class CompressedPersonaStateSymlinkTests(TestCase):
    """replay_persona_state compressed fallback is descriptor-relative.

    The compressed_storage root is opened with O_DIRECTORY | O_NOFOLLOW
    and the simulation, personas, persona, and bootstrap_memory ancestors
    are walked with openat, so a symlinked or swapped compressed root or
    intermediate answers 404 and the external victim tree is never opened
    or read. The valid compressed fallback still renders unchanged, and
    no descriptor leaks a ResourceWarning.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client.force_login(self.user)
        self.sim_code = "test_persona_compressed_symlink"
        self.persona_name = "Isabella Rodriguez"
        self.compressed_memory = (
            Path("compressed_storage")
            / self.sim_code
            / "personas"
            / self.persona_name
            / "bootstrap_memory"
        )
        self.externals = []

    def tearDown(self):
        self._restore_compressed_root()
        self._remove_node(Path("compressed_storage") / self.sim_code)
        shutil.rmtree(Path("storage") / self.sim_code, ignore_errors=True)
        for external in self.externals:
            shutil.rmtree(external, ignore_errors=True)

    def _url(self, persona="Isabella_Rodriguez"):
        return f"/replay_persona_state/{self.sim_code}/5/{persona}/"

    def _extern(self) -> Path:
        external = Path(tempfile.mkdtemp())
        self.externals.append(external)
        return external

    @staticmethod
    def _remove_node(path: Path) -> None:
        if path.is_symlink():
            path.unlink()
        else:
            shutil.rmtree(path, ignore_errors=True)

    def _restore_compressed_root(self):
        root = Path("compressed_storage")
        backup = root.with_name(root.name + ".real")
        if root.is_symlink():
            root.unlink()
        if backup.exists():
            os.rename(backup, root)

    def _restore_swap(self, target: Path, backup: Path) -> None:
        if target.is_symlink():
            target.unlink()
        if backup.exists():
            os.rename(backup, target)

    def _write_compressed(self, memory: Path, marker="compressed"):
        _write_valid_bootstrap(memory, marker)

    def _write_external_payload(self, external: Path, marker="external"):
        _write_valid_bootstrap(
            external / self.sim_code / "personas" / self.persona_name / "bootstrap_memory",
            marker,
        )
        (external / "victim.json").write_text(
            json.dumps({"name": "external"}), encoding="utf-8"
        )

    def _recording_open(self, opened):
        real_open = views.os.open

        def recording_open(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        return recording_open

    def _race_open(self, opened, target: Path, symlink_target: Path):
        backup = target.with_name(target.name + ".real")
        state = {"done": False}
        real_open = views.os.open

        def swapping_open(path, *args, **kwargs):
            opened.append(str(path))
            if (
                not state["done"]
                and kwargs.get("dir_fd") is not None
                and os.fspath(path) == target.name
            ):
                state["done"] = True
                os.rename(target, backup)
                target.symlink_to(symlink_target)
            return real_open(path, *args, **kwargs)

        return swapping_open, backup

    def _assert_compressed_refused(self, response, opened, external):
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(str(external), opened)
        self.assertNotIn(str(external / "victim.json"), opened)

    def _assert_marker(self, response, marker):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["scratch"], {"name": marker})
        self.assertEqual(response.context["spatial"], {"spatial": marker})
        self.assertEqual(
            response.context["a_mem_event"][0]["description"], f"event {marker}"
        )
        self.assertEqual(response.context["persona_name"], "Isabella Rodriguez")
        self.assertEqual(response.context["step"], 5)

    def test_compressed_fallback_valid_unchanged(self):
        self._write_compressed(self.compressed_memory)
        self._assert_marker(self.client.get(self._url()), "compressed")

    def test_compressed_root_symlink_404_no_victim_read(self):
        external = self._extern()
        self._write_external_payload(external)
        root = Path("compressed_storage")
        backup = root.with_name(root.name + ".real")
        os.rename(root, backup)
        root.symlink_to(external)
        opened = []
        try:
            with patch(
                "translator.views.os.open", side_effect=self._recording_open(opened)
            ):
                response = self.client.get(self._url())
        finally:
            self._restore_compressed_root()
        self._assert_compressed_refused(response, opened, external)

    def test_compressed_sim_dir_symlink_404_no_victim_read(self):
        external = self._extern()
        self._write_external_payload(external)
        self._write_compressed(self.compressed_memory)
        target = Path("compressed_storage") / self.sim_code
        backup = target.with_name(target.name + ".real")
        os.rename(target, backup)
        target.symlink_to(external / self.sim_code)
        opened = []
        try:
            with patch(
                "translator.views.os.open", side_effect=self._recording_open(opened)
            ):
                response = self.client.get(self._url())
        finally:
            self._restore_swap(target, backup)
        self._assert_compressed_refused(response, opened, external)

    def test_compressed_personas_symlink_404_no_victim_read(self):
        external = self._extern()
        self._write_external_payload(external)
        self._write_compressed(self.compressed_memory)
        target = Path("compressed_storage") / self.sim_code / "personas"
        backup = target.with_name(target.name + ".real")
        os.rename(target, backup)
        target.symlink_to(external / self.sim_code / "personas")
        opened = []
        try:
            with patch(
                "translator.views.os.open", side_effect=self._recording_open(opened)
            ):
                response = self.client.get(self._url())
        finally:
            self._restore_swap(target, backup)
        self._assert_compressed_refused(response, opened, external)

    def test_compressed_persona_symlink_404_no_victim_read(self):
        external = self._extern()
        self._write_external_payload(external)
        self._write_compressed(self.compressed_memory)
        target = (
            Path("compressed_storage")
            / self.sim_code
            / "personas"
            / self.persona_name
        )
        backup = target.with_name(target.name + ".real")
        os.rename(target, backup)
        target.symlink_to(external / self.sim_code / "personas" / self.persona_name)
        opened = []
        try:
            with patch(
                "translator.views.os.open", side_effect=self._recording_open(opened)
            ):
                response = self.client.get(self._url())
        finally:
            self._restore_swap(target, backup)
        self._assert_compressed_refused(response, opened, external)

    def test_compressed_bootstrap_symlink_404_no_victim_read(self):
        external = self._extern()
        self._write_external_payload(external)
        self._write_compressed(self.compressed_memory)
        target = self.compressed_memory
        backup = target.with_name(target.name + ".real")
        os.rename(target, backup)
        target.symlink_to(
            external
            / self.sim_code
            / "personas"
            / self.persona_name
            / "bootstrap_memory"
        )
        opened = []
        try:
            with patch(
                "translator.views.os.open", side_effect=self._recording_open(opened)
            ):
                response = self.client.get(self._url())
        finally:
            self._restore_swap(target, backup)
        self._assert_compressed_refused(response, opened, external)

    def test_compressed_sim_dir_swap_to_symlink_404_no_victim_read(self):
        external = self._extern()
        self._write_external_payload(external)
        self._write_compressed(self.compressed_memory)
        target = Path("compressed_storage") / self.sim_code
        opened = []
        swapping, backup = self._race_open(
            opened, target, external / self.sim_code
        )
        try:
            with patch("translator.views.os.open", side_effect=swapping):
                response = self.client.get(self._url())
        finally:
            self._restore_swap(target, backup)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(str(external), opened)

    def test_compressed_personas_swap_to_symlink_404_no_victim_read(self):
        external = self._extern()
        self._write_external_payload(external)
        self._write_compressed(self.compressed_memory)
        target = Path("compressed_storage") / self.sim_code / "personas"
        opened = []
        swapping, backup = self._race_open(
            opened, target, external / self.sim_code / "personas"
        )
        try:
            with patch("translator.views.os.open", side_effect=swapping):
                response = self.client.get(self._url())
        finally:
            self._restore_swap(target, backup)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(str(external), opened)

    def test_compressed_persona_swap_to_symlink_404_no_victim_read(self):
        external = self._extern()
        self._write_external_payload(external)
        self._write_compressed(self.compressed_memory)
        target = (
            Path("compressed_storage")
            / self.sim_code
            / "personas"
            / self.persona_name
        )
        opened = []
        swapping, backup = self._race_open(
            opened,
            target,
            external / self.sim_code / "personas" / self.persona_name,
        )
        try:
            with patch("translator.views.os.open", side_effect=swapping):
                response = self.client.get(self._url())
        finally:
            self._restore_swap(target, backup)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(str(external), opened)

    def test_compressed_fallback_no_fd_leak(self):
        self._write_compressed(self.compressed_memory)
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            self._assert_marker(self.client.get(self._url()), "compressed")
            gc.collect()
        external = self._extern()
        self._write_external_payload(external)
        target = Path("compressed_storage") / self.sim_code / "personas"
        backup = target.with_name(target.name + ".real")
        os.rename(target, backup)
        target.symlink_to(external / self.sim_code / "personas")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", ResourceWarning)
                response = self.client.get(self._url())
                gc.collect()
        finally:
            self._restore_swap(target, backup)
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()

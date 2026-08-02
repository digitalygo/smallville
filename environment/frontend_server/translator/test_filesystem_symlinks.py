"""Filesystem, symlink, and concurrency tests for the Smallville Django frontend.

Covers symlink ancestor rejection, descriptor traversal swaps, symlinked
environment and lock directories, and atomic write interruption."""

import gc
import json
import os
import shutil
import tempfile
import unittest
import warnings

from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.contrib.auth.models import User

from translator import views
from translator.test_helpers import (
    BackendLeaseMixin,
    _remove_runtime_markers,
    _resolves_under,
    _write_complete_checkpoint,
    _write_runtime_markers,
    _write_valid_bootstrap,
)


class SymlinkAncestorRejectionTests(BackendLeaseMixin, TestCase):
    """Symlinked checkpoint or legacy ancestors fail controlled.

    A complete external checkpoint tree reached only through a symlinked
    sim root, reverie, or checkpoints ancestor is never read and never
    falls back to legacy state: home answers the startup error, replay
    answers 404, and replay_persona_state and process_environment answer
    500. The legacy layout is consulted only when its sim root and
    personas ancestors are real directories. A guarded open records every
    open and refuses any path that resolves under the external tree, so a
    read through a symlink is both refused and detected.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client.force_login(self.user)
        self.sim_code = "test_symlink_ancestor"
        self.sim_dir = Path("storage") / self.sim_code
        self.env_dir = self.sim_dir / "environment"
        self.temp_dir = Path("temp_storage")
        self.compressed_dir = (
            Path("compressed_storage") / self.sim_code
        )
        self.externals = []

    def tearDown(self):
        for name in ("curr_step.json", "curr_sim_code.json"):
            (self.temp_dir / name).unlink(missing_ok=True)
        sim_path = Path("storage") / self.sim_code
        if sim_path.is_symlink():
            sim_path.unlink()
        shutil.rmtree(sim_path, ignore_errors=True)
        shutil.rmtree(self.compressed_dir, ignore_errors=True)
        for external in self.externals:
            shutil.rmtree(external, ignore_errors=True)

    def _extern(self) -> Path:
        external = Path(tempfile.mkdtemp())
        self.externals.append(external)
        return external

    def _positions(self):
        return {
            "Isabella Rodriguez": (1, 1),
            "Maria Lopez": (2, 2),
            "Klaus Mueller": (3, 3),
        }

    def _personas(self):
        return [
            ("Isabella Rodriguez", "external"),
            ("Maria Lopez", "external"),
            ("Klaus Mueller", "external"),
        ]

    def _write_env(self, step, positions, env_dir=None):
        env = {name: {"x": x, "y": y} for name, (x, y) in positions.items()}
        ((env_dir or self.env_dir) / f"{step}.json").write_text(
            json.dumps(env), encoding="utf-8"
        )

    def _external_checkpoint(self, external: Path) -> None:
        _write_complete_checkpoint(external, "0000000001", self._personas())
        env_dir = external / "environment"
        env_dir.mkdir(parents=True, exist_ok=True)
        self._write_env(0, self._positions(), env_dir=env_dir)

    def _external_legacy(self, external: Path) -> None:
        for name, marker in self._personas():
            _write_valid_bootstrap(
                external / "personas" / name / "bootstrap_memory", marker
            )

    def _guarded_opens(self, external: Path):
        opened = []
        real_os_open = views.os.open

        def guarded_os_open(path, *args, **kwargs):
            opened.append(str(path))
            if _resolves_under(str(path), external):
                raise OSError("refused external open")
            return real_os_open(path, *args, **kwargs)

        real_open = open

        def guarded_open(path, *args, **kwargs):
            opened.append(str(path))
            if _resolves_under(str(path), external):
                raise OSError("refused external open")
            return real_open(path, *args, **kwargs)

        return opened, guarded_os_open, guarded_open

    def _home(self):
        _write_runtime_markers(self.sim_code, 0)
        return self.client.get("/simulator_home")

    def _replay(self):
        return self.client.get(f"/replay/{self.sim_code}/0/")

    def _persona_state(self):
        return self.client.get(
            f"/replay_persona_state/{self.sim_code}/5/Isabella_Rodriguez/"
        )

    def _process(self):
        _write_runtime_markers(self.sim_code, 0)
        env = {name: {"x": x, "y": y} for name, (x, y) in self._positions().items()}
        return self.client.post(
            "/process_environment/",
            data=json.dumps(
                {"step": 0, "sim_code": self.sim_code, "environment": env}
            ),
            content_type="application/json",
        )

    def _assert_endpoint_rejected(
        self, response, endpoint, external, opened, exclude=()
    ):
        if endpoint == "home":
            self.assertEqual(response.status_code, 200)
            self.assertTemplateUsed(response, "home/error_start_backend.html")
        elif endpoint == "replay":
            self.assertEqual(response.status_code, 404)
            self.assertIn("Checkpoint", response.json()["error"])
        elif endpoint == "persona_state":
            self.assertEqual(response.status_code, 500)
        elif endpoint == "process":
            self.assertEqual(response.status_code, 500)
        self.assertFalse(
            any(
                _resolves_under(path, external)
                for path in opened
                if path not in exclude
            )
        )

    def test_symlink_sim_root_to_external_checkpoint_fails_controlled(self):
        external = self._extern()
        self._external_checkpoint(external)
        self.sim_dir.symlink_to(external)
        opened, guarded_os_open, guarded_open = self._guarded_opens(external)
        for endpoint in ("home", "replay", "persona_state", "process"):
            with self.subTest(endpoint=endpoint):
                request = {
                    "home": self._home,
                    "replay": self._replay,
                    "persona_state": self._persona_state,
                    "process": self._process,
                }[endpoint]
                with patch(
                    "translator.views.os.open", side_effect=guarded_os_open
                ), patch("translator.views.open", side_effect=guarded_open):
                    response = request()
                self._assert_endpoint_rejected(
                    response, endpoint, external, opened,
                    exclude=(str(self.sim_dir),),
                )

    def test_symlink_reverie_to_external_checkpoint_fails_controlled(self):
        external = self._extern()
        self._external_checkpoint(external)
        self.sim_dir.mkdir(parents=True, exist_ok=True)
        self.env_dir.mkdir(parents=True, exist_ok=True)
        self._write_env(0, self._positions())
        (self.sim_dir / "reverie").symlink_to(external)
        opened, guarded_os_open, guarded_open = self._guarded_opens(external)
        for endpoint in ("home", "replay", "persona_state", "process"):
            with self.subTest(endpoint=endpoint):
                request = {
                    "home": self._home,
                    "replay": self._replay,
                    "persona_state": self._persona_state,
                    "process": self._process,
                }[endpoint]
                with patch(
                    "translator.views.os.open", side_effect=guarded_os_open
                ), patch("translator.views.open", side_effect=guarded_open):
                    response = request()
                self._assert_endpoint_rejected(
                    response, endpoint, external, opened
                )

    def test_symlink_checkpoints_to_external_tree_fails_controlled(self):
        external = self._extern()
        self._external_checkpoint(external)
        self.sim_dir.mkdir(parents=True, exist_ok=True)
        self.env_dir.mkdir(parents=True, exist_ok=True)
        self._write_env(0, self._positions())
        reverie = self.sim_dir / "reverie"
        reverie.mkdir(parents=True, exist_ok=True)
        (reverie / "checkpoints").symlink_to(
            external / "reverie" / "checkpoints"
        )
        opened, guarded_os_open, guarded_open = self._guarded_opens(external)
        for endpoint in ("home", "replay", "persona_state", "process"):
            with self.subTest(endpoint=endpoint):
                request = {
                    "home": self._home,
                    "replay": self._replay,
                    "persona_state": self._persona_state,
                    "process": self._process,
                }[endpoint]
                with patch(
                    "translator.views.os.open", side_effect=guarded_os_open
                ), patch("translator.views.open", side_effect=guarded_open):
                    response = request()
                self._assert_endpoint_rejected(
                    response, endpoint, external, opened
                )

    def test_legacy_symlink_personas_home_startup_error(self):
        external = self._extern()
        self._external_legacy(external)
        self.sim_dir.mkdir(parents=True, exist_ok=True)
        self.env_dir.mkdir(parents=True, exist_ok=True)
        self._write_env(0, self._positions())
        (self.sim_dir / "personas").symlink_to(external / "personas")
        opened, guarded_os_open, guarded_open = self._guarded_opens(external)
        with patch("translator.views.os.open", side_effect=guarded_os_open), patch(
            "translator.views.open", side_effect=guarded_open
        ):
            response = self._home()
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "home/error_start_backend.html")
        self.assertFalse(any(_resolves_under(path, external) for path in opened))

    def test_legacy_symlink_personas_replay_404(self):
        external = self._extern()
        self._external_legacy(external)
        self.sim_dir.mkdir(parents=True, exist_ok=True)
        self.env_dir.mkdir(parents=True, exist_ok=True)
        self._write_env(0, self._positions())
        (self.sim_dir / "personas").symlink_to(external / "personas")
        opened, guarded_os_open, guarded_open = self._guarded_opens(external)
        with patch("translator.views.os.open", side_effect=guarded_os_open), patch(
            "translator.views.open", side_effect=guarded_open
        ):
            response = self._replay()
        self.assertEqual(response.status_code, 404)
        self.assertFalse(any(_resolves_under(path, external) for path in opened))

    def test_legacy_symlink_personas_state_404_no_compressed_fallback(self):
        external = self._extern()
        self._external_legacy(external)
        self.sim_dir.mkdir(parents=True, exist_ok=True)
        (self.sim_dir / "personas").symlink_to(external / "personas")
        for name, marker in self._personas():
            _write_valid_bootstrap(
                self.compressed_dir / "personas" / name / "bootstrap_memory",
                "compressed",
            )
        opened, guarded_os_open, guarded_open = self._guarded_opens(external)
        with patch("translator.views.os.open", side_effect=guarded_os_open), patch(
            "translator.views.open", side_effect=guarded_open
        ):
            response = self._persona_state()
        self.assertEqual(response.status_code, 404)
        self.assertFalse(any(_resolves_under(path, external) for path in opened))

    def test_legacy_symlink_personas_process_500(self):
        external = self._extern()
        self._external_legacy(external)
        self.sim_dir.mkdir(parents=True, exist_ok=True)
        (self.sim_dir / "personas").symlink_to(external / "personas")
        opened, guarded_os_open, guarded_open = self._guarded_opens(external)
        with patch("translator.views.os.open", side_effect=guarded_os_open), patch(
            "translator.views.open", side_effect=guarded_open
        ):
            response = self._process()
        self.assertEqual(response.status_code, 500)
        self.assertFalse(any(_resolves_under(path, external) for path in opened))


class DescriptorTraversalSwapTests(BackendLeaseMixin, TestCase):
    """A checkpoint ancestor swapped to a symlink between walk and read fails.

    The descriptor walk opens each component with O_DIRECTORY | O_NOFOLLOW
    relative to the parent descriptor, so a parent swapped to a symlink is
    refused at the single openat syscall instead of being followed to the
    external tree. Every scenario swaps the ancestor immediately before its
    first open and asserts the endpoint answers its controlled failure, the
    external victim tree is never opened or read, and no descriptor-backed
    file object leaks a ResourceWarning.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client.force_login(self.user)
        self.sim_code = "test_descriptor_swap"
        self.sim_dir = Path("storage") / self.sim_code
        self.reverie = self.sim_dir / "reverie"
        self.checkpoints = self.reverie / "checkpoints"
        self.gen_dir = self.checkpoints / "0000000001"
        self.env_dir = self.sim_dir / "environment"
        self.temp_dir = Path("temp_storage")
        self.externals = []
        self.env_dir.mkdir(parents=True, exist_ok=True)
        self._write_env(0)
        _write_complete_checkpoint(
            self.sim_dir,
            "0000000001",
            [
                ("Isabella Rodriguez", "checkpoint"),
                ("Maria Lopez", "external"),
                ("Klaus Mueller", "external"),
            ],
        )

    def tearDown(self):
        for name in ("curr_step.json", "curr_sim_code.json"):
            (self.temp_dir / name).unlink(missing_ok=True)
        for target in (
            self.reverie,
            self.checkpoints,
            self.gen_dir,
            self.gen_dir / "personas" / "Isabella Rodriguez",
        ):
            self._restore_swap(target)
        shutil.rmtree(self.sim_dir, ignore_errors=True)
        for external in self.externals:
            shutil.rmtree(external, ignore_errors=True)

    def _positions(self):
        return {
            "Isabella Rodriguez": (1, 1),
            "Maria Lopez": (2, 2),
            "Klaus Mueller": (3, 3),
        }

    def _write_env(self, step):
        env = {name: {"x": x, "y": y} for name, (x, y) in self._positions().items()}
        (self.env_dir / f"{step}.json").write_text(json.dumps(env), encoding="utf-8")

    def _extern(self) -> Path:
        external = Path(tempfile.mkdtemp())
        self.externals.append(external)
        return external

    def _external_personas(self):
        return [
            ("Isabella Rodriguez", "external"),
            ("Maria Lopez", "external"),
            ("Klaus Mueller", "external"),
        ]

    def _restore_swap(self, target: Path) -> None:
        backup = target.with_name(target.name + ".real")
        if target.is_symlink():
            target.unlink()
        if backup.exists():
            os.rename(backup, target)

    def _home(self):
        _write_runtime_markers(self.sim_code, 0)
        return self.client.get("/simulator_home")

    def _replay(self):
        return self.client.get(f"/replay/{self.sim_code}/0/")

    def _persona_state(self):
        return self.client.get(
            f"/replay_persona_state/{self.sim_code}/5/Isabella_Rodriguez/"
        )

    def _process(self):
        _write_runtime_markers(self.sim_code, 0)
        env = {name: {"x": x, "y": y} for name, (x, y) in self._positions().items()}
        return self.client.post(
            "/process_environment/",
            data=json.dumps(
                {"step": 0, "sim_code": self.sim_code, "environment": env}
            ),
            content_type="application/json",
        )

    def _assert_swap_rejected(
        self, response, endpoint, swap_point, external, opened, victim, victim_after
    ):
        if endpoint == "home":
            self.assertEqual(response.status_code, 200)
            self.assertTemplateUsed(response, "home/error_start_backend.html")
        elif endpoint == "replay":
            self.assertEqual(response.status_code, 404)
            self.assertIn("Checkpoint", response.json()["error"])
        elif endpoint == "persona_state":
            if swap_point in ("reverie", "checkpoints"):
                self.assertEqual(response.status_code, 500)
            else:
                self.assertEqual(response.status_code, 404)
        elif endpoint == "process":
            self.assertEqual(response.status_code, 500)
        self.assertEqual(victim_after, json.dumps({"name": "external"}))
        self.assertNotIn(str(victim), opened)
        self.assertFalse(any(_resolves_under(path, external) for path in opened))

    def test_ancestor_swap_to_symlink_between_check_and_read_fails_controlled(self):
        external = self._extern()
        _write_complete_checkpoint(external, "0000000001", self._external_personas())
        victim = external / "victim.json"
        victim_content = json.dumps({"name": "external"})
        victim.write_text(victim_content, encoding="utf-8")
        swaps = {
            "reverie": (self.reverie, external / "reverie"),
            "checkpoints": (self.checkpoints, external / "reverie" / "checkpoints"),
            "generation": (
                self.gen_dir,
                external / "reverie" / "checkpoints" / "0000000001",
            ),
            "persona": (
                self.gen_dir / "personas" / "Isabella Rodriguez",
                external
                / "reverie"
                / "checkpoints"
                / "0000000001"
                / "personas"
                / "Isabella Rodriguez",
            ),
        }
        endpoints = {
            "home": self._home,
            "replay": self._replay,
            "persona_state": self._persona_state,
            "process": self._process,
        }
        for swap_point, (target, symlink_target) in swaps.items():
            for endpoint, request in endpoints.items():
                with self.subTest(swap=swap_point, endpoint=endpoint):
                    opened = []
                    real_open = views.os.open
                    backup = target.with_name(target.name + ".real")
                    state = {"done": False}

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

                    with patch("translator.views.os.open", side_effect=swapping_open):
                        response = request()
                    victim_after = victim.read_text(encoding="utf-8")
                    self._assert_swap_rejected(
                        response,
                        endpoint,
                        swap_point,
                        external,
                        opened,
                        victim,
                        victim_after,
                    )
                    self._restore_swap(target)
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            gc.collect()


class EnvironmentDirectorySymlinkTests(BackendLeaseMixin, TestCase):
    """Symlinked or swapped environment and movement directories fail closed.

    home answers the startup error, replay answers 404, and
    process_environment, update_environment, and replay_movement answer
    500. The external victim tree is never opened, read, written, or
    deleted, and no descriptor leaks a ResourceWarning.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client.force_login(self.user)
        self.sim_code = "test_env_dir_symlink"
        self.sim_dir = Path("storage") / self.sim_code
        self.env_dir = self.sim_dir / "environment"
        self.movement_dir = self.sim_dir / "movement"
        self.personas_dir = self.sim_dir / "personas"
        for name in ("Isabella Rodriguez", "Maria Lopez", "Klaus Mueller"):
            (self.personas_dir / name).mkdir(parents=True, exist_ok=True)
        self.externals = []

    def tearDown(self):
        _remove_runtime_markers()
        for target in (self.env_dir, self.movement_dir):
            self._restore(target)
        sim_path = Path("storage") / self.sim_code
        if sim_path.is_symlink():
            sim_path.unlink()
        shutil.rmtree(sim_path, ignore_errors=True)
        for external in self.externals:
            shutil.rmtree(external, ignore_errors=True)

    def _positions(self):
        return {
            "Isabella Rodriguez": (1, 1),
            "Maria Lopez": (2, 2),
            "Klaus Mueller": (3, 3),
        }

    def _positions_env(self):
        return {name: {"x": x, "y": y} for name, (x, y) in self._positions().items()}

    def _movement_payload(self):
        return {
            "persona": {name: {"movement": [1, 2]} for name in self._positions()},
            "meta": {"curr_time": "March 01, 2023, 00:00:00"},
        }

    def _write_env(self, step, positions, env_dir=None):
        env = {name: {"x": x, "y": y} for name, (x, y) in positions.items()}
        ((env_dir or self.env_dir) / f"{step}.json").parent.mkdir(
            parents=True, exist_ok=True
        )
        ((env_dir or self.env_dir) / f"{step}.json").write_text(
            json.dumps(env), encoding="utf-8"
        )

    def _write_movement(self, step, movement_dir=None):
        ((movement_dir or self.movement_dir) / f"{step}.json").parent.mkdir(
            parents=True, exist_ok=True
        )
        ((movement_dir or self.movement_dir) / f"{step}.json").write_text(
            json.dumps(self._movement_payload()), encoding="utf-8"
        )

    def _extern(self) -> Path:
        external = Path(tempfile.mkdtemp())
        self.externals.append(external)
        return external

    def _external_env(self, external: Path) -> None:
        env_dir = external / "environment"
        env_dir.mkdir(parents=True, exist_ok=True)
        self._write_env(0, self._positions(), env_dir=env_dir)

    def _external_movement(self, external: Path) -> None:
        movement_dir = external / "movement"
        movement_dir.mkdir(parents=True, exist_ok=True)
        self._write_movement(0, movement_dir=movement_dir)

    def _restore(self, target: Path) -> None:
        backup = target.with_name(target.name + ".real")
        if target.is_symlink():
            target.unlink()
        if backup.exists():
            os.rename(backup, target)

    def _guarded_opens(self, external: Path):
        opened = []
        real_os_open = views.os.open

        def guarded_os_open(path, *args, **kwargs):
            opened.append(str(path))
            if _resolves_under(str(path), external):
                raise OSError("refused external open")
            return real_os_open(path, *args, **kwargs)

        real_open = open

        def guarded_open(path, *args, **kwargs):
            opened.append(str(path))
            if _resolves_under(str(path), external):
                raise OSError("refused external open")
            return real_open(path, *args, **kwargs)

        return opened, guarded_os_open, guarded_open

    def _assert_no_fd_leaks(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            gc.collect()

    def _home(self):
        _write_runtime_markers(self.sim_code, 0)
        return self.client.get("/simulator_home")

    def _replay(self):
        return self.client.get(f"/replay/{self.sim_code}/0/")

    def _process(self, step=0):
        _write_runtime_markers(self.sim_code, step)
        return self.client.post(
            "/process_environment/",
            data=json.dumps(
                {
                    "step": step,
                    "sim_code": self.sim_code,
                    "environment": self._positions_env(),
                }
            ),
            content_type="application/json",
        )

    def _update(self, step=0):
        _write_runtime_markers(self.sim_code, step + 1)
        return self.client.post(
            "/update_environment/",
            data=json.dumps({"step": step, "sim_code": self.sim_code}),
            content_type="application/json",
        )

    def _replay_movement(self, step=0):
        return self.client.post(
            "/replay_movement/",
            data=json.dumps({"step": step, "sim_code": self.sim_code}),
            content_type="application/json",
        )

    def test_environment_dir_symlink_home_startup_error(self):
        external = self._extern()
        self._external_env(external)
        self._write_env(0, self._positions())
        self.env_dir.rename(self.sim_dir / "environment.real")
        self.env_dir.symlink_to(external / "environment")
        opened, guarded_os_open, guarded_open = self._guarded_opens(external)
        try:
            with patch("translator.views.os.open", side_effect=guarded_os_open), patch(
                "translator.views.open", side_effect=guarded_open
            ):
                response = self._home()
        finally:
            self._restore(self.env_dir)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "home/error_start_backend.html")
        self.assertFalse(any(_resolves_under(path, external) for path in opened))
        self.assertEqual(
            (external / "environment" / "0.json").read_text(encoding="utf-8"),
            json.dumps(self._positions_env()),
        )
        self._assert_no_fd_leaks()

    def test_environment_dir_symlink_replay_404(self):
        external = self._extern()
        self._external_env(external)
        self._write_env(0, self._positions())
        self.env_dir.rename(self.sim_dir / "environment.real")
        self.env_dir.symlink_to(external / "environment")
        opened, guarded_os_open, guarded_open = self._guarded_opens(external)
        try:
            with patch("translator.views.os.open", side_effect=guarded_os_open), patch(
                "translator.views.open", side_effect=guarded_open
            ):
                response = self._replay()
        finally:
            self._restore(self.env_dir)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(any(_resolves_under(path, external) for path in opened))
        self.assertEqual(
            (external / "environment" / "0.json").read_text(encoding="utf-8"),
            json.dumps(self._positions_env()),
        )
        self._assert_no_fd_leaks()

    def test_environment_dir_symlink_process_500(self):
        external = self._extern()
        self._external_env(external)
        self._write_env(0, self._positions())
        self.env_dir.rename(self.sim_dir / "environment.real")
        self.env_dir.symlink_to(external / "environment")
        opened, guarded_os_open, guarded_open = self._guarded_opens(external)
        try:
            with patch("translator.views.os.open", side_effect=guarded_os_open), patch(
                "translator.views.open", side_effect=guarded_open
            ):
                response = self._process()
        finally:
            self._restore(self.env_dir)
        self.assertEqual(response.status_code, 500)
        self.assertFalse(any(_resolves_under(path, external) for path in opened))
        self.assertEqual(
            (external / "environment" / "0.json").read_text(encoding="utf-8"),
            json.dumps(self._positions_env()),
        )
        self.assertEqual(
            [p.name for p in (external / "environment").iterdir()], ["0.json"]
        )
        self._assert_no_fd_leaks()

    def test_movement_dir_symlink_update_500(self):
        external = self._extern()
        self._external_movement(external)
        self._write_movement(0)
        self.movement_dir.rename(self.sim_dir / "movement.real")
        self.movement_dir.symlink_to(external / "movement")
        opened, guarded_os_open, guarded_open = self._guarded_opens(external)
        try:
            with patch("translator.views.os.open", side_effect=guarded_os_open), patch(
                "translator.views.open", side_effect=guarded_open
            ):
                response = self._update()
        finally:
            self._restore(self.movement_dir)
        self.assertEqual(response.status_code, 500)
        self.assertFalse(any(_resolves_under(path, external) for path in opened))
        self.assertEqual(
            (external / "movement" / "0.json").read_text(encoding="utf-8"),
            json.dumps(self._movement_payload()),
        )
        self._assert_no_fd_leaks()

    def test_movement_dir_symlink_replay_movement_500(self):
        external = self._extern()
        self._external_movement(external)
        self._write_movement(0)
        self.movement_dir.rename(self.sim_dir / "movement.real")
        self.movement_dir.symlink_to(external / "movement")
        opened, guarded_os_open, guarded_open = self._guarded_opens(external)
        try:
            with patch("translator.views.os.open", side_effect=guarded_os_open), patch(
                "translator.views.open", side_effect=guarded_open
            ):
                response = self._replay_movement()
        finally:
            self._restore(self.movement_dir)
        self.assertEqual(response.status_code, 500)
        self.assertFalse(any(_resolves_under(path, external) for path in opened))
        self.assertEqual(
            (external / "movement" / "0.json").read_text(encoding="utf-8"),
            json.dumps(self._movement_payload()),
        )
        self._assert_no_fd_leaks()

    def test_environment_dir_swap_to_symlink_fails_controlled(self):
        external = self._extern()
        self._external_env(external)
        self._write_env(0, self._positions())
        target = self.sim_dir / "environment"
        backup = target.with_name(target.name + ".real")
        state = {"done": False}
        opened = []
        real_open = views.os.open

        def swapping_open(path, *args, **kwargs):
            opened.append(str(path))
            if (
                not state["done"]
                and kwargs.get("dir_fd") is not None
                and os.fspath(path) == "environment"
            ):
                state["done"] = True
                os.rename(target, backup)
                target.symlink_to(external / "environment")
            return real_open(path, *args, **kwargs)

        endpoints = {
            "home": self._home,
            "replay": self._replay,
            "process": self._process,
        }
        try:
            with patch("translator.views.os.open", side_effect=swapping_open):
                for endpoint, request in endpoints.items():
                    with self.subTest(endpoint=endpoint):
                        response = request()
                        if endpoint == "home":
                            self.assertEqual(response.status_code, 200)
                            self.assertTemplateUsed(
                                response, "home/error_start_backend.html"
                            )
                        elif endpoint == "replay":
                            self.assertEqual(response.status_code, 404)
                        else:
                            self.assertEqual(response.status_code, 500)
            victim_after = (external / "environment" / "0.json").read_text(
                encoding="utf-8"
            )
        finally:
            self._restore(target)
        self.assertEqual(victim_after, json.dumps(self._positions_env()))
        self.assertNotIn(str(external), opened)
        self._assert_no_fd_leaks()

    def test_movement_dir_swap_to_symlink_fails_controlled(self):
        external = self._extern()
        self._external_movement(external)
        self._write_movement(0)
        target = self.sim_dir / "movement"
        backup = target.with_name(target.name + ".real")
        state = {"done": False}
        opened = []
        real_open = views.os.open

        def swapping_open(path, *args, **kwargs):
            opened.append(str(path))
            if (
                not state["done"]
                and kwargs.get("dir_fd") is not None
                and os.fspath(path) == "movement"
            ):
                state["done"] = True
                os.rename(target, backup)
                target.symlink_to(external / "movement")
            return real_open(path, *args, **kwargs)

        try:
            with patch("translator.views.os.open", side_effect=swapping_open):
                update_response = self._update()
                movement_response = self._replay_movement()
            victim_after = (external / "movement" / "0.json").read_text(
                encoding="utf-8"
            )
        finally:
            self._restore(target)
        self.assertEqual(update_response.status_code, 500)
        self.assertEqual(movement_response.status_code, 500)
        self.assertEqual(victim_after, json.dumps(self._movement_payload()))
        self.assertNotIn(str(external), opened)
        self._assert_no_fd_leaks()


class LockAncestorSymlinkTests(BackendLeaseMixin, TestCase):
    """Symlinked lock roots, directories, and leaves fail controlled.

    A symlinked temp_storage root, runtime.lock leaf, step lock leaf, or
    environment_locks directory never grants a lock outside the intended
    layout: home answers the startup error, process_environment and
    update_environment answer 500, the victim tree is never written, and
    no descriptor leaks a ResourceWarning.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client.force_login(self.user)
        self.sim_code = "test_lock_symlink"
        self.sim_dir = Path("storage") / self.sim_code
        self.env_dir = self.sim_dir / "environment"
        self.personas_dir = self.sim_dir / "personas"
        for name in ("Isabella Rodriguez", "Maria Lopez", "Klaus Mueller"):
            (self.personas_dir / name).mkdir(parents=True, exist_ok=True)
        self.externals = []

    def tearDown(self):
        self._restore_environment_locks()
        self._restore_runtime_leaf()
        self._restore_temp_storage()
        _remove_runtime_markers()
        sim_path = Path("storage") / self.sim_code
        if sim_path.is_symlink():
            sim_path.unlink()
        shutil.rmtree(sim_path, ignore_errors=True)
        for external in self.externals:
            shutil.rmtree(external, ignore_errors=True)

    def _extern(self) -> Path:
        external = Path(tempfile.mkdtemp())
        self.externals.append(external)
        return external

    def _positions(self):
        return {
            "Isabella Rodriguez": (1, 1),
            "Maria Lopez": (2, 2),
            "Klaus Mueller": (3, 3),
        }

    def _positions_env(self):
        return {name: {"x": x, "y": y} for name, (x, y) in self._positions().items()}

    def _restore_environment_locks(self):
        target = self.sim_dir / "environment_locks"
        backup = target.with_name(target.name + ".real")
        if target.is_symlink():
            target.unlink()
        if backup.exists():
            os.rename(backup, target)

    def _restore_runtime_leaf(self):
        leaf = Path("temp_storage") / "runtime.lock"
        backup = leaf.with_name(leaf.name + ".real")
        if leaf.is_symlink():
            leaf.unlink()
        if backup.exists():
            os.rename(backup, leaf)

    def _restore_temp_storage(self):
        root = Path("temp_storage")
        backup = root.with_name(root.name + ".real")
        if root.is_symlink():
            root.unlink()
        if backup.exists():
            os.rename(backup, root)

    def _assert_no_fd_leaks(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            gc.collect()

    def test_environment_locks_dir_symlink_process_500(self):
        external = self._extern()
        locks = self.sim_dir / "environment_locks"
        locks.mkdir(parents=True, exist_ok=True)
        os.rename(locks, locks.with_name("environment_locks.real"))
        locks.symlink_to(external)
        try:
            response = self._process()
        finally:
            self._restore_environment_locks()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(list(external.iterdir()), [])
        self.assertFalse((self.env_dir / "0.json").exists())
        self._assert_no_fd_leaks()

    def test_step_lock_leaf_symlink_process_500(self):
        external = self._extern()
        victim = external / "victim.lock"
        victim.write_text("victim", encoding="utf-8")
        locks = self.sim_dir / "environment_locks"
        locks.mkdir(parents=True, exist_ok=True)
        leaf = locks / "0.lock"
        leaf.touch()
        backup = locks / "0.lock.real"
        os.rename(leaf, backup)
        leaf.symlink_to(victim)
        try:
            response = self._process()
        finally:
            if leaf.is_symlink():
                leaf.unlink()
            if backup.exists():
                os.rename(backup, leaf)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(victim.read_text(encoding="utf-8"), "victim")
        self.assertFalse((self.env_dir / "0.json").exists())
        self._assert_no_fd_leaks()

    def test_temp_storage_root_symlink_controlled(self):
        external = self._extern()
        _write_runtime_markers(self.sim_code, 0)
        root = Path("temp_storage")
        backup = root.with_name(root.name + ".real")
        os.rename(root, backup)
        root.symlink_to(external)
        try:
            home_response = self.client.get("/simulator_home")
            process_response = self.client.post(
                "/process_environment/",
                data=json.dumps(
                    {
                        "step": 0,
                        "sim_code": self.sim_code,
                        "environment": self._positions_env(),
                    }
                ),
                content_type="application/json",
            )
            update_response = self.client.post(
                "/update_environment/",
                data=json.dumps({"step": 0, "sim_code": self.sim_code}),
                content_type="application/json",
            )
        finally:
            self._restore_temp_storage()
        self.assertEqual(home_response.status_code, 200)
        self.assertTemplateUsed(home_response, "home/error_start_backend.html")
        self.assertEqual(process_response.status_code, 500)
        self.assertEqual(update_response.status_code, 500)
        self.assertEqual(list(external.iterdir()), [])
        self.assertFalse((self.env_dir / "0.json").exists())
        self._assert_no_fd_leaks()

    def test_runtime_lock_leaf_symlink_controlled(self):
        external = self._extern()
        victim = external / "victim.lock"
        victim.write_text("victim", encoding="utf-8")
        root = Path("temp_storage")
        root.mkdir(parents=True, exist_ok=True)
        leaf = root / "runtime.lock"
        leaf.touch()
        backup = root / "runtime.lock.real"
        if backup.exists():
            backup.unlink()
        os.rename(leaf, backup)
        leaf.symlink_to(victim)
        try:
            home_response = self._home()
            process_response = self._process()
            update_response = self._update()
        finally:
            self._restore_runtime_leaf()
        self.assertEqual(home_response.status_code, 200)
        self.assertTemplateUsed(home_response, "home/error_start_backend.html")
        self.assertEqual(process_response.status_code, 500)
        self.assertEqual(update_response.status_code, 500)
        self.assertEqual(victim.read_text(encoding="utf-8"), "victim")
        self.assertFalse((self.env_dir / "0.json").exists())
        self._assert_no_fd_leaks()

    def _home(self):
        _write_runtime_markers(self.sim_code, 0)
        return self.client.get("/simulator_home")

    def _process(self, step=0):
        _write_runtime_markers(self.sim_code, step)
        return self.client.post(
            "/process_environment/",
            data=json.dumps(
                {
                    "step": step,
                    "sim_code": self.sim_code,
                    "environment": self._positions_env(),
                }
            ),
            content_type="application/json",
        )

    def _update(self, step=0):
        _write_runtime_markers(self.sim_code, step + 1)
        return self.client.post(
            "/update_environment/",
            data=json.dumps({"step": step, "sim_code": self.sim_code}),
            content_type="application/json",
        )


class CompressedDemoSymlinkTests(TestCase):
    """Public demo compressed_storage reads are descriptor-relative.

    The compressed_storage root and the simulation directory are opened
    with O_DIRECTORY | O_NOFOLLOW and each JSON file is read relative to
    the final descriptor, so a symlinked or swapped root or simulation
    directory fails controlled and the external victim files are never
    opened or read.
    """

    def setUp(self):
        self.sim_code = "test_demo_symlink"
        self.compressed_dir = Path("compressed_storage") / self.sim_code
        self.externals = []

    def tearDown(self):
        self._restore_compressed_root()
        sim_path = Path("compressed_storage") / self.sim_code
        if sim_path.is_symlink():
            sim_path.unlink()
        shutil.rmtree(sim_path, ignore_errors=True)
        for external in self.externals:
            shutil.rmtree(external, ignore_errors=True)

    def _extern(self) -> Path:
        external = Path(tempfile.mkdtemp())
        self.externals.append(external)
        return external

    def _restore_compressed_root(self):
        root = Path("compressed_storage")
        backup = root.with_name(root.name + ".real")
        if root.is_symlink():
            root.unlink()
        if backup.exists():
            os.rename(backup, root)

    def _write_demo_payload(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "meta.json").write_text(
            json.dumps({"sec_per_step": 10, "start_date": "March 01, 2023"}),
            encoding="utf-8",
        )
        (directory / "master_movement.json").write_text(
            json.dumps({"0": {"Isabella Rodriguez": {"movement": [1, 2]}}}),
            encoding="utf-8",
        )

    def _recording_open(self, opened):
        real_open = views.os.open

        def recording_open(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        return recording_open

    def test_demo_compressed_root_symlink_404_no_victim_read(self):
        external = self._extern()
        self._write_demo_payload(external / self.sim_code)
        root = Path("compressed_storage")
        backup = root.with_name(root.name + ".real")
        os.rename(root, backup)
        root.symlink_to(external)
        opened = []
        try:
            with patch(
                "translator.views.os.open", side_effect=self._recording_open(opened)
            ):
                response = self.client.get(f"/demo/{self.sim_code}/1/2/")
        finally:
            self._restore_compressed_root()
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(str(external / self.sim_code / "meta.json"), opened)
        self.assertNotIn(
            str(external / self.sim_code / "master_movement.json"), opened
        )

    def test_demo_compressed_sim_dir_swap_to_symlink_404_no_victim_read(self):
        external = self._extern()
        self._write_demo_payload(external / self.sim_code)
        self._write_demo_payload(self.compressed_dir)
        target = Path("compressed_storage") / self.sim_code
        backup = target.with_name(target.name + ".real")
        state = {"done": False}
        opened = []
        real_open = views.os.open

        def swapping_open(path, *args, **kwargs):
            opened.append(str(path))
            if (
                not state["done"]
                and kwargs.get("dir_fd") is not None
                and os.fspath(path) == self.sim_code
            ):
                state["done"] = True
                os.rename(target, backup)
                target.symlink_to(external / self.sim_code)
            return real_open(path, *args, **kwargs)

        try:
            with patch("translator.views.os.open", side_effect=swapping_open):
                response = self.client.get(f"/demo/{self.sim_code}/1/2/")
        finally:
            if target.is_symlink():
                target.unlink()
            if backup.exists():
                os.rename(backup, target)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(str(external / self.sim_code), opened)


class AtomicWriteInterruptionTests(BackendLeaseMixin, TestCase):
    """Interrupted descriptor-relative writes preserve prior snapshots."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client.force_login(self.user)
        self.sim_code = "test_atomic_interrupt"
        self.sim_dir = Path("storage") / self.sim_code
        self.env_dir = self.sim_dir / "environment"
        self.personas_dir = self.sim_dir / "personas"
        for name in ("Isabella Rodriguez", "Maria Lopez", "Klaus Mueller"):
            (self.personas_dir / name).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(Path("storage") / self.sim_code, ignore_errors=True)
        _remove_runtime_markers()

    def _post(self, environment, step):
        return self.client.post(
            "/process_environment/",
            data=json.dumps(
                {
                    "step": step,
                    "sim_code": self.sim_code,
                    "environment": environment,
                }
            ),
            content_type="application/json",
        )

    def _valid_env(self):
        return {
            "Isabella Rodriguez": {"x": 1, "y": 1},
            "Maria Lopez": {"x": 2, "y": 2},
            "Klaus Mueller": {"x": 3, "y": 3},
        }

    def test_interrupted_replace_preserves_prior_snapshot(self):
        _write_runtime_markers(self.sim_code, 1)
        response = self._post(self._valid_env(), 1)
        self.assertEqual(response.status_code, 200)
        prior = self.env_dir / "1.json"
        prior_inode = prior.stat().st_ino
        prior_content = prior.read_text(encoding="utf-8")
        _write_runtime_markers(self.sim_code, 2)
        with patch("translator.views.os.replace", side_effect=OSError("disk full")):
            response = self._post(self._valid_env(), 2)
        self.assertEqual(response.status_code, 500)
        self.assertFalse((self.env_dir / "2.json").exists())
        self.assertEqual(prior.stat().st_ino, prior_inode)
        self.assertEqual(prior.read_text(encoding="utf-8"), prior_content)
        leftovers = [p.name for p in self.env_dir.iterdir() if p.name != "1.json"]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()

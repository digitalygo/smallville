"""Shared helpers for the translator test modules.

No TestCase classes live here; behavior tests import from this module.
"""

import fcntl
import importlib
import json
import os
import select
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch


_BACKEND_LEASE_PATH = Path("temp_storage") / "backend_alive.lock"


_HOLDER_SCRIPT = """
import fcntl
import os
import signal
import sys
import time

try:
    import ctypes
    libc = ctypes.CDLL(None)
    libc.prctl(1, signal.SIGKILL)
except (OSError, AttributeError):
    pass
if os.getppid() == 1:
    sys.exit(1)

path = sys.argv[1]
flags = os.O_CREAT | os.O_RDWR
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
fd = os.open(path, flags)
fcntl.flock(fd, fcntl.LOCK_EX)
print("READY", flush=True)
while True:
    time.sleep(60)
"""


_STRONG_KEY = "h1&@=t(_%$9fx$go4j8t=66ma1ph&g5np8ua3ti@hpkio!t$o#"


_HEX_KEY_64 = "b8b51b9186d720b42f3982bedc4efd885fe5ccefce203fce403d3333286e30d5"


_PRODUCTION_DEFAULTS = {
    "DJANGO_DEBUG": "false",
    "DATABASE_URL": "postgres://smallville:secret@localhost:5432/smallville",
}


_MAX_SAFE_STEP = 9_007_199_254_740_991


def _load_production_settings(env_overrides: dict) -> types.ModuleType:
    """Import production settings fresh with the given env overrides.

    Reloads both base and production modules so module-level reads of
    os.environ pick up the overrides.  The import itself must happen
    inside the patch: production.py raises ImproperlyConfigured at import
    time when the key is weak, and that check must see the overrides.
    """
    env = {**_PRODUCTION_DEFAULTS, **env_overrides}
    with patch.dict(os.environ, env, clear=False):
        base_mod = importlib.import_module("frontend_server.settings.base")
        base_mod = importlib.reload(base_mod)
        prod_mod = importlib.import_module("frontend_server.settings.production")
        prod_mod = importlib.reload(prod_mod)
    return prod_mod


def _load_settings_package(env_overrides: dict) -> types.ModuleType:
    """Reload the settings package so DJANGO_ENV selection re-runs."""
    with patch.dict(os.environ, env_overrides, clear=False):
        pkg = importlib.import_module("frontend_server.settings")
        return importlib.reload(pkg)


def _write_runtime_markers(sim_code: str, step: int) -> None:
    """Publish the runtime markers the backend would write."""
    temp_dir = Path("temp_storage")
    temp_dir.mkdir(parents=True, exist_ok=True)
    (temp_dir / "curr_sim_code.json").write_text(
        json.dumps({"sim_code": sim_code}), encoding="utf-8"
    )
    (temp_dir / "curr_step.json").write_text(
        json.dumps({"step": step}), encoding="utf-8"
    )


def _remove_runtime_markers() -> None:
    """Remove the runtime markers written by tests."""
    temp_dir = Path("temp_storage")
    for name in ("curr_step.json", "curr_sim_code.json"):
        (temp_dir / name).unlink(missing_ok=True)


def _start_lease_holder(lock_path: Path) -> subprocess.Popen:
    """Spawn a real subprocess that holds the backend lease exclusively.

    The child dies with its parent (PDEATHSIG) so a killed test run never
    orphans a holder. READY must arrive within a bounded wait, otherwise
    the child is killed and the failure is reported instead of hanging.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT, str(lock_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    ready = []
    try:
        ready, _, _ = select.select([proc.stdout], [], [], 15)
    except (ValueError, OSError):
        pass
    if not ready:
        _kill_holder(proc)
        stderr = proc.stderr.read() if proc.stderr is not None else ""
        raise AssertionError(
            f"lease holder did not acquire the lock in time: {stderr!r}"
        )
    line = proc.stdout.readline()
    if line.strip() != "READY":
        _kill_holder(proc)
        stderr = proc.stderr.read() if proc.stderr is not None else ""
        raise AssertionError(f"lease holder failed to start: {line!r} {stderr!r}")
    return proc


def _kill_holder(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=30)


def _acquire_backend_lease() -> int:
    """Hold the backend lease in-process; close the returned fd to release."""
    _BACKEND_LEASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(_BACKEND_LEASE_PATH, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _write_valid_bootstrap(memory: Path, marker: str) -> None:
    """Write a persona bootstrap memory tree passing complete validation.

    Every JSON file has the shape the frontend validates: scratch and
    spatial objects, embeddings and kw_strength objects, and a single
    contiguous event node with the fields the persona_state template
    renders.
    """
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "scratch.json").write_text(
        json.dumps({"name": marker}), encoding="utf-8"
    )
    (memory / "spatial_memory.json").write_text(
        json.dumps({"spatial": marker}), encoding="utf-8"
    )
    assoc = memory / "associative_memory"
    assoc.mkdir(parents=True, exist_ok=True)
    (assoc / "nodes.json").write_text(
        json.dumps(
            {
                "node_1": {
                    "type": "event",
                    "node_count": 1,
                    "type_count": 1,
                    "created": "2023-01-01 00:00:00",
                    "expiration": None,
                    "description": f"event {marker}",
                    "subject": "subject",
                    "predicate": "predicate",
                    "object": "object",
                    "keywords": ["keyword"],
                    "poignancy": 5,
                    "embedding_key": "e1",
                    "depth": 0,
                    "filling": [],
                }
            }
        ),
        encoding="utf-8",
    )
    (assoc / "embeddings.json").write_text(
        json.dumps({"e1": [0.1]}), encoding="utf-8"
    )
    (assoc / "kw_strength.json").write_text(
        json.dumps({"kw_strength_event": {}, "kw_strength_thought": {}}),
        encoding="utf-8",
    )


def _write_complete_checkpoint(
    sim_dir: Path,
    generation: str,
    personas,
    meta: dict | None = None,
    marker: str = "checkpoint",
) -> None:
    """Write a pointer plus a complete, valid generation for *sim_dir*."""
    checkpoints = sim_dir / "reverie" / "checkpoints"
    checkpoints.mkdir(parents=True, exist_ok=True)
    gen_dir = checkpoints / generation
    for name, person_marker in personas:
        _write_valid_bootstrap(
            gen_dir / "personas" / name / "bootstrap_memory", person_marker
        )
    (gen_dir / "meta.json").write_text(
        json.dumps(
            meta
            if meta is not None
            else {"persona_names": [name for name, _ in personas]}
        ),
        encoding="utf-8",
    )
    (checkpoints / "pointer.json").write_text(
        json.dumps({"version": 1, "generation": generation}),
        encoding="utf-8",
    )


def _resolves_under(path: str, external: Path) -> bool:
    """True when *path* resolves inside the external tree after symlinks."""
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    prefix = str(external)
    return real == prefix or real.startswith(prefix + os.sep)


class BackendLeaseMixin:
    """Hold the backend lease in-process for views that require it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._backend_lease_fd = _acquire_backend_lease()

    @classmethod
    def tearDownClass(cls):
        fcntl.flock(cls._backend_lease_fd, fcntl.LOCK_UN)
        os.close(cls._backend_lease_fd)
        super().tearDownClass()

"""
Smallville frontend server views.

Production-hardened: CSRF enabled, operator auth required for simulation
control surfaces, input validation on sim_code/step.  Invalid input,
malformed JSON, and oversized steps return intentional 400/404 responses
instead of uncaught 500s.
"""

import datetime
import fcntl
import json
import os
import re
import stat
import tempfile
import uuid
from contextlib import contextmanager

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from global_methods import *


_VALID_SIM_CODE_RE = re.compile(r"^[\w._-]{1,256}$")
_CANONICAL_STEP_RE = re.compile(r"^(?:0|[1-9][0-9]{0,15})$")
_VALID_STEP_RE = _CANONICAL_STEP_RE
_VALID_PLAY_SPEED_RE = re.compile(r"^[1-6]$")

_MAX_STEP = 9_007_199_254_740_991
_MAX_DEMO_STEP = 100_000
_SAFE_GENERATION_RE = re.compile(r"^[0-9]+$")
_MAX_STEP_STEM_LENGTH = 16

_POINTER_VERSION = 1


def _validate_sim_code(sim_code: str) -> None:
    """Raise ValueError if sim_code contains path traversal or invalid chars."""
    if not _VALID_SIM_CODE_RE.fullmatch(sim_code) or ".." in sim_code:
        raise ValueError(f"Invalid sim_code: {sim_code!r}")


def _validate_step(step: str, max_step: int | None = None) -> int:
    """Validate and convert step to int. Raise ValueError on failure.

    Accepts only the canonical decimal form ``0|[1-9][0-9]{0,15}`` with at
    most 16 digits, matching the backend canonical snapshot contract, and a
    numeric value no greater than the cross-stack maximum
    ``9_007_199_254_740_991``.  Leading zeros, signs, and 17+ digit inputs
    are rejected before int conversion.  If
    *max_step* is provided the step must not exceed it.
    """
    if not _VALID_STEP_RE.fullmatch(str(step)):
        raise ValueError(f"Invalid step: {step!r}")
    value = int(step)
    if value > _MAX_STEP:
        raise ValueError(f"Step {value} exceeds maximum {_MAX_STEP}")
    if max_step is not None and value > max_step:
        raise ValueError(f"Step {value} exceeds maximum {max_step}")
    return value


def _parse_json_body(request) -> dict | None:
    """Parse request.body as JSON.  Return None (and no exception) on
    malformed input so callers can answer 400.

    ValueError covers malformed JSON (json.JSONDecodeError), the integer
    conversion digit limit when a literal exceeds sys.int_max_str_digits(),
    and UnicodeDecodeError when the body is not valid UTF-8.  MemoryError
    and other BaseExceptions are never caught here.
    """
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _bad_request(message: str) -> JsonResponse:
    return JsonResponse({"error": message}, status=400)


def _not_found(message: str) -> JsonResponse:
    return JsonResponse({"error": message}, status=404)


def _server_error(message: str) -> JsonResponse:
    return JsonResponse({"error": message}, status=500)


def _conflict(message: str) -> JsonResponse:
    """Retryable conflict response for a step or simulation mismatch."""
    return JsonResponse({"error": message}, status=409)


def _service_unavailable(message: str) -> JsonResponse:
    """Retryable service-unavailable response for missing runtime markers."""
    return JsonResponse({"error": message}, status=503)


def _pending_movement() -> JsonResponse:
    """Retryable pending response for movement not yet available."""
    return JsonResponse({"<step>": -1, "status": "pending"}, status=202)


def _complete_movement(step: int) -> JsonResponse:
    return JsonResponse({"<step>": step, "status": "complete"})


def _render_startup_error(request):
    return render(request, "home/error_start_backend.html", {})


def _persona_names_from_dir(personas_dir: str) -> list[str]:
    """Return persona directory names under *personas_dir*, excluding dotfiles.

    The directory itself must be a real directory, never a symlink, so no
    name scan can read outside the simulation layout.
    """
    if not _dir_is_real(personas_dir):
        return []
    return [entry for entry in os.listdir(personas_dir) if not entry.startswith(".")]


_MAZE_NAME_RE = re.compile(r"^[\w.-]{1,64}$")


def _load_maze_bounds(maze_name: str) -> tuple[int, int] | None:
    """Return (width, height) tile bounds for *maze_name*, or None when unknown."""
    if not _MAZE_NAME_RE.fullmatch(maze_name) or ".." in maze_name:
        return None
    meta_path = os.path.join(
        "static_dirs", "assets", maze_name, "matrix", "maze_meta_info.json"
    )
    try:
        with open(meta_path, encoding="utf-8") as json_file:
            meta = json.load(json_file)
    except (OSError, ValueError):
        return None
    try:
        width = int(meta["maze_width"])
        height = int(meta["maze_height"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _maze_metadata_from_meta(meta: dict) -> dict | None:
    """Extract maze name and bounds from a meta.json object, or None."""
    maze_name = meta.get("maze_name")
    if not isinstance(maze_name, str) or not maze_name:
        return None
    bounds = _load_maze_bounds(maze_name)
    return {"maze_name": maze_name, "bounds": bounds}


def _load_sim_maze_metadata(sim_dir: str) -> dict | None:
    """Load maze name and bounds for a simulation, confined under *sim_dir*.

    A committed checkpoint pointer is authoritative: when a pointer exists
    the pointed generation must be complete and its meta.json supplies the
    maze, and any malformed pointer or incomplete generation raises
    ValueError so the caller answers a controlled failure instead of
    falling back to legacy state. Only when no pointer exists is the
    legacy ``reverie/meta.json`` consulted. Returns a dict with
    ``maze_name`` and ``bounds`` (width, height), or None when no usable
    maze is declared.
    """
    resolved = _resolve_complete_checkpoint(sim_dir)
    if resolved is not None:
        _, meta, _ = resolved
        return _maze_metadata_from_meta(meta)
    root_fd = _open_sim_root(sim_dir)
    if root_fd is None:
        return None
    try:
        with _descend_dirs(
            root_fd, ("reverie",), "Simulation reverie directory"
        ) as reverie_fd:
            if reverie_fd is None:
                return None
            try:
                candidate = _read_json_at(
                    reverie_fd, "meta.json", "Simulation meta.json"
                )
            except ValueError:
                return None
    finally:
        os.close(root_fd)
    if not isinstance(candidate, dict):
        return None
    return _maze_metadata_from_meta(candidate)


def _validate_environment(
    environment, required_personas: list[str], maze_bounds=None
) -> None:
    """Validate an environment payload. Raise ValueError on malformed data.

    Requires a non-empty object of persona records, each with non-bool,
    non-negative integer x/y coordinates. When *maze_bounds* is provided it
    is authoritative for every record; otherwise a record that names a maze
    with known metadata must fall within its bounds. Every required persona
    name must be present.
    """
    if not isinstance(environment, dict) or not environment:
        raise ValueError("environment must be a non-empty JSON object")
    for name, record in environment.items():
        if not isinstance(name, str) or not name:
            raise ValueError("persona names must be non-empty strings")
        if not isinstance(record, dict):
            raise ValueError(f"persona {name!r} record must be an object")
        for axis in ("x", "y"):
            if axis not in record:
                raise ValueError(f"persona {name!r} record missing {axis!r}")
            value = record[axis]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"persona {name!r} {axis} must be an integer")
            if value < 0:
                raise ValueError(f"persona {name!r} {axis} must be non-negative")
        bounds = maze_bounds
        maze_name = record.get("maze")
        if bounds is None and isinstance(maze_name, str) and maze_name:
            bounds = _load_maze_bounds(maze_name)
        if bounds is not None:
            width, height = bounds
            if record["x"] >= width or record["y"] >= height:
                raise ValueError(
                    f"persona {name!r} position ({record['x']}, {record['y']}) "
                    f"outside maze bounds ({width}, {height})"
                )
    missing = [name for name in required_personas if name not in environment]
    if missing:
        raise ValueError(
            "environment missing required personas: " + ", ".join(sorted(missing))
        )


def _atomic_write_json(file_path: str, data) -> None:
    """Atomically persist *data* as JSON to *file_path*.

    Writes to a temp file in the same directory, flushes and fsyncs it, then
    atomically replaces the target. On any failure the temp file is removed
    and the prior target content is left untouched.
    """
    directory = os.path.dirname(file_path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            json.dump(data, tmp_file, indent=2)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, file_path)
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _atomic_write_json_at(dir_fd: int, name: str, data) -> None:
    """Atomically persist *data* as JSON to *name* under *dir_fd*.

    Creates a unique temp file in the same directory descriptor, writes,
    flushes and fsyncs it, then atomically replaces *name* with os.replace
    using the same descriptor for source and destination, and fsyncs the
    directory. On any failure the temp file is removed and the prior
    target content is left untouched.
    """
    tmp_name = f".tmp-{os.getpid()}-{uuid.uuid4().hex}.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(tmp_name, flags, 0o600, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            json.dump(data, tmp_file, indent=2)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp_name, dir_fd=dir_fd)
        except OSError:
            pass
        raise


@contextmanager
def _step_lock(sim_dir: str, step: int):
    """Serialize writers for one environment step with an exclusive flock.

    The simulation root is opened with O_DIRECTORY | O_NOFOLLOW, the
    ``environment_locks`` directory is created when absent and reopened as
    a real directory, and the per-step lock leaf is opened with
    O_CREAT | O_RDWR | O_NOFOLLOW relative to it. A symlinked or swapped
    root, lock directory, or lock leaf raises ValueError so the caller
    fails controlled. The lock file is persistent and never unlinked so a
    waiting writer can never lock a fresh inode while another holds the
    old one.
    """
    try:
        os.makedirs(sim_dir, exist_ok=True)
    except OSError as exc:
        raise ValueError("Simulation root refused") from exc
    root_fd = _open_sim_root(sim_dir)
    if root_fd is None:
        raise ValueError("Simulation root missing for step lock")
    try:
        try:
            os.mkdir("environment_locks", dir_fd=root_fd)
        except FileExistsError:
            pass
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            locks_fd = os.open("environment_locks", flags, dir_fd=root_fd)
        except OSError as exc:
            raise ValueError(
                "Environment lock directory is not a real directory"
            ) from exc
        try:
            leaf_flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                leaf_flags |= os.O_NOFOLLOW
            try:
                fd = os.open(f"{step}.lock", leaf_flags, dir_fd=locks_fd)
            except OSError as exc:
                raise ValueError("Environment step lock refused") from exc
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
        finally:
            os.close(locks_fd)
    finally:
        os.close(root_fd)


_RUNTIME_LOCK_NAME = "runtime.lock"
_BACKEND_ALIVE_LOCK_NAME = "backend_alive.lock"


def _open_temp_storage_root() -> int:
    """Open ``temp_storage`` as a real directory descriptor.

    Creates the directory when absent, then opens it with O_RDONLY plus
    O_DIRECTORY and O_NOFOLLOW, so a symlinked or non-directory root
    raises ValueError instead of being followed. The caller must close
    the returned descriptor.
    """
    os.makedirs("temp_storage", exist_ok=True)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        return os.open("temp_storage", flags)
    except OSError as exc:
        raise ValueError("temp_storage is not a real directory") from exc


def _open_lock_leaf(root_fd: int, name: str) -> int:
    """Open the persistent lock leaf *name* under *root_fd*.

    The leaf is opened with O_CREAT | O_RDWR plus O_NOFOLLOW relative to
    *root_fd*, so a symlinked or otherwise non-regular leaf raises
    ValueError instead of being followed. The caller must close the
    returned descriptor.
    """
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        return os.open(name, flags, dir_fd=root_fd)
    except OSError as exc:
        raise ValueError(f"Lock file {name} refused") from exc


@contextmanager
def _runtime_lock():
    """Serialize runtime authority mutations with an exclusive fcntl lock.

    The lock root is opened descriptor-relative with O_DIRECTORY |
    O_NOFOLLOW and the lock leaf with O_NOFOLLOW relative to it, so a
    symlinked or swapped temp_storage root or lock leaf raises ValueError
    and the caller fails controlled. The lock file is persistent and never
    unlinked so a waiting holder can never lock a fresh inode while
    another holds the old one. The backend holds the same lock for marker
    clear, marker publish, and per-step authority mutations, so a holder
    sees a stable runtime view.
    """
    temp_fd = _open_temp_storage_root()
    try:
        fd = _open_lock_leaf(temp_fd, _RUNTIME_LOCK_NAME)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    finally:
        os.close(temp_fd)


def _backend_lease_held() -> bool:
    """Return True when a live backend holds the exclusive lease file.

    The temp_storage root is opened with O_DIRECTORY | O_NOFOLLOW and the
    lease leaf with O_CREAT | O_RDWR plus O_NOFOLLOW relative to it. When
    the root or leaf cannot be opened safely False is reported. A
    nonblocking exclusive flock is attempted; when the flock succeeds no
    backend owns the lease, so the lock is released and False is
    reported. BlockingIOError means a backend holds the lease. The file
    is never unlinked so a fresh probe can never lock a new inode while a
    backend still holds the old one.
    """
    try:
        temp_fd = _open_temp_storage_root()
    except ValueError:
        return False
    try:
        try:
            fd = _open_lock_leaf(temp_fd, _BACKEND_ALIVE_LOCK_NAME)
        except ValueError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)
    finally:
        os.close(temp_fd)


def _available_steps_fd(dir_fd: int) -> list[int]:
    """Return numeric steps of safe JSON snapshot names, sorted descending.

    Enumerates only the actual entries of the open directory descriptor
    and stats each candidate relative to it, so a directory swapped to a
    symlink after open can never steer the enumeration. A name is
    accepted only when it is a canonical decimal step (``0.json`` or a
    nonzero name with no leading zero), its stem is at most 16 digits,
    its numeric value does not exceed the cross-stack maximum, and lstat
    reports a regular file. Symlinks, directories, FIFOs, hidden files,
    junk files, oversized numeric names, and beyond-maximum values are
    ignored so no candidate set can expand and no range is materialized.
    """
    try:
        entries = os.listdir(dir_fd)
    except OSError:
        return []
    steps = []
    for entry in entries:
        if not entry.endswith(".json"):
            continue
        stem = entry[: -len(".json")]
        if not stem or len(stem) > _MAX_STEP_STEM_LENGTH:
            continue
        if not _CANONICAL_STEP_RE.fullmatch(stem):
            continue
        try:
            mode = os.lstat(entry, dir_fd=dir_fd).st_mode
        except OSError:
            continue
        if not stat.S_ISREG(mode):
            continue
        value = int(stem)
        if value > _MAX_STEP:
            continue
        steps.append(value)
    steps.sort(reverse=True)
    return steps


def _open_subdir_fd(sim_dir: str, component: str, label: str) -> int | None:
    """Open the single real subdirectory *component* under *sim_dir*.

    Opens the simulation root with O_RDONLY plus O_DIRECTORY and
    O_NOFOLLOW, walks the single component with the same flags, and
    returns an independent descriptor the caller must close. None means
    the tree is genuinely absent; a symlinked or swapped root or
    component raises ValueError so the caller fails controlled instead of
    enumerating or reading outside the simulation layout.
    """
    root_fd = _open_sim_root(sim_dir)
    if root_fd is None:
        return None
    try:
        with _descend_dirs(root_fd, (component,), label) as sub_fd:
            if sub_fd is None:
                return None
            return os.dup(sub_fd)
    finally:
        os.close(root_fd)


def _open_environment_dir(sim_dir: str) -> int | None:
    """Open the real ``environment`` directory under *sim_dir*.

    The simulation root and environment directory are opened with
    O_DIRECTORY | O_NOFOLLOW, so a symlinked or swapped environment
    directory fails controlled with ValueError instead of being followed
    or enumerated. Returns an independent descriptor the caller must
    close, or None when the tree is genuinely absent.
    """
    return _open_subdir_fd(
        sim_dir, "environment", "Simulation environment directory"
    )


def _open_movement_dir(sim_dir: str) -> int | None:
    """Open the real ``movement`` directory under *sim_dir*.

    The simulation root and movement directory are opened with
    O_DIRECTORY | O_NOFOLLOW, so a symlinked or swapped movement
    directory fails controlled with ValueError instead of being followed
    or enumerated. Returns an independent descriptor the caller must
    close, or None when the tree is genuinely absent.
    """
    return _open_subdir_fd(sim_dir, "movement", "Simulation movement directory")


def _available_environment_steps(sim_dir: str) -> list[int]:
    """Return numeric steps of safe JSON snapshot names, sorted descending.

    Opens the real ``environment`` directory under *sim_dir* through the
    sim-root descriptor and enumerates only its actual entries. A
    symlinked or swapped environment directory raises ValueError so the
    caller fails controlled and can never enumerate an external tree. A
    name is accepted only when it is a canonical decimal step (``0.json``
    or a nonzero name with no leading zero), its stem is at most 16
    digits, and lstat reports a regular file. Symlinks, directories,
    FIFOs, hidden files, junk files, and oversized numeric names are
    ignored so no candidate set can expand and no range is materialized.
    """
    env_fd = _open_environment_dir(sim_dir)
    if env_fd is None:
        return []
    try:
        return _available_steps_fd(env_fd)
    finally:
        os.close(env_fd)


def _open_sim_root(sim_dir: str) -> int | None:
    """Open *sim_dir* as a real directory descriptor, or None when absent.

    The root is opened with O_RDONLY plus O_DIRECTORY and O_NOFOLLOW, so a
    symlinked or non-directory root fails controlled instead of being
    followed. The caller must close the returned descriptor.
    """
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        return os.open(sim_dir, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"Simulation root is not a real directory: {exc}") from exc


@contextmanager
def _descend_dirs(root_fd: int, components: tuple[str, ...], label: str):
    """Walk *components* under *root_fd* as real directories.

    Every component is opened with O_RDONLY plus O_DIRECTORY and O_NOFOLLOW
    relative to the previous descriptor, so an ancestor swapped to a
    symlink between checks is refused at the single openat syscall instead
    of being followed. Yields the final descriptor, or None when a
    component is genuinely absent. Every intermediate descriptor is closed
    on exit; *root_fd* is owned by the caller and never closed here.
    """
    current = root_fd
    opened = []
    try:
        for component in components:
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                current = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                yield None
                return
            except OSError as exc:
                raise ValueError(f"{label} is not a real directory") from exc
            opened.append(current)
        yield current
    finally:
        for fd in opened:
            try:
                os.close(fd)
            except OSError:
                pass


def _open_regular_at(dir_fd: int, name: str, label: str) -> int | None:
    """Open *name* under *dir_fd* as a regular file, or None when absent.

    The open uses O_RDONLY plus O_NOFOLLOW and O_NONBLOCK relative to
    *dir_fd* and verifies regular by fstat, so a symlink or other
    non-regular node fails controlled instead of being followed. The
    caller must close the returned descriptor.
    """
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"{label} read refused: {exc}") from exc
    try:
        try:
            is_regular = stat.S_ISREG(os.fstat(fd).st_mode)
        except OSError as exc:
            raise ValueError(f"{label} read refused: {exc}") from exc
        if not is_regular:
            raise ValueError(f"{label} is not a regular file")
        return fd
    except ValueError:
        os.close(fd)
        raise


_MISSING_JSON = object()


def _read_json_value_at(dir_fd: int, name: str, label: str):
    """Read and parse one JSON file under *dir_fd*, or _MISSING_JSON when absent.

    The file is opened descriptor-relative with O_RDONLY plus O_NOFOLLOW
    and O_NONBLOCK, verified regular by fstat, read, and closed on every
    exit path. A symlink, non-regular node, or refused open raises
    ValueError so the caller fails controlled. A genuinely absent file
    returns the private ``_MISSING_JSON`` sentinel so callers can
    distinguish an absent file from an existing file whose content parses
    to JSON null.
    """
    fd = _open_regular_at(dir_fd, name, label)
    if fd is None:
        return _MISSING_JSON
    try:
        chunks = []
        while True:
            try:
                chunk = os.read(fd, 1 << 20)
            except OSError as exc:
                raise ValueError(f"{label} read failed: {exc}") from exc
            if not chunk:
                break
            chunks.append(chunk)
        try:
            return json.loads(b"".join(chunks))
        except (OSError, ValueError) as exc:
            raise ValueError(f"Invalid JSON in {label}: {exc}") from exc
    finally:
        os.close(fd)


def _read_json_at(dir_fd: int, name: str, label: str):
    """Read and parse one JSON file under *dir_fd*, or None when absent.

    The file is opened descriptor-relative with O_RDONLY plus O_NOFOLLOW
    and O_NONBLOCK, verified regular by fstat, read, and closed on every
    exit path. A symlink, non-regular node, or refused open raises
    ValueError so the caller fails controlled; a genuinely absent file
    returns None.
    """
    value = _read_json_value_at(dir_fd, name, label)
    if value is _MISSING_JSON:
        return None
    return value


def _descend_memory_fd(
    sim_dir: str, components: tuple[str, ...], label: str
) -> int | None:
    """Open the memory directory at *components* under *sim_dir*.

    Returns an independent descriptor for the final directory (the caller
    must close it), None when the tree is genuinely absent, and raises
    ValueError when any ancestor is a symlink or non-directory.
    """
    root_fd = _open_sim_root(sim_dir)
    if root_fd is None:
        return None
    try:
        with _descend_dirs(root_fd, components, label) as final_fd:
            if final_fd is None:
                return None
            return os.dup(final_fd)
    finally:
        os.close(root_fd)


def _open_compressed_memory(sim_code: str, persona_name: str) -> int | None:
    """Open the compressed bootstrap memory directory for a persona.

    The compressed tree has no live sim root, so it is treated as static
    data and opened descriptor-relative: the compressed_storage root is
    opened with O_RDONLY plus O_DIRECTORY and O_NOFOLLOW, and the
    simulation, personas, persona, and bootstrap_memory components are
    walked with openat through _descend_dirs, so a symlinked or swapped
    ancestor is refused at the single openat syscall instead of being
    followed. Safe single components are required for both the simulation
    code and the persona name. Returns the descriptor (the caller must
    close it) or None when the tree is genuinely absent; raises
    ValueError when the root or any ancestor is a symlink or
    non-directory.
    """
    _validate_sim_code(sim_code)
    if not _safe_persona_name(persona_name):
        raise ValueError(f"Invalid persona name: {persona_name!r}")
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        root_fd = os.open("compressed_storage", flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("compressed_storage is not a real directory") from exc
    try:
        with _descend_dirs(
            root_fd,
            (sim_code, "personas", persona_name, "bootstrap_memory"),
            "Compressed persona memory",
        ) as memory_fd:
            if memory_fd is None:
                return None
            return os.dup(memory_fd)
    finally:
        os.close(root_fd)


def _read_compressed_simulation_file(sim_code: str, name: str, label: str):
    """Read one compressed_storage simulation file descriptor-relative.

    Opens the compressed_storage root and the simulation directory with
    O_RDONLY plus O_DIRECTORY and O_NOFOLLOW, then reads *name* relative
    to the final descriptor, so a symlinked or swapped ancestor or file
    fails controlled instead of being followed. Returns the parsed JSON,
    or None when the root, simulation directory, or file is genuinely
    absent; raises ValueError when an ancestor is a symlink or
    non-directory or the file is a symlink or non-regular.
    """
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        root_fd = os.open("compressed_storage", flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("compressed_storage is not a real directory") from exc
    try:
        with _descend_dirs(
            root_fd, (sim_code,), "Compressed simulation directory"
        ) as sim_fd:
            if sim_fd is None:
                return None
            return _read_json_at(sim_fd, name, label)
    finally:
        os.close(root_fd)


def _safe_persona_name(name: str) -> bool:
    """True when *name* is a safe single path component for a persona dir.

    Rejects empty names, traversal components, separators, and control
    characters so no persona name can steer path construction outside the
    simulation root. Spaces are allowed because persona directories are
    named with the persona's display name.
    """
    if not isinstance(name, str) or not name:
        return False
    if name in (".", ".."):
        return False
    if "/" in name or "\\" in name:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        return False
    return True


def _read_checkpoint_pointer(sim_dir: str) -> str | None:
    """Read and validate the checkpoint pointer for *sim_dir*.

    Returns the committed generation name when a valid pointer exists, or
    None only when the pointer is genuinely absent so the simulation uses
    the legacy layout. Absence is established through the held checkpoints
    descriptor: a missing pointer file answers ENOENT and maps to the
    private ``_MISSING_JSON`` sentinel, while an existing pointer whose
    content parses to JSON null, a list, a scalar, or any other
    non-object raises ValueError so a null pointer is never mistaken for
    a missing pointer. The simulation root, reverie, and checkpoints
    ancestors are walked descriptor-relative with O_DIRECTORY |
    O_NOFOLLOW, so a symlinked or swapped ancestor raises ValueError and
    no external pointer or generation can be read; the caller never falls
    back to legacy state. A pointer that exists but is malformed, has an
    unsupported version, names an unsafe generation, or is not a regular
    file raises ValueError so the caller fails controlled instead of
    silently reading stale legacy state. The pointer file itself is
    opened descriptor-relative: symlinks and other non-regular files are
    refused.
    """
    root_fd = _open_sim_root(sim_dir)
    if root_fd is None:
        return None
    try:
        with _descend_dirs(
            root_fd,
            ("reverie", "checkpoints"),
            "Simulation checkpoints directory",
        ) as checkpoints_fd:
            if checkpoints_fd is None:
                return None
            pointer = _read_json_value_at(
                checkpoints_fd, "pointer.json", "Checkpoint pointer"
            )
            if pointer is _MISSING_JSON:
                return None
    finally:
        os.close(root_fd)
    if not isinstance(pointer, dict):
        raise ValueError("Invalid checkpoint pointer: expected a JSON object")
    version = pointer.get("version", _POINTER_VERSION)
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != _POINTER_VERSION
    ):
        raise ValueError(f"Unsupported checkpoint pointer version {version!r}")
    generation = pointer.get("generation")
    if not isinstance(generation, str) or not _SAFE_GENERATION_RE.fullmatch(
        generation
    ):
        raise ValueError(f"Invalid checkpoint generation {generation!r}")
    return generation


def _persona_bootstrap_complete(memory_fd: int) -> bool:
    """True when *memory_fd* holds the persona bootstrap memory files.

    *memory_fd* must be an open directory descriptor for the bootstrap
    memory directory. Requires regular scratch.json, spatial_memory.json,
    and associative_memory/nodes.json, embeddings.json, and
    kw_strength.json files, each opened descriptor-relative with
    O_DIRECTORY | O_NOFOLLOW. Symlinks, directories, FIFOs, and other
    non-regular files are refused, so a swapped-in symlink can never
    surface stale or external content. Every opened descriptor is closed.
    """
    try:
        for relative in ("scratch.json", "spatial_memory.json"):
            fd = _open_regular_at(memory_fd, relative, "bootstrap memory")
            if fd is None:
                return False
            os.close(fd)
        with _descend_dirs(
            memory_fd, ("associative_memory",), "associative memory directory"
        ) as assoc_fd:
            if assoc_fd is None:
                return False
            for relative in ("nodes.json", "embeddings.json", "kw_strength.json"):
                fd = _open_regular_at(assoc_fd, relative, "associative memory")
                if fd is None:
                    return False
                os.close(fd)
    except ValueError:
        return False
    return True


def _dir_is_real(path: str) -> bool:
    """True when *path* is a real directory, never a symlink or other node.

    lstat reports the node itself, so a symlinked directory fails closed
    even when its target is a directory.
    """
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _require_real_dir(path: str, label: str) -> None:
    """Raise ValueError when *path* exists but is not a real directory.

    A missing path is allowed so callers treat it as absent, but an
    existing symlink or other non-directory node fails controlled before
    any read can follow it outside the simulation layout.
    """
    if os.path.lexists(path) and not _dir_is_real(path):
        raise ValueError(f"{label} is not a real directory")


_NODE_TYPES = ("event", "chat", "thought")


def _validate_nodes_shape(nodes) -> None:
    """Validate associative memory nodes for safe rendering. Raise ValueError.

    Requires a JSON object with exact contiguous ``node_1`` .. ``node_N``
    keys, and every node must be an object with a supported type
    (``event``, ``chat``, or ``thought``) plus the fields the
    persona_state template renders: ``node_count``, ``created``, and
    ``description`` for every node; ``subject``, ``predicate``, and
    ``object`` for events; ``depth`` and a list ``filling`` for thoughts;
    and a list ``filling`` for chats.
    """
    if not isinstance(nodes, dict):
        raise ValueError("associative memory nodes must be a JSON object")
    for index in range(1, len(nodes) + 1):
        node_id = f"node_{index}"
        if node_id not in nodes:
            raise ValueError(
                "associative memory nodes must have exact contiguous "
                f"node_1..node_{len(nodes)} keys; missing {node_id!r}"
            )
        node = nodes[node_id]
        if not isinstance(node, dict):
            raise ValueError(f"associative memory node {node_id!r} must be an object")
        node_type = node.get("type")
        if node_type not in _NODE_TYPES:
            raise ValueError(
                f"associative memory node {node_id!r} has unsupported "
                f"type {node_type!r}"
            )
        for field in ("node_count", "created", "description"):
            if field not in node:
                raise ValueError(
                    f"associative memory node {node_id!r} missing {field!r}"
                )
        if node_type == "event":
            for field in ("subject", "predicate", "object"):
                if field not in node:
                    raise ValueError(
                        f"associative memory node {node_id!r} missing {field!r}"
                    )
        elif node_type == "thought":
            if "depth" not in node:
                raise ValueError(
                    f"associative memory node {node_id!r} missing 'depth'"
                )
            if not isinstance(node.get("filling"), list):
                raise ValueError(
                    f"associative memory node {node_id!r} 'filling' must be a list"
                )
        elif node_type == "chat":
            if not isinstance(node.get("filling"), list):
                raise ValueError(
                    f"associative memory node {node_id!r} 'filling' must be a list"
                )


def _validate_persona_memory_shapes(memory_fd: int) -> None:
    """Validate every bootstrap memory JSON shape. Raise ValueError.

    *memory_fd* must be an open directory descriptor for the bootstrap
    memory directory. Descriptor-reads scratch.json, spatial_memory.json,
    and the associative memory embeddings.json, nodes.json, and
    kw_strength.json files, walking every directory component with
    O_DIRECTORY | O_NOFOLLOW so a swapped ancestor is refused. Each file
    must parse as JSON of the expected object shape; nodes must satisfy
    _validate_nodes_shape. Any malformed file or shape raises ValueError so
    the caller fails controlled instead of rendering stale or partial state.
    """
    scratch = _load_persona_memory_json(memory_fd, "scratch.json")
    spatial = _load_persona_memory_json(memory_fd, "spatial_memory.json")
    embeddings = _load_persona_memory_json(
        memory_fd, "associative_memory/embeddings.json"
    )
    kw_strength = _load_persona_memory_json(
        memory_fd, "associative_memory/kw_strength.json"
    )
    nodes = _load_persona_memory_json(memory_fd, "associative_memory/nodes.json")
    if not isinstance(scratch, dict):
        raise ValueError("scratch.json must be a JSON object")
    if not isinstance(spatial, dict):
        raise ValueError("spatial_memory.json must be a JSON object")
    if not isinstance(embeddings, dict):
        raise ValueError("associative_memory/embeddings.json must be a JSON object")
    if not isinstance(kw_strength, dict):
        raise ValueError("associative_memory/kw_strength.json must be a JSON object")
    _validate_nodes_shape(nodes)


def _validate_persona_names(names) -> list[str]:
    """Validate checkpoint meta persona names before any path construction.

    Requires a nonempty list of unique strings, each a safe single path
    component with no separators, traversal components, or control
    characters. Every entry is safety-checked before any set operation, so
    unhashable entries (lists, dicts) raise a sanitized ValueError instead
    of an unhandled TypeError. Raises ValueError on any violation.
    """
    if not isinstance(names, list) or not names:
        raise ValueError(
            "Invalid persona_names in checkpoint meta.json: expected a nonempty list"
        )
    for name in names:
        if not _safe_persona_name(name):
            display = repr(name)
            if len(display) > 120:
                display = display[:117] + "..."
            raise ValueError(
                f"Unsafe persona name {display} in checkpoint meta.json; "
                f"refusing to construct persona paths"
            )
    if len(names) != len(set(names)):
        raise ValueError(
            "Invalid persona_names in checkpoint meta.json: names must be unique"
        )
    return names


def _checkpoint_generation_ready(
    sim_dir: str, generation: str
) -> tuple[dict, list[str]] | None:
    """Validate the pointed checkpoint generation, None when not ready.

    Requires the simulation root, reverie, checkpoints, pointed
    generation, personas, per-persona, bootstrap_memory, and
    associative_memory directories to be real directory trees walked
    descriptor-relative with O_DIRECTORY | O_NOFOLLOW (symlinked or
    swapped directories are refused), meta.json to be a descriptor-safe
    regular file containing a JSON object with valid nonempty unique safe
    persona_names, and every declared persona to have complete bootstrap
    files (scratch.json, spatial_memory.json, and associative
    embeddings.json, nodes.json, kw_strength.json) as real regular
    non-symlink files with validated JSON shapes. Returns
    ``(meta, persona_names)``, or None when any requirement fails so the
    caller answers a controlled failure without stale legacy fallback.
    """
    try:
        root_fd = _open_sim_root(sim_dir)
        if root_fd is None:
            return None
        try:
            with _descend_dirs(
                root_fd,
                ("reverie", "checkpoints", generation),
                "Checkpoint generation",
            ) as gen_fd:
                if gen_fd is None:
                    return None
                try:
                    meta = _read_json_at(gen_fd, "meta.json", "Checkpoint meta.json")
                except ValueError:
                    return None
                if meta is None or not isinstance(meta, dict):
                    return None
                try:
                    persona_names = _validate_persona_names(meta.get("persona_names"))
                except ValueError:
                    return None
                with _descend_dirs(
                    gen_fd, ("personas",), "Checkpoint personas directory"
                ) as personas_fd:
                    if personas_fd is None:
                        return None
                    for name in persona_names:
                        with _descend_dirs(
                            personas_fd,
                            (name, "bootstrap_memory"),
                            "Checkpoint persona memory",
                        ) as memory_fd:
                            if memory_fd is None:
                                return None
                            if not _persona_bootstrap_complete(memory_fd):
                                return None
                            try:
                                _validate_persona_memory_shapes(memory_fd)
                            except ValueError:
                                return None
        finally:
            os.close(root_fd)
    except ValueError:
        return None
    return meta, persona_names


def _resolve_complete_checkpoint(sim_dir: str):
    """Resolve the committed complete checkpoint for *sim_dir*.

    Returns ``(generation, meta, persona_names)`` when a valid pointer
    exists and the pointed generation is complete and fully validated,
    None when the simulation uses the legacy layout (no pointer), and
    raises ValueError when a pointer exists but is malformed, unsafe, or
    points at a missing or incomplete generation so the caller fails
    controlled instead of silently reading stale legacy state.
    """
    generation = _read_checkpoint_pointer(sim_dir)
    if generation is None:
        return None
    resolved = _checkpoint_generation_ready(sim_dir, generation)
    if resolved is None:
        raise ValueError(
            f"Checkpoint generation {generation!r} is missing or incomplete"
        )
    meta, persona_names = resolved
    return generation, meta, persona_names


def _load_persona_memory_json(memory_fd: int, relative: str):
    """Descriptor-relative read of one persona bootstrap memory JSON file.

    *memory_fd* must be an open directory descriptor for the bootstrap
    memory directory. The relative path is split into components and every
    directory component is opened with O_DIRECTORY | O_NOFOLLOW before the
    final file is opened with O_RDONLY | O_NOFOLLOW, so no ancestor can be
    swapped to a symlink between checks. Returns the parsed JSON, or None
    when the file is missing, refused (symlink or non-regular), or
    malformed. Every descriptor is closed on every exit path.
    """
    parts = relative.split("/")
    try:
        with _descend_dirs(
            memory_fd, tuple(parts[:-1]), "persona memory directory"
        ) as parent_fd:
            if parent_fd is None:
                return None
            try:
                return _read_json_at(parent_fd, parts[-1], "persona memory JSON")
            except ValueError:
                return None
    except ValueError:
        return None


def _load_environment_snapshot(
    sim_dir: str,
    step: int,
    persona_names,
    *,
    strict_max: bool = False,
    maze_metadata: dict | None = None,
) -> tuple[dict | None, int | None]:
    """Load the environment snapshot at or before *step*, never beyond it.

    Opens the real ``environment`` directory under *sim_dir* through the
    sim-root descriptor, enumerates and stats its actual entries
    descriptor-relative, and reads every candidate with openat. A
    symlinked or swapped environment directory raises ValueError so the
    caller fails controlled. Candidates at or below *step* are considered
    in descending order: the exact step is preferred, then the latest
    valid prior snapshot. With *strict_max* set, a requested *step*
    beyond the authoritative maximum available step returns no snapshot
    at all instead of falling back. *maze_metadata* supplies the
    authoritative bounds when provided; otherwise it is resolved from the
    simulation layout, which raises ValueError when a committed
    checkpoint pointer exists but its generation is malformed or
    incomplete. Returns ``(environment, selected_step)`` or
    ``(None, None)`` when no valid snapshot exists at or before *step*.
    """
    if maze_metadata is None:
        maze_metadata = _load_sim_maze_metadata(sim_dir)
    maze_bounds = maze_metadata["bounds"] if maze_metadata else None
    env_fd = _open_environment_dir(sim_dir)
    if env_fd is None:
        return None, None
    try:
        available = _available_steps_fd(env_fd)
        if strict_max and (not available or step > available[0]):
            return None, None
        for candidate in available:
            if candidate > step:
                continue
            try:
                env = _read_json_at(
                    env_fd, f"{candidate}.json", "Environment snapshot"
                )
            except ValueError:
                continue
            if env is None:
                continue
            try:
                _validate_environment(env, persona_names, maze_bounds=maze_bounds)
            except ValueError:
                continue
            return env, candidate
        return None, None
    finally:
        os.close(env_fd)


def _read_runtime_marker(name: str) -> dict | None:
    """Return the runtime marker JSON object under temp_storage, or None.

    The temp_storage root is opened with O_DIRECTORY | O_NOFOLLOW and the
    marker is read descriptor-relative, so a symlinked or swapped root or
    marker file fails unavailable instead of reading outside the runtime
    directory. Missing, refused, corrupt, and non-object markers return
    None.
    """
    try:
        temp_fd = _open_temp_storage_root()
    except (ValueError, OSError):
        return None
    try:
        try:
            data = _read_json_at(temp_fd, name, "Runtime marker")
        except ValueError:
            return None
    finally:
        os.close(temp_fd)
    if not isinstance(data, dict):
        return None
    return data


def _read_expected_step() -> int | None:
    """Return the backend's expected step from curr_step.json, or None.

    The marker must be an object whose ``step`` value is a non-bool,
    non-negative integer no greater than the cross-stack maximum.
    """
    data = _read_runtime_marker("curr_step.json")
    if data is None:
        return None
    step = data.get("step")
    if isinstance(step, bool) or not isinstance(step, int):
        return None
    if step < 0 or step > _MAX_STEP:
        return None
    return step


def _read_expected_sim_code() -> str | None:
    """Return the backend's expected sim_code from curr_sim_code.json, or None."""
    data = _read_runtime_marker("curr_sim_code.json")
    if data is None:
        return None
    sim_code = data.get("sim_code")
    if not isinstance(sim_code, str) or not sim_code:
        return None
    return sim_code


def landing(request):
    context = {}
    template = "landing/landing.html"
    return render(request, template, context)


def demo(request, sim_code, step, play_speed="2"):
    try:
        _validate_sim_code(sim_code)
        step = _validate_step(step, max_step=_MAX_DEMO_STEP)
    except ValueError as exc:
        return _bad_request(str(exc))
    if not _VALID_PLAY_SPEED_RE.fullmatch(str(play_speed)):
        play_speed = 2
    else:
        play_speed = {1: 1, 2: 2, 3: 4, 4: 8, 5: 16, 6: 32}[int(play_speed)]

    try:
        meta = _read_compressed_simulation_file(
            sim_code, "meta.json", "Simulation meta.json"
        )
    except ValueError:
        return _not_found(f"Simulation {sim_code!r} not found")
    if meta is None:
        return _not_found(f"Simulation {sim_code!r} not found")

    try:
        raw_all_movement = _read_compressed_simulation_file(
            sim_code, "master_movement.json", "Simulation master movement"
        )
    except ValueError:
        return _not_found(f"Simulation {sim_code!r} not found")
    if raw_all_movement is None:
        return _not_found(f"Simulation {sim_code!r} not found")

    max_available = len(raw_all_movement) - 1
    if step > max_available:
        step = max_available

    sec_per_step = meta["sec_per_step"]
    start_datetime = datetime.datetime.strptime(
        meta["start_date"] + " 00:00:00", "%B %d, %Y %H:%M:%S"
    )
    for i in range(step):
        start_datetime += datetime.timedelta(seconds=sec_per_step)
    start_datetime = start_datetime.strftime("%Y-%m-%dT%H:%M:%S")

    persona_names = []
    persona_names_set = set()
    for p in list(raw_all_movement["0"].keys()):
        persona_names += [
            {
                "original": p,
                "underscore": p.replace(" ", "_"),
                "initial": p[0] + p.split(" ")[-1][0],
            }
        ]
        persona_names_set.add(p)

    all_movement = {}

    init_prep = {}
    for int_key in range(step + 1):
        key = str(int_key)
        val = raw_all_movement[key]
        for p in persona_names_set:
            if p in val:
                init_prep[p] = val[p]
    persona_init_pos = {}
    for p in persona_names_set:
        persona_init_pos[p.replace(" ", "_")] = init_prep[p]["movement"]
    all_movement[step] = init_prep

    for int_key in range(step + 1, len(raw_all_movement.keys())):
        all_movement[int_key] = raw_all_movement[str(int_key)]

    context = {
        "sim_code": sim_code,
        "step": step,
        "persona_names": persona_names,
        "persona_init_pos": persona_init_pos,
        "all_movement": all_movement,
        "start_datetime": start_datetime,
        "sec_per_step": sec_per_step,
        "play_speed": play_speed,
        "mode": "demo",
    }
    template = "demo/demo.html"
    return render(request, template, context)


def UIST_Demo(request):
    return demo(request, "March20_the_ville_n25_UIST_RUN-step-1-141", 2160, play_speed="3")


@login_required
def home(request):
    try:
        with _runtime_lock():
            if not _backend_lease_held():
                return _render_startup_error(request)
            sim_code = _read_expected_sim_code()
            step = _read_expected_step()
            if sim_code is None or step is None:
                return _render_startup_error(request)
            try:
                _validate_sim_code(sim_code)
                step = _validate_step(str(step))
            except ValueError:
                return _render_startup_error(request)
    except ValueError:
        return _render_startup_error(request)

    sim_dir = f"storage/{sim_code}"
    try:
        checkpoint = _resolve_complete_checkpoint(sim_dir)
    except ValueError:
        return _render_startup_error(request)

    if checkpoint is not None:
        _, meta, persona_names = checkpoint
        persona_names = [[name, name.replace(" ", "_")] for name in persona_names]
        persona_names_set = {name for name, _ in persona_names}
        maze_metadata = _maze_metadata_from_meta(meta)
    else:
        personas_dir = os.path.join(sim_dir, "personas")
        if not _dir_is_real(personas_dir):
            return _render_startup_error(request)
        persona_names = []
        persona_names_set = set()
        for i in find_filenames(personas_dir, ""):
            x = i.split("/")[-1].strip()
            if x[0] != ".":
                persona_names += [[x, x.replace(" ", "_")]]
                persona_names_set.add(x)
        maze_metadata = None

    try:
        env, selected_step = _load_environment_snapshot(
            sim_dir, step, persona_names_set, maze_metadata=maze_metadata
        )
    except ValueError:
        return _render_startup_error(request)
    if env is None:
        return _render_startup_error(request)

    persona_init_pos = []
    for key, val in env.items():
        if key in persona_names_set:
            persona_init_pos += [[key, val["x"], val["y"]]]

    initial_phase = "update" if selected_step == step else "process"

    context = {
        "sim_code": sim_code,
        "step": step,
        "persona_names": persona_names,
        "persona_init_pos": persona_init_pos,
        "mode": "simulate",
        "initial_phase": initial_phase,
    }
    template = "home/home.html"
    return render(request, template, context)


@login_required
def replay(request, sim_code, step):
    try:
        _validate_sim_code(sim_code)
        step = _validate_step(step)
    except ValueError as exc:
        return _bad_request(str(exc))

    sim_dir = f"storage/{sim_code}"
    try:
        checkpoint = _resolve_complete_checkpoint(sim_dir)
    except ValueError as exc:
        return _not_found(
            f"Checkpoint for simulation {sim_code!r} is missing or "
            f"incomplete: {exc}"
        )

    if checkpoint is not None:
        _, meta, persona_names = checkpoint
        persona_names = [[name, name.replace(" ", "_")] for name in persona_names]
        persona_names_set = {name for name, _ in persona_names}
        maze_metadata = _maze_metadata_from_meta(meta)
    else:
        personas_dir = os.path.join(sim_dir, "personas")
        if not _dir_is_real(personas_dir):
            return _not_found(f"Simulation {sim_code!r} not found")
        persona_names = []
        persona_names_set = set()
        for i in find_filenames(personas_dir, ""):
            x = i.split("/")[-1].strip()
            if x[0] != ".":
                persona_names += [[x, x.replace(" ", "_")]]
                persona_names_set.add(x)
        maze_metadata = None

    try:
        env, selected_step = _load_environment_snapshot(
            sim_dir,
            step,
            persona_names_set,
            strict_max=True,
            maze_metadata=maze_metadata,
        )
    except ValueError:
        return _not_found(
            f"Environment for simulation {sim_code!r} is not a real directory"
        )
    if env is None:
        return _not_found(
            f"Environment step {step} not available for simulation {sim_code!r}"
        )

    persona_init_pos = []
    for key, val in env.items():
        if key in persona_names_set:
            persona_init_pos += [[key, val["x"], val["y"]]]

    initial_phase = "update" if selected_step == step else "process"

    context = {
        "sim_code": sim_code,
        "step": step,
        "persona_names": persona_names,
        "persona_init_pos": persona_init_pos,
        "mode": "replay",
        "initial_phase": initial_phase,
    }
    template = "home/home.html"
    return render(request, template, context)


def _load_persona_state_memory(memory_fd: int):
    """Descriptor-relative load of one persona's bootstrap memory.

    *memory_fd* must be an open directory descriptor for the bootstrap
    memory directory. Returns
    ``(scratch, spatial, a_mem_event, a_mem_chat, a_mem_thought)``
    or None when any required file is missing, refused (symlink or
    non-regular), or malformed. Every directory component is walked with
    O_DIRECTORY | O_NOFOLLOW so a swapped ancestor is never followed. The
    nodes object must satisfy _validate_nodes_shape so every node has a
    supported type and the template-rendered fields; anything else returns
    None so the caller answers a controlled failure instead of a 500
    traceback.
    """
    scratch = _load_persona_memory_json(memory_fd, "scratch.json")
    spatial = _load_persona_memory_json(memory_fd, "spatial_memory.json")
    associative = _load_persona_memory_json(
        memory_fd, "associative_memory/nodes.json"
    )
    if scratch is None or spatial is None or associative is None:
        return None
    try:
        _validate_nodes_shape(associative)
    except ValueError:
        return None
    a_mem_event = []
    a_mem_chat = []
    a_mem_thought = []
    for count in range(len(associative), 0, -1):
        node_id = f"node_{count}"
        node_details = associative[node_id]
        node_type = node_details.get("type")
        if node_type == "event":
            a_mem_event += [node_details]
        elif node_type == "chat":
            a_mem_chat += [node_details]
        elif node_type == "thought":
            a_mem_thought += [node_details]
    return scratch, spatial, a_mem_event, a_mem_chat, a_mem_thought


@login_required
def replay_persona_state(request, sim_code, step, persona_name):
    try:
        _validate_sim_code(sim_code)
        step = _validate_step(step)
    except ValueError as exc:
        return _bad_request(str(exc))

    if not _safe_persona_name(persona_name):
        return _bad_request(f"Invalid persona name: {persona_name!r}")
    persona_name_underscore = persona_name
    persona_name = " ".join(persona_name.split("_"))
    sim_dir = f"storage/{sim_code}"

    try:
        generation = _read_checkpoint_pointer(sim_dir)
    except ValueError as exc:
        return _server_error(str(exc))

    if generation is not None:
        resolved = _checkpoint_generation_ready(sim_dir, generation)
        if resolved is None:
            return _not_found(
                f"Checkpoint generation {generation!r} for {sim_code!r} "
                f"is missing or incomplete"
            )
        _, persona_names = resolved
        if persona_name not in persona_names:
            return _not_found(
                f"Persona state for {persona_name!r} in {sim_code!r} not found"
            )
        try:
            memory_fd = _descend_memory_fd(
                sim_dir,
                (
                    "reverie",
                    "checkpoints",
                    generation,
                    "personas",
                    persona_name,
                    "bootstrap_memory",
                ),
                "Checkpoint persona memory",
            )
        except ValueError:
            return _not_found(
                f"Persona state for {persona_name!r} in {sim_code!r} not found"
            )
        if memory_fd is None:
            return _not_found(
                f"Persona state for {persona_name!r} in {sim_code!r} not found"
            )
        try:
            loaded = _load_persona_state_memory(memory_fd)
        finally:
            os.close(memory_fd)
        if loaded is None:
            return _not_found(
                f"Persona state for {persona_name!r} in {sim_code!r} not found"
            )
    else:
        try:
            root_fd = _open_sim_root(sim_dir)
        except ValueError:
            return _not_found(
                f"Persona state for {persona_name!r} in {sim_code!r} not found"
            )
        if root_fd is None:
            try:
                compressed_fd = _open_compressed_memory(sim_code, persona_name)
            except ValueError:
                return _not_found(
                    f"Persona state for {persona_name!r} in {sim_code!r} not found"
                )
            if compressed_fd is None:
                return _not_found(
                    f"Persona state for {persona_name!r} in {sim_code!r} not found"
                )
            try:
                loaded = _load_persona_state_memory(compressed_fd)
            finally:
                os.close(compressed_fd)
            if loaded is None:
                return _not_found(
                    f"Persona state for {persona_name!r} in {sim_code!r} not found"
                )
        else:
            try:
                with _descend_dirs(
                    root_fd,
                    ("personas", persona_name, "bootstrap_memory"),
                    "Persona bootstrap memory",
                ) as memory_fd:
                    if memory_fd is None:
                        return _not_found(
                            f"Persona state for {persona_name!r} in {sim_code!r} "
                            "not found"
                        )
                    loaded = _load_persona_state_memory(memory_fd)
                    if loaded is None:
                        return _not_found(
                            f"Persona state for {persona_name!r} in {sim_code!r} "
                            "not found"
                        )
            except ValueError:
                return _not_found(
                    f"Persona state for {persona_name!r} in {sim_code!r} not found"
                )
            finally:
                os.close(root_fd)

    scratch, spatial, a_mem_event, a_mem_chat, a_mem_thought = loaded

    context = {
        "sim_code": sim_code,
        "step": step,
        "persona_name": persona_name,
        "persona_name_underscore": persona_name_underscore,
        "scratch": scratch,
        "spatial": spatial,
        "a_mem_event": a_mem_event,
        "a_mem_chat": a_mem_chat,
        "a_mem_thought": a_mem_thought,
    }
    template = "persona_state/persona_state.html"
    return render(request, template, context)


@login_required
def path_tester(request):
    context = {}
    template = "path_tester/path_tester.html"
    return render(request, template, context)


@login_required
@require_POST
@csrf_protect
def process_environment(request):
    """
    <FRONTEND to BACKEND>
    Sends the frontend visual world information to the backend server.

    Requires operator authentication for production safety. Accepts the
    environment only for the backend's current expected runtime step as
    atomically published in temp_storage/curr_step.json and
    temp_storage/curr_sim_code.json. Missing or corrupt markers answer 503,
    and a step or simulation mismatch answers 409, both without writing any
    file so the frontend can retry.

    The runtime lock is held from before the first marker read through
    validation, the per-step lock, the immutable comparison, the atomic
    publication, and the response decision. Because the backend holds the
    same lock for marker clear, marker publish, and per-step authority
    mutations, no marker or prune can interleave with this request. An
    already published step is immutable: an identical payload answers 200
    without rewriting, and a differing or corrupt file answers 409 and is
    preserved. No prior or concurrent environment is ever deleted.
    """
    data = _parse_json_body(request)
    if data is None:
        return _bad_request("Malformed JSON body")
    step = data.get("step")
    sim_code = data.get("sim_code")
    environment = data.get("environment")

    if step is None or sim_code is None or environment is None:
        return _bad_request("Missing required fields")

    try:
        _validate_sim_code(str(sim_code))
        step = _validate_step(str(step))
    except ValueError as exc:
        return _bad_request(str(exc))

    try:
        with _runtime_lock():
            if not _backend_lease_held():
                return _service_unavailable("Simulation backend not alive")

            expected_step = _read_expected_step()
            expected_sim_code = _read_expected_sim_code()
            if expected_step is None or expected_sim_code is None:
                return _service_unavailable("Simulation runtime not ready")
            if sim_code != expected_sim_code or step != expected_step:
                return _conflict("Simulation runtime mismatch")

            sim_dir = f"storage/{sim_code}"
            try:
                checkpoint = _resolve_complete_checkpoint(sim_dir)
            except ValueError as exc:
                return _server_error(str(exc))
            if checkpoint is not None:
                _, meta, persona_names = checkpoint
                required_personas = persona_names
                maze_metadata = _maze_metadata_from_meta(meta)
            else:
                personas_dir = os.path.join(sim_dir, "personas")
                if os.path.lexists(personas_dir) and not _dir_is_real(personas_dir):
                    return _server_error(
                        "Simulation personas directory is not a real directory"
                    )
                required_personas = _persona_names_from_dir(personas_dir)
                maze_metadata = _load_sim_maze_metadata(sim_dir)
            maze_bounds = maze_metadata["bounds"] if maze_metadata else None
            try:
                _validate_environment(
                    environment, required_personas, maze_bounds=maze_bounds
                )
            except ValueError as exc:
                return _bad_request(str(exc))

            try:
                with _step_lock(sim_dir, step):
                    root_fd = _open_sim_root(sim_dir)
                    if root_fd is None:
                        return _server_error("Simulation root missing")
                    try:
                        try:
                            os.mkdir("environment", dir_fd=root_fd)
                        except FileExistsError:
                            pass
                        env_flags = os.O_RDONLY
                        if hasattr(os, "O_DIRECTORY"):
                            env_flags |= os.O_DIRECTORY
                        if hasattr(os, "O_NOFOLLOW"):
                            env_flags |= os.O_NOFOLLOW
                        try:
                            env_fd = os.open(
                                "environment", env_flags, dir_fd=root_fd
                            )
                        except OSError as exc:
                            return _server_error(
                                "Simulation environment directory is not a "
                                "real directory"
                            )
                        try:
                            try:
                                existing = _read_json_value_at(
                                    env_fd,
                                    f"{step}.json",
                                    "Environment snapshot",
                                )
                            except ValueError:
                                return _conflict(
                                    "Environment already published for step"
                                )
                            if existing is not _MISSING_JSON:
                                if existing == environment:
                                    if not _backend_lease_held():
                                        return _service_unavailable(
                                            "Simulation backend not alive"
                                        )
                                    return HttpResponse("received")
                                return _conflict(
                                    "Environment already published for step"
                                )
                            try:
                                _atomic_write_json_at(
                                    env_fd, f"{step}.json", environment
                                )
                            except OSError:
                                return _server_error(
                                    "Failed to persist environment"
                                )
                            if not _backend_lease_held():
                                return _service_unavailable(
                                    "Simulation backend not alive"
                                )
                            return HttpResponse("received")
                        finally:
                            os.close(env_fd)
                    finally:
                        os.close(root_fd)
            except ValueError as exc:
                return _server_error(str(exc))
    except ValueError:
        return _server_error("Simulation runtime lock refused")


@login_required
@require_POST
@csrf_protect
def update_environment(request):
    """
    <BACKEND to FRONTEND>
    Sends the backend computation of persona behavior to the frontend.

    The runtime marker identifies the next expected environment step. A
    movement query may target that step while it is pending or its immediately
    preceding step after backend processing advances the marker. Other step or
    simulation mismatches answer 409 with a resync payload carrying safe
    runtime values and never movement data. The runtime lock is held across
    the marker gate and movement read so a backend restart or prune cannot
    interleave.
    """
    data = _parse_json_body(request)
    if data is None:
        return _bad_request("Malformed JSON body")
    step = data.get("step")
    sim_code = data.get("sim_code")

    if step is None or sim_code is None:
        return _bad_request("Missing required fields")

    try:
        _validate_sim_code(str(sim_code))
        step_int = _validate_step(str(step))
    except ValueError as exc:
        return _bad_request(str(exc))

    try:
        with _runtime_lock():
            if not _backend_lease_held():
                return _service_unavailable("Simulation backend not alive")

            expected_step = _read_expected_step()
            expected_sim_code = _read_expected_sim_code()
            if expected_step is None or expected_sim_code is None:
                return _service_unavailable("Simulation runtime not ready")

            if (
                sim_code != expected_sim_code
                or step_int not in (expected_step, expected_step - 1)
            ):
                return JsonResponse(
                    {
                        "status": "resync",
                        "expected_step": expected_step,
                        "expected_sim_code": expected_sim_code,
                    },
                    status=409,
                )

            if step_int == expected_step:
                return _pending_movement()

            sim_dir = f"storage/{sim_code}"
            try:
                movement_fd = _open_movement_dir(sim_dir)
            except ValueError:
                return _server_error(
                    "Simulation movement directory is not a real directory"
                )
            if movement_fd is None:
                return _pending_movement()
            try:
                try:
                    response_data = _read_json_at(
                        movement_fd, f"{step_int}.json", "Movement file"
                    )
                except ValueError:
                    return _pending_movement()
            finally:
                os.close(movement_fd)

            if not isinstance(response_data, dict):
                return _pending_movement()

            if not _backend_lease_held():
                return _service_unavailable("Simulation backend not alive")

            response_data["<step>"] = step_int
            return JsonResponse(response_data)
    except ValueError:
        return _server_error("Simulation runtime lock refused")


@login_required
@require_POST
@csrf_protect
def replay_movement(request):
    data = _parse_json_body(request)
    if data is None:
        return _bad_request("Malformed JSON body")
    step = data.get("step")
    sim_code = data.get("sim_code")
    if step is None or sim_code is None:
        return _bad_request("Missing required fields")
    try:
        _validate_sim_code(str(sim_code))
        step_int = _validate_step(str(step))
    except ValueError as exc:
        return _bad_request(str(exc))
    sim_dir = f"storage/{sim_code}"
    try:
        root_fd = _open_sim_root(sim_dir)
    except ValueError:
        return _server_error("Simulation root is not a real directory")
    if root_fd is None:
        return _not_found(f"Simulation {sim_code!r} not found")
    os.close(root_fd)
    try:
        movement_fd = _open_movement_dir(sim_dir)
    except ValueError:
        return _server_error(
            "Simulation movement directory is not a real directory"
        )
    if movement_fd is None:
        return _complete_movement(step_int)
    try:
        available_steps = _available_steps_fd(movement_fd)
        if not available_steps or step_int > available_steps[0]:
            return _complete_movement(step_int)
        try:
            response_data = _read_json_at(
                movement_fd, f"{step_int}.json", "Movement file"
            )
        except ValueError:
            return _pending_movement()
    finally:
        os.close(movement_fd)
    if not isinstance(response_data, dict):
        return _pending_movement()
    response_data["<step>"] = step_int
    return JsonResponse(response_data)


@login_required
@require_POST
@csrf_protect
def path_tester_update(request):
    """
    Processing the path and saving it to path_tester_env.json temp storage.
    """
    data = _parse_json_body(request)
    if data is None:
        return _bad_request("Malformed JSON body")
    camera = data.get("camera")

    if camera is None:
        return _bad_request("Missing camera data")

    if not isinstance(camera, dict) or "x" not in camera or "y" not in camera:
        return _bad_request("Invalid camera format")

    try:
        int(camera["x"])
        int(camera["y"])
    except (ValueError, TypeError):
        return _bad_request("Invalid camera coordinates")

    os.makedirs("temp_storage", exist_ok=True)
    try:
        _atomic_write_json("temp_storage/path_tester_env.json", camera)
    except OSError:
        return _server_error("Failed to persist camera data")

    return HttpResponse("received")


@require_GET
def health(request):
    """Simple health check endpoint."""
    return JsonResponse({"status": "ok"})

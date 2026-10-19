"""
ReverieServer: generative agent simulation backend.

Supports two modes:
  1. Interactive CLI mode (preserved for backwards compatibility)
  2. Service mode (non-interactive, for production deployment)

Service mode starts the simulation backend without stdin prompts and can
continuously process simulation steps as the frontend writes env files.

Usage (interactive):
    uv run python reverie.py

Usage (service mode):
    uv run python reverie.py --serve \\
        --origin base_the_ville_isabella_maria_klaus \\
        --target MySimulation

    Or via environment variables:
    REVERIE_FORK_SIM_CODE=base_the_ville_isabella_maria_klaus \\
    REVERIE_SIM_CODE=MySimulation \\
        uv run python reverie.py --serve

Persistence uses a versioned checkpoint protocol: a complete generation
(meta plus every persona's bootstrap memory) is committed by atomically
publishing a small pointer JSON under ``reverie/checkpoints/pointer.json``.
The pointer is the single commit marker, so a crash between persona files can
never leave a mixed generation authoritative. Simulations without a pointer
keep using the legacy layout unchanged.
"""

import argparse
import collections
import csv
import datetime
import json
import math
import os
import pickle
import re
import shutil
import stat
import sys
import threading
import time
import traceback
import uuid

from selenium import webdriver

from durability import (
    LivenessLeaseError,
    atomic_write_json,
    atomic_write_json_beneath,
    ensure_dir_beneath,
    fsync_dir,
    fsync_dir_checked,
    fsync_regular,
    install_shutdown_handlers,
    is_real_dir,
    is_real_regular,
    liveness_lease,
    new_shutdown_event,
    read_json_beneath,
    rename_no_replace,
    runtime_lock,
)
from global_methods import *
from utils import *
from maze import *
from persona.persona import *


##############################################################################
#                                  REVERIE                                   #
##############################################################################


_POINTER_VERSION = 1
_SAFE_GENERATION_RE = re.compile(r"^[0-9]+$")
_NUMERIC_JSON_RE = re.compile(r"^(0|[1-9][0-9]*)\.json$")
_SAFE_SIM_CODE_RE = re.compile(r"^[\w.-]{1,256}$")
_CHECKPOINT_STAGING = ".staging"
_Checkpoint = collections.namedtuple("_Checkpoint", ["generation", "meta"])

_MAX_STEP_DIGITS = 16
_MAX_STEP = 9_007_199_254_740_991

_REQUIRED_META_FIELDS = (
    "fork_sim_code",
    "start_date",
    "curr_time",
    "sec_per_step",
    "maze_name",
    "persona_names",
    "step",
)

_RUN_MAX_STEPS = 10000
_RUN_COUNT_RE = re.compile(r"^run\s+([0-9]{1,5})$", re.IGNORECASE)

_STALE_MARKER_FILES = (
    "backend_heartbeat.json",
    "curr_step.json",
    "curr_sim_code.json",
)

_RUNTIME_LOCK_FILE = "runtime.lock"

_LIVENESS_LOCK_FILE = "backend_alive.lock"

_HEARTBEAT_MIN_INTERVAL = 0.01

_PACE_WAIT_CHUNK_SECONDS = 60.0


def _runtime_lock_path():
    """Return the shared runtime authority lock path."""
    return f"{fs_temp_storage}/{_RUNTIME_LOCK_FILE}"


def _liveness_lease_path():
    """Return the backend liveness lease path."""
    return f"{fs_temp_storage}/{_LIVENESS_LOCK_FILE}"


_PLACEHOLDER_API_KEYS = frozenset(
    {"sk-or-v1-your-openrouter-key", "sk-your-openai-api-key"}
)


def _safe_persona_name(name):
    """True when ``name`` is a safe single path component."""
    if not isinstance(name, str) or not name:
        return False
    if name in (".", ".."):
        return False
    if "/" in name or "\\" in name:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        return False
    return True


def _validate_persona_names(names):
    """Validate metadata persona names before any persona path construction.

    Requires a nonempty list of unique strings, each a safe single path
    component with no separators, traversal components, or control
    characters. Every entry is checked before any set or uniqueness
    operation, so unhashable entries (lists, dicts) raise a sanitized
    ``ValueError`` instead of an unhandled ``TypeError``. Raises
    ``ValueError`` so resume and fresh fork fail closed instead of
    constructing paths outside the simulation root.
    """
    if not isinstance(names, list) or not names:
        raise ValueError(
            "Invalid persona_names in reverie meta.json: expected a "
            "nonempty list."
        )
    for name in names:
        if not _safe_persona_name(name):
            display = repr(name)
            if len(display) > 120:
                display = display[:117] + "..."
            raise ValueError(
                f"Unsafe persona name {display} in reverie meta.json; "
                f"refusing to construct persona paths."
            )
    if len(names) != len(set(names)):
        raise ValueError(
            "Invalid persona_names in reverie meta.json: names must be "
            "unique."
        )
    return names


def _validate_step(step):
    """Validate a loaded step value before any step-derived path work.

    Requires a non-bool integer in ``0.._MAX_STEP``, the canonical snapshot
    bound of at most 16 decimal digits matching the cross-stack
    ``Number.MAX_SAFE_INTEGER``, so a crafted meta or checkpoint cannot steer
    path construction or snapshot enumeration outside the bounded numeric
    namespace. Raises ``ValueError``.
    """
    if not isinstance(step, int) or isinstance(step, bool):
        raise ValueError(
            f"Invalid step {step!r} in meta.json: "
            f"expected a non-negative integer."
        )
    if step < 0 or step > _MAX_STEP:
        raise ValueError(
            f"Invalid step {step!r} in meta.json: expected an integer from "
            f"0 to {_MAX_STEP}."
        )
    return step


def _is_positive_number(value):
    """True when ``value`` is a finite positive non-bool int or float.

    Booleans are excluded even though ``bool`` subclasses ``int``, and
    non-finite values (NaN, infinity, or an int too large for a float) are
    rejected so a crafted ``sec_per_step`` can never produce a nonsense time
    delta.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    if value <= 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _validate_sim_code(code, *, label):
    """Validate a simulation code before any path construction.

    Accepts 1..256 word/dot/hyphen characters (the Django-safe pattern) but
    rejects ``..`` traversal, absolute paths, slashes, control characters,
    and empty values. Raises ``ValueError`` with a clear, non-secret message.
    """
    if not isinstance(code, str) or not code:
        raise ValueError(f"Invalid {label}: simulation code is empty.")
    if len(code) > 256:
        raise ValueError(
            f"Invalid {label}: simulation code exceeds 256 characters."
        )
    if "/" in code or "\\" in code:
        raise ValueError(
            f"Invalid {label}: simulation code must not contain slashes."
        )
    if code in (".", ".."):
        raise ValueError(
            f"Invalid {label}: simulation code must not be '.' or '..'."
        )
    if ".." in code:
        raise ValueError(
            f"Invalid {label}: simulation code must not contain '..'."
        )
    if not _SAFE_SIM_CODE_RE.fullmatch(code):
        raise ValueError(
            f"Invalid {label}: simulation code must contain only letters, "
            f"digits, underscores, dots, and hyphens."
        )
    return code


def _safe_maze_name(name):
    """True when ``name`` is a safe nonempty maze name string."""
    if not isinstance(name, str) or not name:
        return False
    if name in (".", ".."):
        return False
    if "/" in name or "\\" in name:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        return False
    return True


def _validate_reverie_meta(meta):
    """Validate a reverie meta object before any direct indexing.

    Requires a JSON object carrying every required field: a safe origin
    simulation code, parseable ``start_date`` and ``curr_time``, a positive
    finite non-bool ``sec_per_step``, a safe nonempty ``maze_name``, valid
    unique persona names, and a bounded non-bool integer ``step``. Raises
    ``ValueError`` with the offending field name so every legacy, checkpoint,
    and staged meta fails closed before any field is indexed or any path is
    constructed from it.
    """
    if not isinstance(meta, dict):
        raise ValueError("Invalid reverie meta.json: expected a JSON object.")
    for field in _REQUIRED_META_FIELDS:
        if field not in meta:
            raise ValueError(
                f"Invalid reverie meta.json: missing required field "
                f"{field!r}."
            )
    try:
        _validate_sim_code(
            meta["fork_sim_code"], label="origin simulation code"
        )
    except ValueError as exc:
        raise ValueError(
            f"Invalid fork_sim_code {meta['fork_sim_code']!r} in reverie "
            f"meta.json: {exc}."
        ) from exc
    try:
        _validate_persona_names(meta["persona_names"])
    except ValueError as exc:
        raise ValueError(
            f"Invalid persona_names in reverie meta.json: {exc}."
        ) from exc
    _validate_step(meta["step"])

    start_date = meta["start_date"]
    if not isinstance(start_date, str) or not start_date:
        raise ValueError(
            f"Invalid start_date {start_date!r} in reverie meta.json: "
            f"expected a nonempty string."
        )
    try:
        datetime.datetime.strptime(
            f"{start_date}, 00:00:00", "%B %d, %Y, %H:%M:%S"
        )
    except ValueError as exc:
        raise ValueError(
            f"Invalid start_date {start_date!r} in reverie meta.json: "
            f"{exc}."
        ) from exc

    curr_time = meta["curr_time"]
    if not isinstance(curr_time, str) or not curr_time:
        raise ValueError(
            f"Invalid curr_time {curr_time!r} in reverie meta.json: "
            f"expected a nonempty string."
        )
    try:
        datetime.datetime.strptime(curr_time, "%B %d, %Y, %H:%M:%S")
    except ValueError as exc:
        raise ValueError(
            f"Invalid curr_time {curr_time!r} in reverie meta.json: "
            f"{exc}."
        ) from exc

    sec_per_step = meta["sec_per_step"]
    if not _is_positive_number(sec_per_step):
        raise ValueError(
            f"Invalid sec_per_step {sec_per_step!r} in reverie meta.json: "
            f"expected a positive non-bool number."
        )

    maze_name = meta["maze_name"]
    if not _safe_maze_name(maze_name):
        raise ValueError(
            f"Invalid maze_name {maze_name!r} in reverie meta.json: "
            f"expected a nonempty safe string."
        )
    return meta


def _validate_history_suffix(suffix):
    """Validate a ``call -- load history`` file suffix before any open.

    Requires a nonempty relative path with no absolute components, no
    traversal components, no empty or dot components, no backslashes, and no
    control characters. Raises ``ValueError`` so the command handler can
    print a controlled message instead of a traceback.
    """
    if not isinstance(suffix, str) or not suffix:
        raise ValueError(
            "call -- load history requires a CSV file name."
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in suffix):
        raise ValueError(
            "call -- load history: file name must not contain control "
            "characters."
        )
    if os.path.isabs(suffix):
        raise ValueError(
            "call -- load history: file name must be relative to the "
            "assets directory."
        )
    if "\\" in suffix:
        raise ValueError(
            "call -- load history: file name must not contain backslashes."
        )
    if any(part in ("", ".", "..") for part in suffix.split("/")):
        raise ValueError(
            "call -- load history: file name must not contain traversal "
            "components."
        )
    return suffix


def _read_history_rows(suffix):
    """Read a validated history CSV from beneath the assets directory.

    The suffix is validated before any path construction, then the assets
    root is opened once and every component is opened descriptor-relative
    with ``dir_fd``. Intermediate components require ``O_DIRECTORY`` plus
    ``O_NOFOLLOW``; the final component requires ``O_NOFOLLOW`` plus
    ``O_NONBLOCK`` and must be a regular file. A symlink swapped in at any
    level is refused by the kernel, so there is no realpath-then-open race.
    The descriptor is read as UTF-8 CSV and every descriptor is closed on
    every exit path. Returns the data rows below the header row with
    stripped fields; blank rows are dropped. Raises ``ValueError`` on any
    unsafe or unreadable input.
    """
    suffix = _validate_history_suffix(suffix)
    dir_flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        dir_flags |= os.O_NOFOLLOW
    file_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        file_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        file_flags |= os.O_NONBLOCK
    fds = []
    try:
        fds.append(os.open(maze_assets_loc, dir_flags))
        parts = suffix.split("/")
        for component in parts[:-1]:
            fds.append(os.open(component, dir_flags, dir_fd=fds[-1]))
        fds.append(os.open(parts[-1], file_flags, dir_fd=fds[-1]))
        st = os.fstat(fds[-1])
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(
                "call -- load history: file is not a regular file."
            )
        with os.fdopen(fds[-1], "r", encoding="utf-8", newline="") as csv_file:
            fds.pop()
            rows = list(csv.reader(csv_file))
    except OSError as exc:
        raise ValueError(
            f"call -- load history: cannot read '{suffix}': {exc}."
        ) from exc
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
    if not rows:
        raise ValueError("call -- load history: file is empty.")
    return [
        [field.strip() for field in row]
        for row in rows[1:]
        if any(field.strip() for field in row)
    ]


def _unlink_markers():
    """Unlink stale runtime markers without acquiring the runtime lock.

    Callers must already hold the runtime lock. The temp storage root is
    opened once with ``O_DIRECTORY | O_NOFOLLOW`` and every marker is
    unlinked with ``dir_fd`` relative to that descriptor, so a symlinked or
    swapped temp root is refused and never deletes outside it. Missing
    files are fine.
    """
    fd = _open_real_dir(fs_temp_storage)
    if fd is None:
        return
    try:
        for name in _STALE_MARKER_FILES:
            try:
                os.unlink(name, dir_fd=fd)
            except FileNotFoundError:
                pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _clear_stale_markers():
    """Remove stale runtime markers from fs_temp_storage under the lock.

    Only the marker paths themselves are unlinked through a verified temp
    root descriptor; a symlink target is never followed or removed. Missing
    files are fine. The runtime lock is held so a concurrent publish or
    per-step update cannot interleave with the clear.
    """
    with runtime_lock(_runtime_lock_path()):
        _unlink_markers()


def _validate_generation_name(name):
    """Raise ``ValueError`` when ``name`` is not a safe generation name."""
    if not isinstance(name, str) or not _SAFE_GENERATION_RE.fullmatch(name):
        raise ValueError(
            f"Invalid checkpoint generation name {name!r}: expected a "
            f"numeric directory name."
        )


def _bootstrap_memory_complete(base):
    """True when ``base`` holds a complete persona bootstrap memory.

    Every required file is verified by lstat to be a real regular file and
    every required directory to be a real directory, so a symlink at any
    required path (dangling or not) is rejected instead of followed.
    """
    if not is_real_dir(base):
        return False
    if not is_real_regular(f"{base}/scratch.json"):
        return False
    if not is_real_regular(f"{base}/spatial_memory.json"):
        return False
    assoc = f"{base}/associative_memory"
    if not is_real_dir(assoc):
        return False
    for fname in ("embeddings.json", "nodes.json", "kw_strength.json"):
        if not is_real_regular(f"{assoc}/{fname}"):
            return False
    return True


def _checkpoint_complete(checkpoints_dir, generation):
    """True when the pointed generation has meta and all persona memory.

    Every required directory is verified by lstat to be a real directory and
    every required file to be a real regular file, so a symlink at any
    required path (dangling or not) is rejected instead of followed. The
    generation meta must parse as a JSON object with valid persona names.
    """
    gen_dir = f"{checkpoints_dir}/{generation}"
    if not is_real_dir(gen_dir):
        return False
    meta_file = f"{gen_dir}/meta.json"
    if not is_real_regular(meta_file):
        return False
    try:
        meta = read_json_beneath(checkpoints_dir, (generation, "meta.json"))
    except (OSError, ValueError):
        return False
    if not isinstance(meta, dict):
        return False
    try:
        _validate_reverie_meta(meta)
    except ValueError:
        return False
    persona_names = meta["persona_names"]
    personas_dir = f"{gen_dir}/personas"
    if not is_real_dir(personas_dir):
        return False
    for name in persona_names:
        persona_dir = f"{personas_dir}/{name}"
        if not is_real_dir(persona_dir):
            return False
        if not _bootstrap_memory_complete(f"{persona_dir}/bootstrap_memory"):
            return False
    return True


def _checkpoint_ancestors_real(sim_folder):
    """Validate the checkpoint ancestor chain; return the checkpoints dir.

    ``sim_folder``, ``sim_folder/reverie``, and
    ``sim_folder/reverie/checkpoints`` must each be a real lstat directory
    when present. A missing level means the simulation uses the legacy
    layout and ``None`` is returned. A symlink or any other non-directory at
    any level raises ``ValueError`` so the caller fails closed instead of
    following the link (or falling back to the legacy layout) and reading
    outside the simulation root.
    """
    for label, path in (
        ("simulation root", sim_folder),
        ("reverie directory", f"{sim_folder}/reverie"),
        ("checkpoints directory", f"{sim_folder}/reverie/checkpoints"),
    ):
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ValueError(
                f"Cannot lstat {label} '{path}': {exc}."
            ) from exc
        if not stat.S_ISDIR(st.st_mode):
            raise ValueError(
                f"Refusing checkpoint access: {label} at '{path}' is not a "
                f"real directory (symlink or special file)."
            )
    return f"{sim_folder}/reverie/checkpoints"


def _load_checkpoint(sim_folder):
    """Load the committed checkpoint for ``sim_folder``.

    Returns a ``_Checkpoint`` when a valid pointer exists, or ``None`` when
    the simulation uses the legacy layout (no pointer). The simulation root,
    reverie directory, and checkpoints directory must each be a real lstat
    directory when present; a symlinked ancestor raises ``ValueError``
    instead of falling back to the legacy layout or reading through the
    link. The pointer must be a real regular file, never a symlink: a
    dangling or non-dangling symlinked pointer, or any other non-regular
    pointer, raises ``ValueError`` instead of falling back to the legacy
    layout. An invalid pointer, unsafe generation name, or incomplete
    pointed generation raises ``ValueError`` so the caller fails closed
    instead of silently combining generations.
    """
    checkpoints_dir = _checkpoint_ancestors_real(sim_folder)
    if checkpoints_dir is None:
        return None
    try:
        pointer = read_json_beneath(
            sim_folder, ("reverie", "checkpoints", "pointer.json")
        )
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"Invalid checkpoint pointer at "
            f"'{checkpoints_dir}/pointer.json': {exc}."
        ) from exc
    if not isinstance(pointer, dict):
        raise ValueError(
            f"Invalid checkpoint pointer at '{checkpoints_dir}/pointer.json': "
            f"expected a JSON object."
        )
    version = pointer.get("version", _POINTER_VERSION)
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != _POINTER_VERSION
    ):
        raise ValueError(
            f"Unsupported checkpoint pointer version {version!r} at "
            f"'{checkpoints_dir}/pointer.json'."
        )
    generation = pointer.get("generation")
    _validate_generation_name(generation)
    if not _checkpoint_complete(checkpoints_dir, generation):
        raise ValueError(
            f"Checkpoint generation {generation!r} is incomplete or "
            f"missing at '{checkpoints_dir}'."
        )
    try:
        meta = read_json_beneath(
            sim_folder,
            ("reverie", "checkpoints", generation, "meta.json"),
        )
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"Checkpoint generation {generation!r} meta.json is invalid: "
            f"{exc}."
        ) from exc
    if not isinstance(meta, dict):
        raise ValueError(
            f"Checkpoint generation {generation!r} meta.json is not a "
            f"JSON object."
        )
    return _Checkpoint(generation=generation, meta=meta)


def _next_generation(checkpoints_dir):
    """Return the next zero-padded generation name for ``checkpoints_dir``."""
    max_gen = 0
    if os.path.isdir(checkpoints_dir):
        for entry in os.listdir(checkpoints_dir):
            if _SAFE_GENERATION_RE.fullmatch(entry):
                try:
                    max_gen = max(max_gen, int(entry))
                except ValueError:
                    pass
    return f"{max_gen + 1:010d}"


def _cleanup_old_generations(checkpoints_dir, keep=2):
    """Remove old generations, retaining the ``keep`` most recent.

    Only called after the pointer commit, so the pointed generation is always
    the newest and is never removed. At least the previous generation is
    retained for recovery and debugging.
    """
    if not os.path.isdir(checkpoints_dir):
        return
    entries = [
        e for e in os.listdir(checkpoints_dir) if _SAFE_GENERATION_RE.fullmatch(e)
    ]
    entries.sort(key=int, reverse=True)
    for entry in entries[keep:]:
        shutil.rmtree(os.path.join(checkpoints_dir, entry))


def _remove_tree(path):
    """Remove ``path`` whether it is a directory, a file, or missing."""
    if os.path.isdir(path):
        shutil.rmtree(path)
    elif os.path.exists(path):
        os.remove(path)


def _clean_stale_checkpoint_staging(sim_folder):
    """Remove leftover checkpoint staging dirs from an interrupted save."""
    checkpoints_dir = f"{sim_folder}/reverie/checkpoints"
    if not os.path.isdir(checkpoints_dir):
        return
    staging = f"{checkpoints_dir}/{_CHECKPOINT_STAGING}"
    _remove_tree(staging)


def _open_real_dir(directory):
    """Open ``directory`` as a real directory or return ``None``.

    The directory is opened with ``O_DIRECTORY | O_NOFOLLOW`` so a symlink
    (dangling or not) or a non-directory at the path is refused instead of
    followed. Missing or unopenable paths return ``None``.
    """
    dir_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        dir_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        dir_flags |= os.O_NOFOLLOW
    try:
        return os.open(directory, dir_flags)
    except OSError:
        return None


def _iter_numeric_json_fd(fd, *, max_digits=_MAX_STEP_DIGITS):
    """Yield ``(digits, name)`` for canonical numeric JSON regular entries.

    Only basenames matching ``0.json`` or a nonzero decimal with no leading
    zero are considered, with at most ``max_digits`` digits and a numeric
    value at most ``_MAX_STEP``, so an oversized name is rejected before it
    is ever yielded, pruned, or read. Entries are stat'ed relative to the
    held descriptor with ``follow_symlinks=False``; symlinks, directories,
    FIFOs, and other special files are ignored, and an entry that changes
    between listing and stat is skipped safely.
    """
    try:
        entries = os.listdir(fd)
    except OSError:
        return
    for entry in entries:
        match = _NUMERIC_JSON_RE.fullmatch(entry)
        if not match:
            continue
        digits = match.group(1)
        if len(digits) > max_digits:
            continue
        if int(digits) > _MAX_STEP:
            continue
        try:
            st = os.stat(entry, dir_fd=fd, follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        yield digits, entry


def _enumerate_numeric_json(directory, *, max_digits=_MAX_STEP_DIGITS):
    """Yield ``(digits, name)`` for canonical numeric JSON regular files.

    The directory is opened with ``O_DIRECTORY | O_NOFOLLOW`` and entries
    are listed through the descriptor, so a symlinked directory is rejected
    and enumeration never escapes the root. Only entries matching ``0.json``
    or a nonzero decimal with no leading zero are considered, with at most
    ``max_digits`` digits and a numeric value at most ``_MAX_STEP``, and
    only real regular files are yielded; symlinks, directories, FIFOs, and
    other special files are ignored. The descriptor is closed on every exit
    path.
    """
    fd = _open_real_dir(directory)
    if fd is None:
        return
    try:
        yield from _iter_numeric_json_fd(fd, max_digits=max_digits)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _read_snapshot_json(sim_folder, filename):
    """Read a canonical snapshot JSON beneath ``sim_folder``, or ``None``.

    The basename must be canonical ``<n>.json`` with at most
    ``_MAX_STEP_DIGITS`` digits and a numeric value at most ``_MAX_STEP``;
    anything else (noncanonical stems, leading zeros, or oversized numbers)
    is rejected before any open. The file is read descriptor-relative from
    the simulation root: the root and the ``environment`` parent are opened
    with ``O_DIRECTORY | O_NOFOLLOW`` via ``dir_fd`` and the final leaf with
    ``O_NOFOLLOW`` plus ``O_NONBLOCK`` when available, so a symlinked
    environment directory or a symlink swapped in at any level is refused by
    the kernel instead of followed. The opened descriptor is fstat'ed and
    must be a regular file before ``json.load`` runs, so an inode replaced
    by a non-regular one inside the race window is never read. Every
    descriptor is closed on every exit path, including parse failures.
    Returns ``None`` for missing, unopenable, non-regular, or malformed
    inputs.
    """
    match = _NUMERIC_JSON_RE.fullmatch(filename)
    if not match or len(match.group(1)) > _MAX_STEP_DIGITS:
        return None
    if int(match.group(1)) > _MAX_STEP:
        return None
    try:
        return read_json_beneath(sim_folder, ("environment", filename))
    except (OSError, ValueError):
        return None


def _prune_numeric_json(directory, threshold):
    """Remove ``<n>.json`` files with ``n >= threshold`` in ``directory``.

    The directory is opened with ``O_DIRECTORY | O_NOFOLLOW`` and every
    canonical candidate is verified through the held descriptor with
    ``follow_symlinks=False`` before it is unlinked with ``dir_fd``, so a
    symlinked or swapped directory never deletes outside the simulation
    root. Only canonical regular files (``0.json`` or a nonzero decimal with
    no leading zero, at most ``_MAX_STEP_DIGITS`` digits and a numeric value
    at most ``_MAX_STEP``) are considered; symlinks and oversized names are
    never followed or removed. A file that cannot be removed raises
    ``OSError`` with the offending path in the message.
    """
    fd = _open_real_dir(directory)
    if fd is None:
        return
    try:
        for digits, name in _iter_numeric_json_fd(fd):
            if int(digits) < threshold:
                continue
            try:
                os.unlink(name, dir_fd=fd)
            except OSError as exc:
                raise OSError(
                    f"Failed to prune stale artifact "
                    f"'{directory}/{name}': {exc}."
                ) from exc
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _prune_uncommitted_artifacts(sim_folder, step):
    """Prune uncommitted movement and environment artifacts at ``step``.

    The committed checkpoint step is authoritative. Movement files at or
    beyond the committed step are stale because the backend recomputes them
    when the matching environment input arrives. Environment files beyond the
    committed step are stale future input that must not be consumed
    automatically after replay. The environment at the committed step is
    preserved as the input to process or replay, and all committed movement
    below the step is preserved. Non-numeric and unrelated files are ignored.
    """
    _validate_step(step)
    _prune_numeric_json(f"{sim_folder}/movement", step)
    _prune_numeric_json(f"{sim_folder}/environment", step + 1)


def _sync_legacy_personas_from_checkpoint(sim_folder, generation):
    """Copy the pointed generation's personas into the legacy personas dir.

    Used when forking from an origin with a committed checkpoint so the new
    target's legacy layout matches the pointed state instead of stale legacy
    state. Both paths stay inside ``sim_folder``. The copy preserves
    symlinks as symlinks (``symlinks=True``) so a symlink is never
    dereferenced into the staged tree; the staged regular-tree validation
    rejects any surviving symlink before publish.
    """
    src = f"{sim_folder}/reverie/checkpoints/{generation}/personas"
    dst = f"{sim_folder}/personas"
    if not is_real_dir(src):
        return
    if is_real_dir(dst):
        shutil.rmtree(dst)
    elif os.path.lexists(dst):
        os.remove(dst)
    shutil.copytree(src, dst, symlinks=True)


def _copy_origin_tree(src, dst):
    """Copy the origin tree into ``dst`` preserving symlinks as symlinks.

    ``shutil.copytree(..., symlinks=True)`` never dereferences a symlink, so
    a pre-existing or race-introduced source symlink stays a symlink in the
    staged copy instead of publishing the linked outside bytes. The staged
    regular-tree validation rejects any surviving symlink before publish.
    """
    shutil.copytree(src, dst, symlinks=True)


def _validate_regular_tree(root):
    """Validate ``root`` contains only real directories and regular files.

    Every entry beneath ``root`` is checked with ``lstat`` so a symlink
    (dangling or not), FIFO, socket, device, or other special file is
    rejected with ``ValueError`` before any publish. Missing or unreadable
    entries fail closed too.
    """
    try:
        root_st = os.lstat(root)
    except OSError as exc:
        raise ValueError(
            f"Cannot validate staged fork root '{root}': {exc}."
        ) from exc
    if not stat.S_ISDIR(root_st.st_mode):
        raise ValueError(
            f"Staged fork root '{root}' is not a real directory."
        )
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            try:
                entry_st = os.lstat(path)
            except OSError as exc:
                raise ValueError(
                    f"Cannot validate staged fork entry '{path}': {exc}."
                ) from exc
            if stat.S_ISLNK(entry_st.st_mode):
                raise ValueError(
                    f"Refusing staged fork symlink at '{path}'."
                )
            if not (
                stat.S_ISDIR(entry_st.st_mode)
                or stat.S_ISREG(entry_st.st_mode)
            ):
                raise ValueError(
                    f"Refusing staged fork special file at '{path}'."
                )


def _fsync_staged_tree(root):
    """fsync every regular file and directory under ``root`` bottom-up.

    Each regular file is fsynced through a checked ``O_NOFOLLOW`` descriptor
    and every directory is fsynced only after its own contents, so a rename
    of ``root`` is durable where the filesystem supports it. Any failure
    raises ``OSError`` with the offending path.
    """
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            fsync_regular(os.path.join(dirpath, name))
        for name in dirnames:
            fsync_dir_checked(os.path.join(dirpath, name))
    fsync_dir_checked(root)


def _validate_staged_fork(staging):
    """Fully validate a staged fresh-fork simulation before publication.

    The authoritative staged ``reverie/meta.json`` must be a JSON object
    with every required field of the right type: ``fork_sim_code`` must be a
    safe simulation code, ``start_date`` and ``curr_time`` must parse with
    the canonical month names, ``sec_per_step`` must be a positive non-bool
    finite number, ``maze_name`` must construct a loadable ``Maze``,
    ``persona_names`` must be safe unique names, and ``step`` must be a
    bounded integer. When the staged copy carries a checkpoint pointer, the
    pointed generation must be complete and every persona is loaded from it;
    otherwise every persona needs a complete legacy bootstrap memory. Every
    persona is then loaded descriptor-safely, and an environment snapshot at
    or below the committed step within the maze bounds must resolve. Any
    ordinary persona-loader shape failure (``KeyError``, ``TypeError``,
    ``AttributeError``, ``IndexError``, or any other ``Exception`` short of
    ``KeyboardInterrupt``/``SystemExit``) is converted to a sanitized
    ``ValueError``, so a missing or malformed scratch/associative/spatial
    structure never tracebacks from the CLI. Any failure raises
    ``ValueError`` before the staged tree is fsynced or published, so the
    caller's ``finally`` cleanup removes only this invocation's staging and
    the origin is never modified.
    """
    meta_file = f"{staging}/reverie/meta.json"
    try:
        meta = read_json_beneath(staging, ("reverie", "meta.json"))
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"Staged fork meta.json at '{meta_file}' is invalid: {exc}."
        ) from exc
    _validate_reverie_meta(meta)

    try:
        maze = Maze(meta["maze_name"])
    except Exception as exc:
        raise ValueError(
            f"Cannot load maze {meta['maze_name']!r} for staged fork: "
            f"{exc}."
        ) from exc

    staged_checkpoint = _load_checkpoint(staging)
    if staged_checkpoint is not None:
        personas_root = (
            f"{staging}/reverie/checkpoints/"
            f"{staged_checkpoint.generation}/personas"
        )
    else:
        personas_root = f"{staging}/personas"
        for name in meta["persona_names"]:
            base = f"{personas_root}/{name}/bootstrap_memory"
            if not _bootstrap_memory_complete(base):
                raise ValueError(
                    f"Staged fork persona {name!r} has incomplete legacy "
                    f"bootstrap memory at '{base}'."
                )

    for name in meta["persona_names"]:
        folder = f"{personas_root}/{name}"
        try:
            Persona(name, folder)
        except Exception as exc:
            raise ValueError(
                f"Cannot load persona {name!r} from staged fork "
                f"'{folder}': {exc}."
            ) from exc

    try:
        _resolve_environment_snapshot(
            staging,
            meta["step"],
            meta["persona_names"],
            maze.maze_width,
            maze.maze_height,
        )
    except FileNotFoundError as exc:
        raise ValueError(str(exc)) from exc


class HeartbeatPublicationError(RuntimeError):
    """Raised when a heartbeat write fails; the message is always sanitized."""


def _sanitize_heartbeat_error(exc):
    """Return a sanitized heartbeat failure message without details."""
    return f"Heartbeat publication failed ({exc.__class__.__name__})."


def _wait_until_deadline(event, deadline):
    """Wait until the monotonic ``deadline`` or until ``event`` is set.

    The wait runs in finite bounded chunks of ``_PACE_WAIT_CHUNK_SECONDS``,
    rechecking the monotonic clock and the shutdown flag after each chunk,
    so an infinite deadline (the reciprocal of the parser-valid minimum
    positive finite rate) never overflows the event wait and a shutdown
    interrupts promptly.
    """
    while not event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        event.wait(min(remaining, _PACE_WAIT_CHUNK_SECONDS))


class ReverieServer:
    def __init__(
        self, fork_sim_code, sim_code, *, resume=False, publish_markers=False
    ):
        _validate_sim_code(sim_code, label="target simulation code")
        if fork_sim_code:
            _validate_sim_code(fork_sim_code, label="origin simulation code")
        self.sim_code = sim_code
        sim_folder = f"{fs_storage}/{self.sim_code}"

        _checkpoint_ancestors_real(sim_folder)

        target_exists = is_real_dir(sim_folder)
        checkpoint = _load_checkpoint(sim_folder) if target_exists else None
        self._checkpoint_generation = None

        if resume and target_exists:
            if checkpoint is not None:
                print(f"[reverie] Resuming existing simulation: {self.sim_code}")
                reverie_meta = checkpoint.meta
                self._checkpoint_generation = checkpoint.generation
            else:
                if not _is_complete_sim(sim_folder):
                    raise ValueError(
                        f"Simulation target '{sim_folder}' exists but is "
                        f"incomplete (missing or invalid reverie/meta.json). "
                        f"Cannot resume a partial simulation."
                    )
                print(f"[reverie] Resuming existing simulation: {self.sim_code}")
                try:
                    reverie_meta = read_json_beneath(
                        sim_folder, ("reverie", "meta.json")
                    )
                except (OSError, ValueError) as exc:
                    raise ValueError(
                        f"Simulation target '{sim_folder}' reverie/meta.json "
                        f"is invalid: {exc}."
                    ) from exc
            self.fork_sim_code = (
                reverie_meta.get("fork_sim_code") or fork_sim_code
            )
        elif target_exists:
            if checkpoint is not None or _is_complete_sim(sim_folder):
                raise FileExistsError(
                    f"Simulation target '{sim_folder}' already exists. "
                    f"Use --resume to restart from existing state, or choose a "
                    f"different --target name for a fresh fork."
                )
            raise ValueError(
                f"Simulation target '{sim_folder}' exists but is incomplete "
                f"(missing or invalid reverie/meta.json). Refusing to delete "
                f"an existing target; choose a different --target name for a "
                f"fresh fork."
            )
        else:
            if resume:
                print(
                    f"[reverie] --resume specified but target '{sim_folder}' "
                    f"does not exist. Forking fresh from '{fork_sim_code}'."
                )
            self.fork_sim_code = fork_sim_code
            reverie_meta = self._fork_fresh(sim_folder)

        _validate_reverie_meta(reverie_meta)
        if self.fork_sim_code:
            _validate_sim_code(
                self.fork_sim_code, label="origin simulation code"
            )
        ensure_dir_beneath(sim_folder, ("movement",))
        ensure_dir_beneath(sim_folder, ("environment",))
        _ensure_dir(fs_temp_storage)
        _clean_stale_checkpoint_staging(sim_folder)

        self.start_time = datetime.datetime.strptime(
            f"{reverie_meta['start_date']}, 00:00:00",
            "%B %d, %Y, %H:%M:%S",
        )
        self.curr_time = datetime.datetime.strptime(
            reverie_meta["curr_time"], "%B %d, %Y, %H:%M:%S"
        )
        self.sec_per_step = reverie_meta["sec_per_step"]

        try:
            self.maze = Maze(reverie_meta["maze_name"])
        except Exception as exc:
            raise ValueError(
                f"Cannot load maze {reverie_meta['maze_name']!r}: {exc}."
            ) from exc
        self.step = _validate_step(reverie_meta["step"])

        _prune_uncommitted_artifacts(sim_folder, self.step)

        self.personas = dict()
        self.personas_tile = dict()

        init_env = _resolve_environment_snapshot(
            sim_folder,
            self.step,
            reverie_meta["persona_names"],
            self.maze.maze_width,
            self.maze.maze_height,
        )
        for persona_name in reverie_meta["persona_names"]:
            if self._checkpoint_generation is not None:
                persona_folder = (
                    f"{sim_folder}/reverie/checkpoints/"
                    f"{self._checkpoint_generation}/personas/{persona_name}"
                )
            else:
                persona_folder = f"{sim_folder}/personas/{persona_name}"
            p_x = init_env[persona_name]["x"]
            p_y = init_env[persona_name]["y"]
            try:
                curr_persona = Persona(persona_name, persona_folder)
            except Exception as exc:
                raise ValueError(
                    f"Cannot load persona {persona_name!r} from "
                    f"'{persona_folder}': {exc}."
                ) from exc

            self.personas[persona_name] = curr_persona
            self.personas_tile[persona_name] = (p_x, p_y)
            self.maze.tiles[p_y][p_x]["events"].add(
                curr_persona.scratch.get_curr_event_and_desc()
            )

        self.server_sleep = 0.1

        self._state_lock = threading.Lock()
        self._heartbeat_thread = None
        self._heartbeat_stop = None
        self._heartbeat_interval = None
        self._heartbeat_failure = None

        if publish_markers:
            self.publish_runtime_markers()

    def publish_runtime_markers(self):
        """Atomically publish the curr_sim_code and curr_step markers.

        Called explicitly by interactive mode before ``open_server`` and by
        service mode only after provider validation succeeds, so a failed
        init or validation never leaves a fresh marker for the frontend. The
        sim and step markers are published as one critical section under the
        runtime lock so a concurrent clear cannot interleave between them.
        """
        with runtime_lock(_runtime_lock_path()):
            atomic_write_json_beneath(
                fs_temp_storage,
                (),
                "curr_sim_code.json",
                {"sim_code": self.sim_code},
            )
            atomic_write_json_beneath(
                fs_temp_storage,
                (),
                "curr_step.json",
                {"step": self.step},
            )

    def _publish_curr_step(self):
        """Publish the current step marker under the runtime lock.

        The per-step publication shares the runtime authority lock with
        marker clear and publish so a restart clear can never interleave
        with a step update.
        """
        with runtime_lock(_runtime_lock_path()):
            atomic_write_json_beneath(
                fs_temp_storage, (), "curr_step.json", {"step": self.step}
            )

    def _fork_fresh(self, sim_folder):
        """Fork a fresh simulation from the origin into ``sim_folder``.

        Validates the origin, copies it into an unpredictable unique staging
        sibling (a fresh UUID per invocation), rewrites the staging meta
        atomically, fully validates the staged simulation (meta fields and
        dates, step bound, maze loadability, persona names, pointed
        checkpoint or legacy bootstrap memory, descriptor-safe persona
        loading, and an environment snapshot within maze bounds), fsyncs the
        staged tree, then atomically renames the staging directory into the
        final target. A leftover legacy deterministic staging sibling from an
        interrupted old-version fork is cleaned first; a concurrent fork's
        unique staging is never touched. The final target only ever appears
        via the atomic no-replace rename, so a partial target is never
        accepted as complete, and only this invocation's staging is removed
        on failure. Returns the forked meta dict.

        When the origin has a committed checkpoint, the fork copies the
        pointed latest state (including the checkpoint pointer) and syncs the
        legacy personas to that state, so the new target never inherits stale
        legacy state. The immediate origin code is recorded atomically in
        both the staging legacy ``reverie/meta.json`` and the pointed
        generation ``reverie/checkpoints/<generation>/meta.json``, so a
        resume reads the immediate origin lineage from the authoritative
        checkpoint. Legacy seeds without a checkpoint are copied unchanged.
        """
        fork_folder = f"{fs_storage}/{self.fork_sim_code}"
        if os.path.lexists(fork_folder):
            if not is_real_dir(fork_folder):
                raise ValueError(
                    f"Fork origin '{fork_folder}' is not a real directory "
                    f"(symlink or special file)."
                )
        else:
            raise FileNotFoundError(
                f"Fork origin '{fork_folder}' does not exist or is not a "
                f"directory."
            )

        dir_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            dir_flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            dir_flags |= os.O_NOFOLLOW
        origin_fd = os.open(fork_folder, dir_flags)
        try:
            origin_st = os.fstat(origin_fd)
            if not stat.S_ISDIR(origin_st.st_mode):
                raise ValueError(
                    f"Fork origin '{fork_folder}' is not a real directory "
                    f"(symlink or special file)."
                )

            _validate_regular_tree(fork_folder)

            origin_checkpoint = _load_checkpoint(fork_folder)
            if origin_checkpoint is not None:
                origin_meta = origin_checkpoint.meta
            else:
                try:
                    origin_meta = read_json_beneath(
                        fork_folder, ("reverie", "meta.json")
                    )
                except FileNotFoundError:
                    raise FileNotFoundError(
                        f"Fork origin '{fork_folder}' has no "
                        f"reverie/meta.json."
                    ) from None
                except (OSError, ValueError) as exc:
                    raise ValueError(
                        f"Fork origin '{fork_folder}' reverie/meta.json is "
                        f"invalid: {exc}."
                    ) from exc
                if not isinstance(origin_meta, dict):
                    raise ValueError(
                        f"Fork origin '{fork_folder}' reverie/meta.json is "
                        f"not a JSON object."
                    )

            _validate_reverie_meta(origin_meta)

            legacy_staging = f"{sim_folder}.staging"
            if os.path.isdir(legacy_staging) or os.path.exists(legacy_staging):
                print(
                    f"[reverie] Cleaning stale staging path "
                    f"'{legacy_staging}'."
                )
                _remove_tree(legacy_staging)

            staging = f"{sim_folder}.staging-{uuid.uuid4().hex}"

            try:
                _copy_origin_tree(fork_folder, staging)

                try:
                    current_st = os.lstat(fork_folder)
                except OSError as exc:
                    raise ValueError(
                        f"Fork origin '{fork_folder}' vanished during copy: "
                        f"{exc}."
                    ) from exc
                if (
                    current_st.st_dev,
                    current_st.st_ino,
                ) != (origin_st.st_dev, origin_st.st_ino):
                    raise ValueError(
                        f"Fork origin '{fork_folder}' was replaced during "
                        f"copy; refusing to publish a target from a "
                        f"different tree."
                    )

                pointed_meta_file = None
                if origin_checkpoint is not None:
                    _sync_legacy_personas_from_checkpoint(
                        staging, origin_checkpoint.generation
                    )
                    _validate_generation_name(origin_checkpoint.generation)
                    pointed_meta_file = (
                        f"{staging}/reverie/checkpoints/"
                        f"{origin_checkpoint.generation}/meta.json"
                    )
                    if not is_real_regular(pointed_meta_file):
                        raise ValueError(
                            f"Fork origin '{fork_folder}' checkpoint "
                            f"generation {origin_checkpoint.generation!r} "
                            f"has no meta.json at '{pointed_meta_file}'."
                        )

                origin_meta["fork_sim_code"] = self.fork_sim_code
                atomic_write_json(f"{staging}/reverie/meta.json", origin_meta)
                if pointed_meta_file is not None:
                    atomic_write_json(pointed_meta_file, origin_meta)

                _validate_regular_tree(staging)
                _validate_staged_fork(staging)
                _fsync_staged_tree(staging)

                if os.path.exists(sim_folder):
                    raise FileExistsError(
                        f"Simulation target '{sim_folder}' already exists. "
                        f"Refusing to overwrite a concurrent target."
                    )

                try:
                    rename_no_replace(staging, sim_folder)
                except FileExistsError as exc:
                    raise FileExistsError(
                        f"Simulation target '{sim_folder}' appeared during "
                        f"fork. Refusing to overwrite a concurrent target. "
                        f"({exc})"
                    ) from exc
                try:
                    fsync_dir_checked(fs_storage)
                except OSError as exc:
                    _remove_tree(sim_folder)
                    raise OSError(
                        f"Failed to fsync the simulation parent directory "
                        f"'{fs_storage}': {exc}."
                    ) from exc
            finally:
                _remove_tree(staging)

        finally:
            os.close(origin_fd)

        self._checkpoint_generation = (
            origin_checkpoint.generation
            if origin_checkpoint is not None
            else None
        )
        return origin_meta

    def save(self):
        """Save all Reverie progress as a crash-consistent checkpoint.

        A complete generation (meta plus every persona's bootstrap memory) is
        built in same-filesystem staging, fsynced through the atomic helpers,
        atomically renamed into place, then committed by atomically publishing
        a small checkpoint pointer. Only after the pointer commit are old
        generations cleaned (retaining the previous one), and the legacy
        ``reverie/meta.json`` is updated atomically as compatibility metadata.
        An interruption before the pointer commit leaves the prior committed
        generation authoritative and recoverable.
        """
        sim_folder = f"{fs_storage}/{self.sim_code}"
        if not is_real_dir(sim_folder):
            raise ValueError(
                f"Simulation root '{sim_folder}' is not a real directory "
                f"(symlink or special file)."
            )
        _checkpoint_ancestors_real(sim_folder)

        reverie_meta = dict()
        reverie_meta["fork_sim_code"] = self.fork_sim_code
        reverie_meta["start_date"] = self.start_time.strftime("%B %d, %Y")
        reverie_meta["curr_time"] = self.curr_time.strftime(
            "%B %d, %Y, %H:%M:%S"
        )
        reverie_meta["sec_per_step"] = self.sec_per_step
        reverie_meta["maze_name"] = self.maze.maze_name
        reverie_meta["persona_names"] = list(self.personas.keys())
        reverie_meta["step"] = _validate_step(self.step)

        checkpoints_dir = f"{sim_folder}/reverie/checkpoints"
        _ensure_dir(checkpoints_dir)
        if not is_real_dir(checkpoints_dir):
            raise ValueError(
                f"Checkpoints directory '{checkpoints_dir}' is not a real "
                f"directory (symlink or special file)."
            )

        for persona_name in self.personas:
            if not _safe_persona_name(persona_name):
                raise ValueError(
                    f"Unsafe persona name {persona_name!r}; refusing to "
                    f"save a checkpoint."
                )

        staging = f"{checkpoints_dir}/{_CHECKPOINT_STAGING}"
        if os.path.isdir(staging):
            shutil.rmtree(staging)
        elif os.path.exists(staging):
            os.remove(staging)

        generation = _next_generation(checkpoints_dir)

        _ensure_dir(staging)
        atomic_write_json(f"{staging}/meta.json", reverie_meta)
        for persona_name, persona in self.personas.items():
            save_folder = (
                f"{staging}/personas/{persona_name}/bootstrap_memory"
            )
            persona.save(save_folder)

        fsync_dir(staging)

        final = f"{checkpoints_dir}/{generation}"
        os.replace(staging, final)
        fsync_dir(checkpoints_dir)

        atomic_write_json(
            f"{checkpoints_dir}/pointer.json",
            {"version": _POINTER_VERSION, "generation": generation},
        )

        _cleanup_old_generations(checkpoints_dir, keep=2)

        atomic_write_json(f"{sim_folder}/reverie/meta.json", reverie_meta)

    def start_path_tester_server(self):
        """Starts the path tester server."""
        def print_tree(tree):
            def _print_tree(tree, depth):
                dash = " >" * depth

                if type(tree) == type(list()):
                    if tree:
                        print(dash, tree)
                    return

                for key, val in tree.items():
                    if key:
                        print(dash, key)
                    _print_tree(val, depth + 1)

            _print_tree(tree, 0)

        curr_vision = 8
        s_mem = dict()

        while True:
            try:
                curr_dict = {}
                tester_file = fs_temp_storage + "/path_tester_env.json"
                if check_if_file_exists(tester_file):
                    with open(tester_file) as json_file:
                        curr_dict = json.load(json_file)
                        os.remove(tester_file)

                    curr_sts = self.maze.sq_tile_size
                    curr_camera = (
                        int(math.ceil(curr_dict["x"] / curr_sts)),
                        int(math.ceil(curr_dict["y"] / curr_sts)) + 1,
                    )
                    curr_tile_det = self.maze.access_tile(curr_camera)

                    world = curr_tile_det["world"]
                    if curr_tile_det["world"] not in s_mem:
                        s_mem[world] = dict()

                    nearby_tiles = self.maze.get_nearby_tiles(
                        curr_camera, curr_vision
                    )
                    for i in nearby_tiles:
                        i_det = self.maze.access_tile(i)
                        if (
                            curr_tile_det["sector"] == i_det["sector"]
                            and curr_tile_det["arena"] == i_det["arena"]
                        ):
                            if i_det["sector"] != "":
                                if i_det["sector"] not in s_mem[world]:
                                    s_mem[world][i_det["sector"]] = dict()
                            if i_det["arena"] != "":
                                if (
                                    i_det["arena"]
                                    not in s_mem[world][i_det["sector"]]
                                ):
                                    s_mem[world][i_det["sector"]][
                                        i_det["arena"]
                                    ] = list()
                            if i_det["game_object"] != "":
                                if (
                                    i_det["game_object"]
                                    not in s_mem[world][i_det["sector"]][
                                        i_det["arena"]
                                    ]
                                ):
                                    s_mem[world][i_det["sector"]][
                                        i_det["arena"]
                                    ] += [i_det["game_object"]]

                print("= " * 15)
                _ensure_dir(fs_temp_storage)
                out_file = fs_temp_storage + "/path_tester_out.json"
                with open(out_file, "w") as outfile:
                    outfile.write(json.dumps(s_mem, indent=2))
                print_tree(s_mem)

            except:
                pass

            time.sleep(self.server_sleep * 10)

    def start_server(self, int_counter):
        """The main backend server of Reverie."""
        sim_folder = f"{fs_storage}/{self.sim_code}"

        game_obj_cleanup = dict()
        warned_env_files = set()

        while True:
            if int_counter == 0:
                break
            if self.step >= _MAX_STEP:
                break

            curr_env_file = f"{sim_folder}/environment/{self.step}.json"
            new_env = _read_snapshot_json(sim_folder, f"{self.step}.json")
            if new_env is not None:
                if not _validate_env_snapshot(
                    new_env, self.personas, self.maze
                ):
                    if curr_env_file not in warned_env_files:
                        warned_env_files.add(curr_env_file)
                        print(
                            f"[reverie] Ignoring invalid environment "
                            f"snapshot {curr_env_file}; waiting for "
                            f"corrected input."
                        )
                    time.sleep(self.server_sleep)
                    continue

                for key, val in game_obj_cleanup.items():
                    self.maze.turn_event_from_tile_idle(key, val)
                game_obj_cleanup = dict()

                for persona_name, persona in self.personas.items():
                    curr_tile = self.personas_tile[persona_name]
                    new_tile = (
                        new_env[persona_name]["x"],
                        new_env[persona_name]["y"],
                    )

                    self.personas_tile[persona_name] = new_tile
                    self.maze.remove_subject_events_from_tile(
                        persona.name, curr_tile
                    )
                    self.maze.add_event_from_tile(
                        persona.scratch.get_curr_event_and_desc(),
                        new_tile,
                    )

                    if not persona.scratch.planned_path:
                        game_obj_cleanup[
                            persona.scratch.get_curr_obj_event_and_desc()
                        ] = new_tile
                        self.maze.add_event_from_tile(
                            persona.scratch.get_curr_obj_event_and_desc(),
                            new_tile,
                        )
                        blank = (
                            persona.scratch.get_curr_obj_event_and_desc()[0],
                            None,
                            None,
                            None,
                        )
                        self.maze.remove_event_from_tile(blank, new_tile)

                movements = {"persona": dict(), "meta": dict()}
                for persona_name, persona in self.personas.items():
                    (
                        next_tile,
                        pronunciatio,
                        description,
                    ) = persona.move(
                        self.maze,
                        self.personas,
                        self.personas_tile[persona_name],
                        self.curr_time,
                    )
                    movements["persona"][persona_name] = {}
                    movements["persona"][persona_name][
                        "movement"
                    ] = next_tile
                    movements["persona"][persona_name][
                        "pronunciatio"
                    ] = pronunciatio
                    movements["persona"][persona_name][
                        "description"
                    ] = description
                    movements["persona"][persona_name][
                        "chat"
                    ] = persona.scratch.chat

                movements["meta"][
                    "curr_time"
                ] = self.curr_time.strftime("%B %d, %Y, %H:%M:%S")

                atomic_write_json_beneath(
                    sim_folder,
                    ("movement",),
                    f"{self.step}.json",
                    movements,
                )

                self._advance_step_time()

                self._publish_curr_step()
                _prune_uncommitted_artifacts(sim_folder, self.step)
                self._publish_heartbeat()

                int_counter -= 1

            time.sleep(self.server_sleep)

    def serve_forever(
        self, autosave_interval: int = 10, heartbeat_interval: float = 5.0
    ):
        """
        Service mode: continuously process simulation steps as the frontend
        writes env files. Autosaves every N steps.

        Blocks until interrupted. SIGTERM (container shutdown) and Ctrl+C
        (SIGINT) both request a cooperative shutdown: the current step is
        finished, then a final save runs before exit. A heartbeat file is
        published atomically at startup and then by a dedicated background
        thread at ``heartbeat_interval``, so freshness is preserved even
        while a step blocks on the LLM. The background publisher is stopped
        and joined on every exit path.

        Valid environment steps are rate-limited: the next valid step starts
        no sooner than ``1 / max_steps_per_second`` wall-clock seconds after
        the previous successful step completed, so slow LLM work naturally
        lowers the throughput.
        Missing or invalid environment input keeps the
        short idle polling and never consumes a rate window. The pacing wait
        runs through ``shutdown_event`` so a signal interrupts it and the
        normal final-save exit still runs. The wait is split into finite
        bounded chunks so a rate whose reciprocal overflows (the parser-valid
        minimum subnormal) never passes infinity to the event wait.
        """
        sim_folder = f"{fs_storage}/{self.sim_code}"
        game_obj_cleanup = dict()
        steps_since_save = 0
        step_count = 0

        print(f"[reverie] Service mode started for sim_code={self.sim_code}")
        print(f"[reverie] Watching {sim_folder}/environment/")
        print(f"[reverie] Autosave every {autosave_interval} steps")
        print(f"[reverie] Press Ctrl+C to stop")

        shutdown_event = new_shutdown_event()
        install_shutdown_handlers(shutdown_event)

        self._ensure_state_lock()
        self._publish_heartbeat_safe()
        self._start_heartbeat_publisher(heartbeat_interval)
        warned_env_files = set()

        min_step_interval = 1.0 / max_steps_per_second
        last_step_completed_at = None

        try:
            while not shutdown_event.is_set():
                self._raise_heartbeat_failure_if_any()

                if self.step >= _MAX_STEP:
                    time.sleep(self.server_sleep)
                    continue

                curr_env_file = f"{sim_folder}/environment/{self.step}.json"
                new_env = _read_snapshot_json(sim_folder, f"{self.step}.json")
                if new_env is not None:
                    if not _validate_env_snapshot(
                        new_env, self.personas, self.maze
                    ):
                        if curr_env_file not in warned_env_files:
                            warned_env_files.add(curr_env_file)
                            print(
                                f"[reverie] Ignoring invalid environment "
                                f"snapshot {curr_env_file}; waiting for "
                                f"corrected input."
                            )
                        time.sleep(self.server_sleep)
                        continue

                    if last_step_completed_at is not None:
                        _wait_until_deadline(
                            shutdown_event,
                            last_step_completed_at + min_step_interval,
                        )
                    if shutdown_event.is_set():
                        continue

                    for key, val in game_obj_cleanup.items():
                        self.maze.turn_event_from_tile_idle(key, val)
                    game_obj_cleanup = dict()

                    for persona_name, persona in self.personas.items():
                        curr_tile = self.personas_tile[persona_name]
                        new_tile = (
                            new_env[persona_name]["x"],
                            new_env[persona_name]["y"],
                        )

                        self.personas_tile[persona_name] = new_tile
                        self.maze.remove_subject_events_from_tile(
                            persona.name, curr_tile
                        )
                        self.maze.add_event_from_tile(
                            persona.scratch.get_curr_event_and_desc(),
                            new_tile,
                        )

                        if not persona.scratch.planned_path:
                            game_obj_cleanup[
                                persona.scratch.get_curr_obj_event_and_desc()
                            ] = new_tile
                            self.maze.add_event_from_tile(
                                persona.scratch.get_curr_obj_event_and_desc(),
                                new_tile,
                            )
                            blank = (
                                persona.scratch.get_curr_obj_event_and_desc()[0],
                                None,
                                None,
                                None,
                            )
                            self.maze.remove_event_from_tile(
                                blank, new_tile
                            )

                    movements = {"persona": dict(), "meta": dict()}
                    for persona_name, persona in self.personas.items():
                        (
                            next_tile,
                            pronunciatio,
                            description,
                        ) = persona.move(
                            self.maze,
                            self.personas,
                            self.personas_tile[persona_name],
                            self.curr_time,
                        )
                        movements["persona"][persona_name] = {}
                        movements["persona"][persona_name][
                            "movement"
                        ] = next_tile
                        movements["persona"][persona_name][
                            "pronunciatio"
                        ] = pronunciatio
                        movements["persona"][persona_name][
                            "description"
                        ] = description
                        movements["persona"][persona_name][
                            "chat"
                        ] = persona.scratch.chat

                    movements["meta"][
                        "curr_time"
                    ] = self.curr_time.strftime(
                        "%B %d, %Y, %H:%M:%S"
                    )

                    atomic_write_json_beneath(
                        sim_folder,
                        ("movement",),
                        f"{self.step}.json",
                        movements,
                    )

                    self._advance_step_time()
                    step_count += 1
                    steps_since_save += 1

                    self._publish_curr_step()
                    _prune_uncommitted_artifacts(sim_folder, self.step)

                    if steps_since_save >= autosave_interval:
                        self.save()
                        steps_since_save = 0
                        print(
                            f"[reverie] Autosaved at step {self.step} "
                            f"({step_count} steps processed)"
                        )

                    last_step_completed_at = time.monotonic()

                time.sleep(self.server_sleep)

        except KeyboardInterrupt:
            print(f"\n[reverie] Received interrupt. Saving and exiting...")
            self.save()
            self._raise_heartbeat_failure_if_any()
            print(f"[reverie] Saved at step {self.step}. Goodbye.")
        else:
            self._publish_heartbeat_safe()
            self.save()
            self._raise_heartbeat_failure_if_any()
            print(
                f"[reverie] Shutdown requested. "
                f"Saved at step {self.step}. Goodbye."
            )
        finally:
            self._stop_heartbeat_publisher()

    def _publish_heartbeat(self):
        """Publish the backend heartbeat atomically for a freshness check.

        The step and curr_time pair is snapshotted under the per-server
        state lock so a publication never observes a mismatched pair while
        the main thread advances both. The heartbeat write shares the
        runtime authority lock so a concurrent clear cannot leave a stale
        heartbeat behind after a restart.
        """
        step, curr_time = self._snapshot_runtime_state()
        payload = {
            "sim_code": self.sim_code,
            "step": step,
            "curr_time": (
                curr_time.strftime("%B %d, %Y, %H:%M:%S")
                if curr_time
                else None
            ),
            "timestamp": time.time(),
            "pid": os.getpid(),
        }
        from persona.prompt_template.gpt_structure import request_activity
        payload["provider_activity"] = request_activity()
        with runtime_lock(_runtime_lock_path()):
            atomic_write_json_beneath(
                fs_temp_storage, (), "backend_heartbeat.json", payload
            )

    def _publish_heartbeat_safe(self):
        """Publish the heartbeat or raise a sanitized publication error."""
        try:
            self._publish_heartbeat()
        except Exception as exc:
            raise HeartbeatPublicationError(
                _sanitize_heartbeat_error(exc)
            ) from None

    def _raise_heartbeat_failure_if_any(self):
        """Raise a sanitized error when the background publisher failed."""
        failure = getattr(self, "_heartbeat_failure", None)
        if failure is not None:
            raise HeartbeatPublicationError(failure)

    def _ensure_state_lock(self):
        """Create the per-server state lock for servers built without init."""
        if getattr(self, "_state_lock", None) is None:
            self._state_lock = threading.Lock()

    def _snapshot_runtime_state(self):
        """Return a coherent (step, curr_time) pair for publication.

        The pair is read under the per-server state lock so a concurrent
        heartbeat publication never observes a mismatched pair while the
        main thread advances both fields together.
        """
        lock = getattr(self, "_state_lock", None)
        if lock is None:
            return self.step, self.curr_time
        with lock:
            return self.step, self.curr_time

    def _advance_step_time(self):
        """Advance step and curr_time atomically under the state lock.

        The lock is held only for the two in-memory assignments; no I/O,
        sleep, or external lock is ever acquired while it is held.
        """
        lock = getattr(self, "_state_lock", None)
        if lock is None:
            self.step += 1
            self.curr_time += datetime.timedelta(seconds=self.sec_per_step)
            return
        with lock:
            self.step += 1
            self.curr_time += datetime.timedelta(seconds=self.sec_per_step)

    def _start_heartbeat_publisher(self, interval):
        """Start the dedicated background heartbeat publisher thread."""
        self._heartbeat_interval = interval
        self._heartbeat_failure = None
        self._heartbeat_stop = new_shutdown_event()
        self._heartbeat_thread = threading.Thread(
            target=self._run_heartbeat_publisher,
            args=(interval, self._heartbeat_stop),
            name="reverie-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _run_heartbeat_publisher(self, interval, stop_event):
        """Publish at ``interval`` until stopped; record failures sanitized.

        A publication failure is recorded on the server and the loop stops
        so the main loop can propagate it at its next safe boundary. No
        exception ever escapes this thread, so no background traceback or
        prompt can print.
        """
        wait_seconds = max(interval, _HEARTBEAT_MIN_INTERVAL)
        while not stop_event.is_set():
            if stop_event.wait(wait_seconds):
                break
            try:
                self._publish_heartbeat()
            except Exception as exc:
                self._heartbeat_failure = _sanitize_heartbeat_error(exc)
                break

    def _stop_heartbeat_publisher(self):
        """Stop the background publisher and bounded-join it."""
        stop = getattr(self, "_heartbeat_stop", None)
        if stop is not None:
            stop.set()
        thread = getattr(self, "_heartbeat_thread", None)
        if thread is None:
            return
        interval = getattr(
            self, "_heartbeat_interval", _HEARTBEAT_MIN_INTERVAL
        )
        thread.join(timeout=max(interval, _HEARTBEAT_MIN_INTERVAL) + 1.0)

    def open_server(self):
        """Open up an interactive terminal prompt for running the simulation."""
        print(
            "Note: The agents in this simulation package are computational"
        )
        print(
            "constructs powered by generative agents architecture and LLM. We"
        )
        print(
            "clarify that these agents lack human-like agency, consciousness,"
        )
        print("and independent decision-making.\n---")

        sim_folder = f"{fs_storage}/{self.sim_code}"

        while True:
            sim_command = input("Enter option: ")
            sim_command = sim_command.strip()
            ret_str = ""

            try:
                if sim_command.lower() in [
                    "f",
                    "fin",
                    "finish",
                    "save and finish",
                ]:
                    self.save()
                    break

                elif sim_command.lower() == "start path tester mode":
                    shutil.rmtree(sim_folder)
                    self.start_path_tester_server()

                elif sim_command.lower() == "exit":
                    shutil.rmtree(sim_folder)
                    break

                elif sim_command.lower() == "save":
                    self.save()

                elif sim_command[:3].lower() == "run":
                    run_match = _RUN_COUNT_RE.fullmatch(sim_command)
                    if not run_match:
                        print(
                            f"[reverie] Invalid 'run' command "
                            f"'{sim_command}': expected 'run <count>' with "
                            f"an integer from 0 to {_RUN_MAX_STEPS}."
                        )
                    else:
                        int_count = int(run_match.group(1))
                        if int_count > _RUN_MAX_STEPS:
                            print(
                                f"[reverie] Refusing 'run {int_count}': "
                                f"maximum is {_RUN_MAX_STEPS} steps."
                            )
                        elif int_count > 0:
                            self.start_server(int_count)

                elif "print persona schedule" in sim_command[:22].lower():
                    ret_str += self.personas[
                        " ".join(sim_command.split()[-2:])
                    ].scratch.get_str_daily_schedule_summary()

                elif "print all persona schedule" in sim_command[:26].lower():
                    for persona_name, persona in self.personas.items():
                        ret_str += f"{persona_name}\n"
                        ret_str += f"{persona.scratch.get_str_daily_schedule_summary()}\n"
                        ret_str += f"---\n"

                elif "print hourly org persona schedule" in sim_command.lower():
                    ret_str += self.personas[
                        " ".join(sim_command.split()[-2:])
                    ].scratch.get_str_daily_schedule_hourly_org_summary()

                elif "print persona current tile" in sim_command[:26].lower():
                    ret_str += str(
                        self.personas[
                            " ".join(sim_command.split()[-2:])
                        ].scratch.curr_tile
                    )

                elif "print persona chatting with buffer" in sim_command.lower():
                    curr_persona = self.personas[
                        " ".join(sim_command.split()[-2:])
                    ]
                    for (
                        p_n,
                        count,
                    ) in curr_persona.scratch.chatting_with_buffer.items():
                        ret_str += f"{p_n}: {count}"

                elif (
                    "print persona associative memory (event)"
                    in sim_command.lower()
                ):
                    ret_str += (
                        f'{self.personas[" ".join(sim_command.split()[-2:])]}\n'
                    )
                    ret_str += self.personas[
                        " ".join(sim_command.split()[-2:])
                    ].a_mem.get_str_seq_events()

                elif (
                    "print persona associative memory (thought)"
                    in sim_command.lower()
                ):
                    ret_str += (
                        f'{self.personas[" ".join(sim_command.split()[-2:])]}\n'
                    )
                    ret_str += self.personas[
                        " ".join(sim_command.split()[-2:])
                    ].a_mem.get_str_seq_thoughts()

                elif (
                    "print persona associative memory (chat)"
                    in sim_command.lower()
                ):
                    ret_str += (
                        f'{self.personas[" ".join(sim_command.split()[-2:])]}\n'
                    )
                    ret_str += self.personas[
                        " ".join(sim_command.split()[-2:])
                    ].a_mem.get_str_seq_chats()

                elif "print persona spatial memory" in sim_command.lower():
                    self.personas[
                        " ".join(sim_command.split()[-2:])
                    ].s_mem.print_tree()

                elif "print current time" in sim_command[:18].lower():
                    ret_str += (
                        f'{self.curr_time.strftime("%B %d, %Y, %H:%M:%S")}\n'
                    )
                    ret_str += f"steps: {self.step}"

                elif "print tile event" in sim_command[:16].lower():
                    cooordinate = [
                        int(i.strip()) for i in sim_command[16:].split(",")
                    ]
                    for i in self.maze.access_tile(cooordinate)["events"]:
                        ret_str += f"{i}\n"

                elif "print tile details" in sim_command.lower():
                    cooordinate = [
                        int(i.strip()) for i in sim_command[18:].split(",")
                    ]
                    for key, val in self.maze.access_tile(cooordinate).items():
                        ret_str += f"{key}: {val}\n"

                elif "call -- analysis" in sim_command.lower():
                    persona_name = sim_command[
                        len("call -- analysis") :
                    ].strip()
                    self.personas[persona_name].open_convo_session("analysis")

                elif "call -- load history" in sim_command.lower():
                    suffix = sim_command[
                        len("call -- load history") :
                    ].strip()
                    try:
                        rows = _read_history_rows(suffix)
                    except ValueError as exc:
                        print(f"[reverie] {exc}")
                    else:
                        clean_whispers = []
                        for row in rows:
                            if len(row) < 2:
                                continue
                            agent_name = row[0].strip()
                            if agent_name not in self.personas:
                                continue
                            for whisper in row[1].split(";"):
                                whisper = whisper.strip()
                                if whisper:
                                    clean_whispers += [
                                        [agent_name, whisper]
                                    ]
                        if clean_whispers:
                            load_history_via_whisper(
                                self.personas, clean_whispers
                            )

                print(ret_str)

            except LLMProviderError:
                raise
            except:
                traceback.print_exc()
                print("Error.")
                pass


def _valid_tile(x, y, maze_width, maze_height):
    """True when ``x``/``y`` are non-bool ints inside the maze bounds."""
    if isinstance(x, bool) or not isinstance(x, int):
        return False
    if isinstance(y, bool) or not isinstance(y, int):
        return False
    if not (0 <= x < maze_width):
        return False
    if not (0 <= y < maze_height):
        return False
    return True


def _validate_env_snapshot(env, personas, maze):
    """Validate an environment snapshot for the service loop.

    Returns True when ``env`` is a dict containing every persona with
    non-bool integer x/y coordinates inside the maze bounds. Malformed input
    is rejected without raising so the loop can keep waiting for corrected
    input.
    """
    if not isinstance(env, dict):
        return False
    for persona_name in personas:
        entry = env.get(persona_name)
        if not isinstance(entry, dict):
            return False
        if not _valid_tile(
            entry.get("x"), entry.get("y"), maze.maze_width, maze.maze_height
        ):
            return False
    return True


def _resolve_environment_snapshot(
    sim_folder, step, persona_names, maze_width, maze_height
):
    """Resolve the environment snapshot used to initialize personas.

    Prefers ``environment/{step}.json``. When that file is absent or invalid
    and ``step > 0``, falls back to the latest valid snapshot at or below
    ``step`` without changing ``self.step``. Only existing regular
    ``<n>.json`` files are considered, so resolution cost depends on the
    number of on-disk snapshots, never on the committed step. Raises
    ``FileNotFoundError`` with a clear message when no valid snapshot exists.
    """
    _validate_step(step)

    env_dir = f"{sim_folder}/environment"
    candidates = []
    for digits, name in _enumerate_numeric_json(env_dir):
        try:
            candidate = int(digits)
        except ValueError:
            continue
        if candidate > step:
            continue
        candidates.append((candidate, name))
    candidates.sort(key=lambda item: item[0], reverse=True)

    for _, name in candidates:
        env = _read_snapshot_json(sim_folder, name)
        if env is None:
            continue
        if not isinstance(env, dict):
            continue
        if not all(
            isinstance(env.get(name), dict)
            and _valid_tile(
                env[name].get("x"),
                env[name].get("y"),
                maze_width,
                maze_height,
            )
            for name in persona_names
        ):
            continue
        return env

    searched = f"environment/{step}.json"
    if step > 0:
        searched += " and prior snapshots down to environment/0.json"
    raise FileNotFoundError(
        f"No valid environment snapshot found for simulation '{sim_folder}'. "
        f"Searched {searched}."
    )


def _is_complete_sim(sim_folder):
    """True when ``sim_folder`` has a valid ``reverie/meta.json`` object.

    The meta is read descriptor-relative from the simulation root, so a
    symlink swapped in at any level is refused by the kernel instead of
    followed, and must parse as a JSON object.
    """
    try:
        meta = read_json_beneath(sim_folder, ("reverie", "meta.json"))
    except (OSError, ValueError):
        return False
    return isinstance(meta, dict)


def _ensure_dir(path):
    """Create ``path`` (and any missing parents) if it does not exist."""
    os.makedirs(path, exist_ok=True)


def _provider_key(provider):
    """Return the configured API key for ``provider`` without exposing it.

    Whitespace is stripped and known ``.env.example`` placeholder values are
    treated as missing so a copied template cannot pass validation.
    """
    if provider == "openrouter":
        raw = openrouter_api_key
    elif provider == "openai":
        raw = openai_api_key
    elif provider == "hetzner":
        raw = hetzner_api_key
    else:
        return ""
    key = raw.strip()
    if not key or key in _PLACEHOLDER_API_KEYS:
        return ""
    return key


def _validate_provider_keys(personas):
    """Fail fast when any provider in use lacks its API key.

    Resolves the effective provider for the global default and every loaded
    persona, then requires the matching provider and explicit embedding
    keys. Hetzner also verifies its model catalog before runtime markers
    are published. Key values are never exposed in error messages.
    """
    from persona.prompt_template import gpt_structure

    providers = set()
    hetzner_models = set()
    provider, model = gpt_structure._resolve_model(None)
    providers.add(provider)
    if provider == "hetzner":
        hetzner_models.add(model)
        providers.add(gpt_structure._resolve_embedding_provider())
        reasoning_effort_kwargs(provider, llm_reasoning_effort)
    if llm_embedding_provider:
        providers.add(llm_embedding_provider)
    for persona in personas.values():
        provider, model = gpt_structure._resolve_model(persona)
        providers.add(provider)
        if provider == "hetzner":
            hetzner_models.add(model)
            providers.add(gpt_structure._resolve_embedding_provider(persona))
            reasoning_effort_kwargs(provider, llm_reasoning_effort)

    for persona in personas.values():
        gpt_structure.bind_embedding_space(persona)

    missing = sorted(p for p in providers if not _provider_key(p))
    if missing:
        raise ValueError(
            "Missing API key for provider(s): "
            + ", ".join(missing)
            + ". Set the matching OPENROUTER_API_KEY, OPENAI_API_KEY or HETZNER_API_KEY."
        )
    if hetzner_models:
        gpt_structure.verify_hetzner_models(hetzner_models)


def _parse_args():
    parser = argparse.ArgumentParser(
        description="ReverieServer: generative agent simulation backend",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Run in non-interactive service mode (production).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an existing simulation instead of forking fresh.",
    )
    parser.add_argument(
        "--origin",
        dest="fork_sim_code",
        help="Name of the forked simulation (overrides REVERIE_FORK_SIM_CODE).",
    )
    parser.add_argument(
        "--target",
        dest="sim_code",
        help="Name of the new simulation (overrides REVERIE_SIM_CODE).",
    )
    parser.add_argument(
        "--autosave-interval",
        type=int,
        default=10,
        help="Autosave every N steps in service mode (default: 10).",
    )
    return parser.parse_args()


def _fail_serve(message):
    """Print a clear error and exit non-zero in service mode."""
    print(f"[reverie] ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def _resolve_codes(args, *, serve=False):
    """Resolve simulation codes from args or env vars.

    In service mode (``serve=True``) missing or unsafe codes are a hard
    error: the process prints a clear message to stderr and exits non-zero
    instead of blocking on stdin. Interactive mode keeps the legacy
    ``input()`` prompts but still rejects unsafe codes before any path
    construction.
    """
    fork_sim_code = args.fork_sim_code or os.getenv("REVERIE_FORK_SIM_CODE")
    sim_code = args.sim_code or os.getenv("REVERIE_SIM_CODE")

    if serve:
        if not sim_code:
            _fail_serve(
                "Missing simulation target code. Pass --target or set "
                "REVERIE_SIM_CODE."
            )
        if not args.resume and not fork_sim_code:
            _fail_serve(
                "Missing fork origin code. Pass --origin or set "
                "REVERIE_FORK_SIM_CODE."
            )
        try:
            _validate_sim_code(sim_code, label="target simulation code")
            if fork_sim_code:
                _validate_sim_code(
                    fork_sim_code, label="origin simulation code"
                )
        except ValueError as exc:
            _fail_serve(str(exc))
        return fork_sim_code, sim_code

    if not sim_code:
        sim_code = input("Enter the name of the new simulation: ").strip()

    _validate_sim_code(sim_code, label="target simulation code")

    target_exists = sim_code and os.path.isdir(f"{fs_storage}/{sim_code}")
    if args.resume and target_exists:
        if not fork_sim_code and not sim_code:
            fork_sim_code = input(
                "Enter the name of the forked simulation: "
            ).strip()
    elif not fork_sim_code:
        fork_sim_code = input(
            "Enter the name of the forked simulation: "
        ).strip()

    if fork_sim_code:
        _validate_sim_code(fork_sim_code, label="origin simulation code")

    return fork_sim_code, sim_code


def _run_serve(args):
    """Run service mode: acquire the lease, then clear, init, validate, serve.

    The backend liveness lease is acquired first, before any stale-marker
    clear, code resolution, server construction, provider validation, or
    marker mutation. A second backend that cannot acquire the lease exits
    immediately without touching the live owner's heartbeat or runtime
    markers. While the lease is held, stale markers are cleared, codes are
    resolved, the server is constructed and provider keys are validated,
    runtime markers are published, and the serve loop runs. Expected startup
    validation/storage errors (``ValueError``, ``FileNotFoundError``,
    ``FileExistsError``, ``OSError``) from code resolution, server
    construction, and provider setup exit controlled status 1 without a
    traceback. On every exit path inside the lease (cooperative shutdown,
    interrupt, missing codes, provider or init failure, or unexpected
    exception) the markers are removed again while the lease is still held,
    and only then is the lease released. A SIGKILL releases the lease via the
    kernel without cleanup, so a replacement backend acquires the lease and
    clears the dead owner's stale markers itself.
    """
    try:
        with liveness_lease(_liveness_lease_path()):
            try:
                _clear_stale_markers()
                fork_sim_code, sim_code = _resolve_codes(args, serve=True)
                if args.resume:
                    print(f"[reverie] Resuming simulation: {sim_code}")
                else:
                    print(f"[reverie] Forking from: {fork_sim_code}")
                    print(f"[reverie] Creating new simulation: {sim_code}")
                try:
                    rs = ReverieServer(
                        fork_sim_code, sim_code, resume=args.resume
                    )
                except Exception as exc:
                    _fail_serve(str(exc))
                try:
                    _validate_provider_keys(rs.personas)
                except Exception as exc:
                    _fail_serve(str(exc))
                rs.publish_runtime_markers()
                try:
                    rs.serve_forever(autosave_interval=args.autosave_interval)
                except LLMProviderError as exc:
                    _fail_serve(str(exc))
                except HeartbeatPublicationError as exc:
                    _fail_serve(str(exc))
            finally:
                _clear_stale_markers()
    except LivenessLeaseError as exc:
        _fail_serve(str(exc))


def _run_interactive(args):
    global rs
    try:
        fork_sim_code, sim_code = _resolve_codes(args)
    except ValueError as exc:
        print(f"[reverie] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    try:
        with liveness_lease(_liveness_lease_path()):
            try:
                _clear_stale_markers()
                print(f"Forking from: {fork_sim_code}")
                print(f"Creating new simulation: {sim_code}")
                try:
                    rs = ReverieServer(fork_sim_code, sim_code)
                except Exception as exc:
                    print(f"[reverie] ERROR: {exc}", file=sys.stderr)
                    sys.exit(1)
                try:
                    _validate_provider_keys(rs.personas)
                except Exception as exc:
                    print(f"[reverie] ERROR: {exc}", file=sys.stderr)
                    sys.exit(1)
                rs.publish_runtime_markers()
                try:
                    rs.open_server()
                except LLMProviderError as exc:
                    print(f"[reverie] ERROR: {exc}", file=sys.stderr)
                    sys.exit(1)
            finally:
                _clear_stale_markers()
    except LivenessLeaseError as exc:
        print(f"[reverie] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(_script_dir)

    args = _parse_args()

    if args.serve:
        _run_serve(args)
    else:
        _run_interactive(args)

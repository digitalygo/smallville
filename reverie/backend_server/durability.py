"""
Durability helpers for the Reverie backend.

Provides crash-safe JSON publication (temp file plus fsync plus atomic
replace), atomic no-replace directory publication, descriptor-relative
traversal helpers for reading and writing strictly beneath a verified root,
cooperative shutdown signaling, a service heartbeat, and a shared runtime
authority lock. The helpers are used by the service loop so readers never
observe partially written JSON, a shutdown signal never interrupts a
partially completed step, and runtime authority markers are never cleared or
published concurrently.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import fcntl
import json
import os
import signal
import stat
import tempfile
import threading
import uuid

_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_renameat2 = None


def _dir_flags():
    """Return read-only directory open flags with O_DIRECTORY and O_NOFOLLOW."""
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


@contextlib.contextmanager
def runtime_lock(lock_path):
    """Acquire an exclusive advisory lock on ``lock_path``.

    The lock file is persistent and never unlinked so a waiting process can
    never lock a fresh inode while another holds the old one. The parent
    directory is created if missing, then reopened with ``O_DIRECTORY`` plus
    ``O_NOFOLLOW``, and the leaf is opened with ``O_CREAT`` and ``O_RDWR``
    plus ``O_NOFOLLOW`` relative to that descriptor, so a symlinked parent
    directory or a symlink at the lock path is never followed and fails
    closed. The descriptors are closed reliably on every exit path.
    """
    directory, leaf = os.path.split(lock_path)
    if not directory:
        directory = "."
    if not _safe_path_component(leaf):
        raise ValueError(
            f"Unsafe lock file name {leaf!r} in runtime lock path "
            f"'{lock_path}'."
        )
    os.makedirs(directory, exist_ok=True)
    dir_fd = os.open(directory, _dir_flags())
    try:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(leaf, flags, 0o666, dir_fd=dir_fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


class LivenessLeaseError(RuntimeError):
    """Raised when the backend liveness lease is already held."""


@contextlib.contextmanager
def liveness_lease(lock_path):
    """Acquire the exclusive backend liveness lease on ``lock_path``.

    The lease is the single-owner authority for runtime marker publication.
    The lease file is persistent and never unlinked so a waiting process can
    never lock a fresh inode while another holds the old one. The parent
    directory is created if missing, then reopened with ``O_DIRECTORY`` plus
    ``O_NOFOLLOW``, and the leaf is opened with ``O_CREAT`` and ``O_RDWR``
    plus ``O_NOFOLLOW`` relative to that descriptor, so a symlinked parent
    directory or a symlink at the lease path is never followed and fails
    closed. Acquisition is nonblocking: a second backend that finds the
    lease held fails fast with ``LivenessLeaseError`` before publishing any
    runtime markers. The kernel releases the lease automatically when the
    holding process dies, and the descriptors are closed reliably on every
    exit path.
    """
    directory, leaf = os.path.split(lock_path)
    if not directory:
        directory = "."
    if not _safe_path_component(leaf):
        raise ValueError(
            f"Unsafe lease file name {leaf!r} in liveness lease path "
            f"'{lock_path}'."
        )
    os.makedirs(directory, exist_ok=True)
    dir_fd = os.open(directory, _dir_flags())
    try:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(leaf, flags, 0o666, dir_fd=dir_fd)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise LivenessLeaseError(
                    f"Another backend already holds the liveness lease at "
                    f"'{lock_path}'. Only one backend may run at a time."
                ) from exc
            try:
                yield fd
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


def _split_path_for_read(path):
    """Split ``path`` into a verified root and safe path components.

    Absolute paths are rooted at ``/`` and relative paths at ``.``. Empty
    components are dropped and every remaining component must pass
    ``_safe_path_component``, so traversal components, embedded separators,
    and control characters are rejected before any open.
    """
    if not isinstance(path, str):
        raise ValueError(f"Invalid path {path!r} for a checked JSON read.")
    if path.startswith("/"):
        root = "/"
        parts = path[1:].split("/")
    else:
        root = "."
        parts = path.split("/")
    components = [part for part in parts if part]
    for component in components:
        if not _safe_path_component(component):
            raise ValueError(
                f"Unsafe path component {component!r} in checked JSON read "
                f"of '{path}'."
            )
    return root, components


_MISSING_DEFAULT = object()


def read_json_checked(path, *, missing_default=_MISSING_DEFAULT):
    """Read UTF-8 JSON from ``path`` through descriptor-relative traversal.

    The path is split into safe single components and read through
    ``read_json_beneath``: the root (``/`` for absolute paths, ``.``
    otherwise) is opened once with ``O_DIRECTORY | O_NOFOLLOW`` and every
    parent component is opened with ``dir_fd`` from the previous descriptor,
    so an ancestor swapped to a symlink after the root open is refused by
    the kernel instead of followed. The final component is opened with
    ``O_RDONLY | O_NOFOLLOW`` plus ``O_NONBLOCK`` when available and must
    fstat as a regular file before ``json.load`` runs, so a symlink or FIFO
    at the path is never followed or read. Every descriptor is closed on
    every exit path, including parse failures. Raises ``OSError`` for
    unopenable or non-regular paths and ``ValueError`` for malformed JSON or
    unsafe components, with the offending path in the message.
    When ``missing_default`` is supplied, return it only for genuine
    FileNotFoundError; JSON null, corrupt files, symlinks and special files
    remain distinguishable from an absent optional file.
    """
    root, components = _split_path_for_read(path)
    if not components:
        raise OSError(f"Refusing to read '{path}': not a regular file.")
    try:
        return read_json_beneath(root, components)
    except OSError as exc:
        if isinstance(exc, FileNotFoundError) and missing_default is not _MISSING_DEFAULT:
            return missing_default
        if str(exc).startswith("Refusing to read "):
            raise OSError(
                f"Refusing to read '{path}': not a regular file."
            ) from exc
        raise OSError(
            f"Cannot open '{path}' for a regular-file JSON read: {exc}."
        ) from exc
    except ValueError as exc:
        message = str(exc)
        prefix = (
            f"Malformed JSON in '{'/'.join(components)}' beneath "
            f"'{root}': "
        )
        if message.startswith(prefix):
            detail = message[len(prefix):]
            if detail.endswith("."):
                detail = detail[:-1]
            raise ValueError(f"Malformed JSON in '{path}': {detail}.") from exc
        raise ValueError(f"Cannot read '{path}': {exc}.") from exc


def _safe_path_component(component):
    """True when ``component`` is a safe single path component."""
    if not isinstance(component, str) or not component:
        return False
    if component in (".", ".."):
        return False
    if "/" in component or "\\" in component:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in component):
        return False
    return True


def ensure_dir_beneath(root_path, components):
    """Ensure every directory in ``components`` exists beneath a real root.

    ``components`` must be a sequence of safe single path components with no
    ``.``/``..``, separators, or control characters and may be empty, in
    which case only the root is verified. The root is opened once with
    ``O_DIRECTORY | O_NOFOLLOW`` and every component is opened with
    ``dir_fd`` from the previous descriptor, so a symlinked root or a
    preexisting symlink directory at any level is refused by the kernel
    instead of followed. A missing component is created with ``mkdir``
    relative to the held descriptor and then reopened. Every descriptor is
    closed on every exit path. Raises ``ValueError`` for unsafe components
    and ``OSError`` for unopenable, symlinked, or uncreatable paths.
    """
    for component in components:
        if not _safe_path_component(component):
            raise ValueError(
                f"Unsafe path component {component!r} in directory "
                f"creation beneath '{root_path}'."
            )
    fds = []
    try:
        fds.append(os.open(root_path, _dir_flags()))
        for component in components:
            try:
                fds.append(os.open(component, _dir_flags(), dir_fd=fds[-1]))
            except FileNotFoundError:
                try:
                    os.mkdir(component, dir_fd=fds[-1])
                except FileExistsError:
                    pass
                fds.append(os.open(component, _dir_flags(), dir_fd=fds[-1]))
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass


def read_json_beneath(root_path, components):
    """Read UTF-8 JSON beneath ``root_path`` through descriptor-relative opens.

    ``components`` must be a nonempty sequence of safe single path components
    with no ``.``/``..``, separators, or control characters. The root is
    opened once with ``O_DIRECTORY | O_NOFOLLOW`` and every parent component
    is opened with ``dir_fd`` from the previous descriptor, so an ancestor
    swapped to a symlink after the root open is refused by the kernel instead
    of followed. The final component is opened with ``O_RDONLY | O_NOFOLLOW``
    plus ``O_NONBLOCK`` when available and must fstat as a regular file
    before ``json.load`` runs. Every descriptor is closed on every exit
    path. Raises ``FileNotFoundError`` for a genuinely missing final path or
    parent, ``OSError`` for unopenable or non-regular paths, and
    ``ValueError`` for unsafe components or malformed JSON.
    """
    if not isinstance(components, (tuple, list)) or not components:
        raise ValueError(
            f"read_json_beneath requires nonempty components beneath "
            f"'{root_path}'."
        )
    for component in components:
        if not _safe_path_component(component):
            raise ValueError(
                f"Unsafe path component {component!r} in descriptor-relative "
                f"read beneath '{root_path}'."
            )
    dir_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        dir_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        dir_flags |= os.O_NOFOLLOW
    file_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        file_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        file_flags |= os.O_NONBLOCK
    display = "/".join(components)
    fds = []
    try:
        fds.append(os.open(root_path, dir_flags))
        for component in components[:-1]:
            fds.append(os.open(component, dir_flags, dir_fd=fds[-1]))
        fds.append(os.open(components[-1], file_flags, dir_fd=fds[-1]))
        st = os.fstat(fds[-1])
        if not stat.S_ISREG(st.st_mode):
            raise OSError(
                f"Refusing to read '{display}' beneath '{root_path}': "
                f"not a regular file."
            )
        with os.fdopen(fds[-1], "r", encoding="utf-8") as json_file:
            fds.pop()
            return json.load(json_file)
    except ValueError as exc:
        raise ValueError(
            f"Malformed JSON in '{display}' beneath '{root_path}': {exc}."
        ) from exc
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass


def is_real_regular(path):
    """True when ``path`` lstat's as a regular file.

    Uses ``lstat`` so a symlink, dangling or not, is never reported as a
    regular file. Returns False for missing paths and any ``OSError``.
    """
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def is_real_dir(path):
    """True when ``path`` lstat's as a directory.

    Uses ``lstat`` so a symlink, dangling or not, is never reported as a
    directory. Returns False for missing paths and any ``OSError``.
    """
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def fsync_regular(path):
    """fsync a regular file at ``path`` through a checked descriptor.

    The file is opened with ``O_RDONLY | O_NOFOLLOW`` and must fstat as a
    regular file, so a symlink or special file swapped in after validation
    is refused instead of followed. Raises ``OSError`` with the offending
    path on any failure.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"Refusing to fsync '{path}': not a regular file.")
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_json(path, payload, *, indent=2):
    """Write ``payload`` as JSON to ``path`` atomically.

    The payload is serialized to a temporary file in the same directory,
    flushed and fsynced, then moved into place with ``os.replace``. Readers
    never observe a partially written file. The temporary file is removed on
    any failure. The directory is fsynced best-effort so the rename itself is
    durable where the filesystem supports it.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".tmp-", suffix=".json", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        fsync_dir(directory)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_json_beneath(
    root_path, parent_components, filename, payload, *, indent=2
):
    """Write ``payload`` as JSON beneath ``root_path`` atomically.

    ``parent_components`` must be a sequence of safe single path components
    and ``filename`` a safe single leaf component. The root is opened once
    with ``O_DIRECTORY | O_NOFOLLOW`` and every parent component is opened
    with ``dir_fd`` from the previous descriptor, so a symlinked root or
    parent swapped to a symlink is refused by the kernel instead of
    followed. The payload is serialized to a temp leaf created with
    ``O_CREAT | O_EXCL`` in the destination directory, flushed and fsynced,
    then moved into place with ``os.replace`` relative to the held
    destination descriptor, and the destination directory is fsynced so the
    rename itself is durable where the filesystem supports it. Readers never
    observe a partially written file. The temp leaf is removed on any
    failure and every descriptor is closed on every exit path. Raises
    ``ValueError`` for unsafe components and ``OSError`` for unopenable or
    unwritable paths.
    """
    if not isinstance(parent_components, (tuple, list)):
        raise ValueError(
            "atomic_write_json_beneath requires parent components beneath "
            f"'{root_path}'."
        )
    for component in parent_components:
        if not _safe_path_component(component):
            raise ValueError(
                f"Unsafe path component {component!r} in atomic write "
                f"beneath '{root_path}'."
            )
    if not _safe_path_component(filename):
        raise ValueError(
            f"Unsafe leaf filename {filename!r} in atomic write beneath "
            f"'{root_path}'."
        )
    ensure_dir_beneath(root_path, parent_components)
    fds = []
    tmp_name = None
    try:
        fds.append(os.open(root_path, _dir_flags()))
        for component in parent_components:
            fds.append(os.open(component, _dir_flags(), dir_fd=fds[-1]))
        dst_fd = fds[-1]
        tmp_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        for _attempt in range(5):
            candidate = f".tmp-{os.getpid()}-{uuid.uuid4().hex}.json"
            try:
                tmp_fd = os.open(candidate, tmp_flags, 0o666, dir_fd=dst_fd)
                tmp_name = candidate
                break
            except FileExistsError:
                continue
        else:
            raise OSError(
                f"Cannot create a temp leaf for atomic write beneath "
                f"'{root_path}'."
            )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as json_file:
                json.dump(payload, json_file, indent=indent)
                json_file.flush()
                os.fsync(json_file.fileno())
            os.replace(
                tmp_name,
                filename,
                src_dir_fd=dst_fd,
                dst_dir_fd=dst_fd,
            )
            tmp_name = None
            os.fsync(dst_fd)
        except BaseException:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name, dir_fd=dst_fd)
                except OSError:
                    pass
            raise
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass


def _fsync_dir(directory):
    """fsync a directory so a rename inside it is durable.

    The directory is opened with ``O_RDONLY`` plus ``O_DIRECTORY`` and
    ``O_NOFOLLOW`` when available, so a symlink at the path is never
    followed. Raises ``OSError`` on any failure.
    """
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(directory, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(directory):
    """Best-effort fsync of a directory so a rename is durable.

    Public wrapper around ``_fsync_dir`` for callers that rename whole
    directories (for example checkpoint staging) and need the rename itself
    to be durable where the filesystem supports it. Failures are ignored.
    """
    try:
        _fsync_dir(directory)
    except OSError:
        pass


def fsync_dir_checked(directory):
    """Strictly fsync a directory or raise ``OSError`` with the path.

    Counterpart to ``fsync_dir`` for callers that treat a failed directory
    fsync as fatal, for example a fresh fork whose rename must be durable
    before the target is considered published.
    """
    _fsync_dir(directory)


def _load_renameat2():
    """Resolve ``renameat2`` from the main program namespace or the libc.

    The main program namespace is tried first so statically linked musl
    binaries work, then the architecture-neutral libc soname returned by
    ``ctypes.util.find_library('c')`` so dynamically linked glibc and musl
    systems resolve their own libc without hard-coded x86 sonames. The
    resolved function is cached for the process lifetime. Returns ``None``
    when ``renameat2`` is unavailable so callers fail closed.
    """
    global _renameat2
    if _renameat2 is not None:
        return _renameat2 or None
    try:
        libc_name = ctypes.util.find_library("c")
    except (OSError, ValueError):
        libc_name = None
    for name in (None, libc_name):
        if name is not None and not name:
            continue
        try:
            if name is None:
                libc = ctypes.CDLL(None, use_errno=True)
            else:
                libc = ctypes.CDLL(name, use_errno=True)
            func = libc.renameat2
        except (AttributeError, OSError):
            continue
        func.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        func.restype = ctypes.c_int
        _renameat2 = func
        return func
    _renameat2 = False
    return None


def rename_no_replace(src, dst):
    """Atomically rename ``src`` to ``dst`` only when ``dst`` does not exist.

    Uses the Linux ``renameat2(RENAME_NOREPLACE)`` syscall through libc
    (glibc or musl), so the kernel itself guarantees that an existing file
    or directory at ``dst`` is never replaced, even when it appears between
    any earlier check and the rename. A collision raises
    ``FileExistsError`` and leaves ``src`` and ``dst`` untouched. When
    ``renameat2`` is unavailable the helper fails closed with ``OSError``
    instead of falling back to a racy existence-check plus replace.
    """
    func = _load_renameat2()
    if func is None:
        raise OSError(
            "renameat2(RENAME_NOREPLACE) is not available on this system; "
            "refusing to publish without an atomic no-replace guarantee."
        )
    result = func(
        _AT_FDCWD,
        os.fsencode(src),
        _AT_FDCWD,
        os.fsencode(dst),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), dst)


def install_shutdown_handlers(event):
    """Route SIGTERM and SIGINT to a shutdown event.

    The service loop checks ``event.is_set()`` at safe points so a signal
    never interrupts a partially completed step. Ctrl+C remains graceful: it
    requests the same cooperative shutdown instead of raising
    ``KeyboardInterrupt`` mid-transaction.
    """
    def _request_shutdown(signum, frame):
        event.set()

    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)


def new_shutdown_event():
    """Return a fresh ``threading.Event`` for cooperative shutdown."""
    return threading.Event()

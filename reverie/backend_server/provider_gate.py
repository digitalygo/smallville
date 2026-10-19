"""A persistent, per-key request gate shared by local processes and threads."""

import fcntl
import hashlib
import json
import math
import os
import stat
import time
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from pathlib import Path


def retry_after_seconds(exception, now=None):
    response = getattr(exception, "response", None)
    headers = getattr(response, "headers", {}) or {}
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - (
                time.time() if now is None else now
            )
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


class SharedRequestGate:
    """Serialize dispatch, with no accumulated permits or catch-up bursts.

    The lock stays held during the request so descheduled workers cannot
    dispatch old permits together. The next start is measured from response
    completion, conservatively allowing at most one request per interval.
    All workers must use the same persistent directory on a filesystem with
    working flock semantics. Never remove gate files while workers run.
    """

    def __init__(self, directory, api_key, interval, clock=time.time, sleep=time.sleep):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("Request interval must be finite and positive.")
        self.directory = Path(directory)
        self.fingerprint = hashlib.sha256(api_key.encode()).hexdigest()
        self.interval = interval
        self.clock = clock
        self.sleep = sleep

    def _write(self, fd, next_start):
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, json.dumps({"next_start": next_start}).encode())
        os.ftruncate(fd, os.lseek(fd, 0, os.SEEK_CUR))
        os.fsync(fd)

    @contextmanager
    def slot(self, on_wait=None):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(self.directory / (self.fingerprint + ".json"), flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("Shared request gate must be a regular file.")
            while True:
                fcntl.flock(fd, fcntl.LOCK_EX)
                os.lseek(fd, 0, os.SEEK_SET)
                raw = os.read(fd, 1024)
                state = json.loads(raw) if raw else {"next_start": 0}
                next_start = state["next_start"]
                if (
                    isinstance(next_start, bool)
                    or not isinstance(next_start, (int, float))
                    or not math.isfinite(next_start)
                ):
                    raise ValueError("Invalid shared request gate state.")
                delay = next_start - self.clock()
                if delay <= 0:
                    break
                fcntl.flock(fd, fcntl.LOCK_UN)
                if on_wait:
                    on_wait(delay)
                self.sleep(min(delay, 60))
            self._write(fd, self.clock() + self.interval)
            cooldown = 0
            try:
                yield
            except Exception as exc:
                retry_after = retry_after_seconds(exc)
                cooldown = (
                    retry_after
                    if retry_after is not None
                    else (60 if getattr(exc, "status_code", None) == 429 else 0)
                )
                raise
            finally:
                self._write(fd, self.clock() + max(self.interval, cooldown))
        finally:
            os.close(fd)

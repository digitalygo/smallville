"""Login throttling for brute-force protection.

Gates POST /accounts/login/ and /admin/login/ before the view runs so
password hashing never happens for throttled attempts. State lives in the
database and is shared by every Gunicorn worker. Buckets are keyed by
HMAC-SHA256 digests of opaque values, never by raw IPs or usernames.
"""

import hashlib
import hmac
import ipaddress
import logging
import random
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.models import F
from django.http import HttpResponse
from django.utils import timezone

from translator.models import LoginThrottleBucket

logger = logging.getLogger(__name__)

_PROTECTED_PATHS = {"/accounts/login/", "/admin/login/"}
_FALLBACK_SOURCE = "unresolvable-client"
_MAX_GATE_RETRIES = 5
_RETRY_SLEEP_SECONDS = 0.005
_SWEEP_PROBABILITY = 0.02
_SWEEP_LIMIT = 100
_SWEEP_MARGIN_SECONDS = 60
_MAX_BACKOFF_EXPONENT = 63
_GATE_PROCESS_LOCK = threading.Lock()


def _normalized_username(raw):
    if not isinstance(raw, str):
        return ""
    return raw.strip().casefold()


def _hmac_digest(message):
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _source_key(client_source):
    return _hmac_digest("login-throttle:source:" + client_source)


def _pair_key(client_source, username):
    normalized = _normalized_username(username)
    return _hmac_digest(
        "login-throttle:pair:" + client_source + "\x00" + normalized
    )


def _is_valid_ip(value):
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _is_trusted(value, trusted_networks):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in trusted_networks)


def resolve_client_source(request):
    """Return the client source marker for *request*.

    X-Forwarded-For is honored only when REMOTE_ADDR belongs to a
    configured trusted proxy CIDR. The chain is parsed right to left and
    the first untrusted valid address wins. Malformed or unusable input
    falls back to a stable opaque marker shared by all such clients.
    """
    remote = (request.META.get("REMOTE_ADDR") or "").strip()
    if not remote or not _is_valid_ip(remote):
        return _FALLBACK_SOURCE
    trusted_networks = settings.LOGIN_THROTTLE_TRUSTED_PROXY_CIDRS
    if not _is_trusted(remote, trusted_networks):
        return remote
    chain = [
        entry.strip()
        for entry in (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")
    ]
    if not chain or any(not _is_valid_ip(entry) for entry in chain):
        return _FALLBACK_SOURCE
    for entry in reversed(chain):
        if not _is_trusted(entry, trusted_networks):
            return entry
    return _FALLBACK_SOURCE


def _backoff_delay(failures, threshold):
    exponent = min(max(failures - threshold, 0), _MAX_BACKOFF_EXPONENT)
    delay = settings.LOGIN_THROTTLE_BASE_DELAY_SECONDS * (2**exponent)
    return min(delay, settings.LOGIN_THROTTLE_MAX_DELAY_SECONDS)


def _gate_bucket(bucket_key, threshold):
    """Register one attempt on *bucket_key* and return (blocked, failures).

    The pass decision is encoded in an atomic conditional UPDATE: a row is
    incremented only while its failure count is below the threshold, so
    concurrent workers cannot double pass a threshold on any backend. A
    blocked attempt increments unconditionally so backoff keeps growing.
    Retries are bounded; the caller treats exhaustion as a throttle failure.
    """
    if connection.vendor == "postgresql":
        return _gate_bucket_unlocked(bucket_key, threshold)
    with _GATE_PROCESS_LOCK:
        return _gate_bucket_unlocked(bucket_key, threshold)


def _gate_bucket_unlocked(bucket_key, threshold):
    now = timezone.now()
    cutoff = now - timedelta(seconds=settings.LOGIN_THROTTLE_WINDOW_SECONDS)
    for attempt in range(_MAX_GATE_RETRIES):
        try:
            with transaction.atomic():
                row = (
                    LoginThrottleBucket.objects.select_for_update()
                    .filter(bucket_key=bucket_key)
                    .first()
                )
                if row is None:
                    try:
                        LoginThrottleBucket.objects.create(
                            bucket_key=bucket_key, failures=1, window_start=now
                        )
                    except IntegrityError:
                        continue
                    return False, 1
                if row.window_start <= cutoff:
                    updated = LoginThrottleBucket.objects.filter(
                        pk=row.pk, window_start=row.window_start
                    ).update(failures=1, window_start=now, updated_at=now)
                    if updated == 1:
                        return False, 1
                    continue
                updated = LoginThrottleBucket.objects.filter(
                    pk=row.pk,
                    window_start=row.window_start,
                    failures__lt=threshold,
                ).update(failures=F("failures") + 1, updated_at=now)
                if updated == 1:
                    return False, row.failures + 1
                updated = LoginThrottleBucket.objects.filter(
                    pk=row.pk, window_start=row.window_start
                ).update(failures=F("failures") + 1, updated_at=now)
                if updated == 1:
                    fresh = LoginThrottleBucket.objects.get(pk=row.pk)
                    return True, fresh.failures
                continue
        except OperationalError:
            time.sleep(_RETRY_SLEEP_SECONDS * (attempt + 1))
            continue
    raise RuntimeError("login throttle gate retries exhausted")


def _sweep_stale_buckets(limit=_SWEEP_LIMIT):
    """Delete expired buckets, bounded to *limit* rows per call."""
    cutoff = timezone.now() - timedelta(
        seconds=(
            settings.LOGIN_THROTTLE_WINDOW_SECONDS
            + settings.LOGIN_THROTTLE_MAX_DELAY_SECONDS
            + _SWEEP_MARGIN_SECONDS
        )
    )
    stale_ids = list(
        LoginThrottleBucket.objects.filter(updated_at__lt=cutoff)
        .values_list("pk", flat=True)[:limit]
    )
    if stale_ids:
        LoginThrottleBucket.objects.filter(pk__in=stale_ids).delete()
    return len(stale_ids)


def _throttled_response(delay_seconds):
    response = HttpResponse(
        "Too many login attempts. Try again later.", status=429
    )
    response["Retry-After"] = str(int(delay_seconds))
    return response


class LoginThrottleMiddleware:
    """Reject excessive login POSTs before the view hashes passwords."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        state = getattr(request, "_login_throttle_state", None)
        if state is not None:
            self._finish(request, state)
        return response

    def process_view(self, request, view_func, view_args, view_kwargs):
        if request.method != "POST":
            return None
        if request.path not in _PROTECTED_PATHS:
            return None
        if request.user.is_authenticated:
            return HttpResponse("Forbidden", status=403)
        if not settings.LOGIN_THROTTLE_ENABLED:
            return None
        try:
            return self._throttle(request)
        except Exception:
            logger.warning("login throttle failure", exc_info=True)
            if not settings.LOGIN_THROTTLE_FAIL_OPEN:
                return HttpResponse("Service unavailable", status=503)
            return None

    def _throttle(self, request):
        source = resolve_client_source(request)
        username = request.POST.get("username", "")
        source_key = _source_key(source)
        pair_key = _pair_key(source, username)
        source_blocked, source_failures = _gate_bucket(
            source_key, settings.LOGIN_THROTTLE_SOURCE_THRESHOLD
        )
        pair_blocked, pair_failures = _gate_bucket(
            pair_key, settings.LOGIN_THROTTLE_ACCOUNT_THRESHOLD
        )
        request._login_throttle_state = {"pair_key": pair_key}
        if random.random() < _SWEEP_PROBABILITY:
            _sweep_stale_buckets()
        if source_blocked or pair_blocked:
            delay = max(
                _backoff_delay(
                    source_failures, settings.LOGIN_THROTTLE_SOURCE_THRESHOLD
                ),
                _backoff_delay(
                    pair_failures, settings.LOGIN_THROTTLE_ACCOUNT_THRESHOLD
                ),
            )
            return _throttled_response(delay)
        return None

    def _finish(self, request, state):
        try:
            if request.session.get(SESSION_KEY):
                LoginThrottleBucket.objects.filter(
                    bucket_key=state["pair_key"]
                ).update(
                    failures=0,
                    window_start=timezone.now(),
                    updated_at=timezone.now(),
                )
        except Exception:
            logger.warning("login throttle success reset failed", exc_info=True)

from django.db import models


class LoginThrottleBucket(models.Model):
    """Login throttle state shared across workers.

    ``bucket_key`` is an HMAC-SHA256 hex digest of an opaque value, never a
    raw IP address or username. ``failures`` counts failed attempts inside
    the current window that starts at ``window_start``.
    """

    bucket_key = models.CharField(max_length=64, unique=True)
    failures = models.PositiveIntegerField(default=0)
    window_start = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["updated_at"], name="login_throttle_updated_idx"),
        ]

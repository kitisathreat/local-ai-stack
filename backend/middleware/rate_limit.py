"""Per-user rate limiting.

Ported from pipelines/rate_limiter.py. In-memory ring buffers per user;
cheap enough for a single-backend-process deployment. A distributed
setup would swap this for a Redis-backed counter.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict

from fastapi import HTTPException


MAX_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))
MAX_PER_DAY = int(os.getenv("RATE_LIMIT_PER_DAY", "500"))
EXEMPT_ROLES = set((os.getenv("RATE_LIMIT_EXEMPT_ROLES", "admin") or "").split(","))


class RateLimiter:
    def __init__(self, per_minute: int = MAX_PER_MINUTE, per_day: int = MAX_PER_DAY):
        self.per_minute = per_minute
        self.per_day = per_day
        self._minute: dict[str, list[float]] = defaultdict(list)
        self._day: dict[str, list[float]] = defaultdict(list)

    def _cleanup(self, key: str, now: float) -> None:
        self._minute[key] = [t for t in self._minute[key] if now - t < 60]
        self._day[key] = [t for t in self._day[key] if now - t < 86400]

    def check(self, key: str, role: str = "user") -> None:
        """Raise HTTPException 429 if the key is over its minute/day budget.
        Counts the current request on success (must be called exactly once
        per attempt)."""
        if role in EXEMPT_ROLES:
            return
        now = time.time()
        self._cleanup(key, now)
        if self.per_minute > 0 and len(self._minute[key]) >= self.per_minute:
            raise HTTPException(
                429,
                f"Rate limit: {self.per_minute} requests/minute exceeded.",
            )
        if self.per_day > 0 and len(self._day[key]) >= self.per_day:
            raise HTTPException(
                429,
                f"Daily limit: {self.per_day} requests/day exceeded.",
            )
        self._minute[key].append(now)
        self._day[key].append(now)


# Module-level singleton — imported by main.py at startup.
rate_limiter = RateLimiter()

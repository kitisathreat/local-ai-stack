"""Per-user rate limiting (sliding window) with Redis backend + in-memory fallback.

When a `redis.asyncio.Redis` client is attached via `configure(...)`, the
limiter uses a sorted-set sliding window (one ZSET per key per window) so
multiple Uvicorn workers share a consistent view. If Redis is unavailable
or returns an error, the limiter falls back to the per-process in-memory
implementation and logs a one-shot warning — this is "fail-open" for
availability; a brief period of per-worker (rather than global) accounting
is preferred over rejecting users outright.

Limits are sourced from `AuthRateLimits` in the config (hot-reloadable via
`configure(...)`) with env-var fallbacks for older deployments.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections import defaultdict
from typing import TYPE_CHECKING

from fastapi import HTTPException

if TYPE_CHECKING:
    from redis.asyncio import Redis as AsyncRedis


logger = logging.getLogger(__name__)


_ENV_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))
_ENV_PER_DAY = int(os.getenv("RATE_LIMIT_PER_DAY", "500"))
EXEMPT_ROLES = set((os.getenv("RATE_LIMIT_EXEMPT_ROLES", "admin") or "").split(","))


class RateLimiter:
    def __init__(
        self,
        per_minute: int = _ENV_PER_MINUTE,
        per_day: int = _ENV_PER_DAY,
    ):
        self.per_minute = per_minute
        self.per_day = per_day
        self._minute: dict[str, list[float]] = defaultdict(list)
        self._day: dict[str, list[float]] = defaultdict(list)
        self._redis: "AsyncRedis | None" = None
        self._redis_warned = False

    def configure(
        self,
        per_minute: int | None = None,
        per_day: int | None = None,
        redis_client: "AsyncRedis | None" = None,
    ) -> None:
        """Called from FastAPI lifespan (and admin config reload)."""
        if per_minute is not None:
            self.per_minute = int(per_minute)
        if per_day is not None:
            self.per_day = int(per_day)
        self._redis = redis_client
        self._redis_warned = False

    def _cleanup(self, key: str, now: float) -> None:
        self._minute[key] = [t for t in self._minute[key] if now - t < 60]
        self._day[key] = [t for t in self._day[key] if now - t < 86400]

    def _check_memory(self, key: str, now: float) -> None:
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

    async def _check_redis(self, key: str, now: float) -> bool:
        """Returns True if the check was handled by Redis, False if we
        should fall back to the in-memory path.
        """
        r = self._redis
        if r is None:
            return False
        now_ms = int(now * 1000)
        minute_cutoff = now_ms - 60_000
        day_cutoff = now_ms - 86_400_000
        minute_key = f"rl:m:{key}"
        day_key = f"rl:d:{key}"
        try:
            pipe = r.pipeline(transaction=False)
            pipe.zremrangebyscore(minute_key, 0, minute_cutoff)
            pipe.zcard(minute_key)
            pipe.zremrangebyscore(day_key, 0, day_cutoff)
            pipe.zcard(day_key)
            _, minute_count, _, day_count = await pipe.execute()
        except Exception as e:
            if not self._redis_warned:
                logger.warning(
                    "Redis rate-limit check failed, falling back to in-memory: %s", e,
                )
                self._redis_warned = True
            return False

        if self.per_minute > 0 and int(minute_count) >= self.per_minute:
            raise HTTPException(
                429,
                f"Rate limit: {self.per_minute} requests/minute exceeded.",
            )
        if self.per_day > 0 and int(day_count) >= self.per_day:
            raise HTTPException(
                429,
                f"Daily limit: {self.per_day} requests/day exceeded.",
            )

        member = f"{now_ms}:{uuid.uuid4().hex[:8]}"
        try:
            pipe = r.pipeline(transaction=False)
            pipe.zadd(minute_key, {member: now_ms})
            pipe.expire(minute_key, 60)
            pipe.zadd(day_key, {member: now_ms})
            pipe.expire(day_key, 86_400)
            await pipe.execute()
        except Exception as e:
            if not self._redis_warned:
                logger.warning(
                    "Redis rate-limit write failed, falling back to in-memory: %s", e,
                )
                self._redis_warned = True
            return False
        return True

    async def check(self, key: str, role: str = "user") -> None:
        """Raise HTTPException 429 if `key` is over its minute/day budget.
        Counts the current request on success.
        """
        if role in EXEMPT_ROLES:
            return
        now = time.time()
        if await self._check_redis(key, now):
            return
        self._check_memory(key, now)


# Module-level singleton — imported by main.py.
rate_limiter = RateLimiter()

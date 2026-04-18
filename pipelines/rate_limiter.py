"""
title: Rate Limiter Filter
author: local-ai-stack
description: Limit requests per user to prevent GPU overload. Configurable per-minute and per-day limits.
required_open_webui_version: 0.4.0
version: 1.0.0
licence: MIT
"""

from typing import Optional, Callable, Any
from pydantic import BaseModel, Field
from collections import defaultdict
import time


class Filter:
    class Valves(BaseModel):
        MAX_REQUESTS_PER_MINUTE: int = Field(
            default=20,
            description="Maximum requests allowed per user per minute (0 = unlimited)",
        )
        MAX_REQUESTS_PER_DAY: int = Field(
            default=500,
            description="Maximum requests allowed per user per day (0 = unlimited)",
        )
        EXEMPT_ROLES: str = Field(
            default="admin",
            description="Comma-separated roles exempt from rate limiting",
        )

    def __init__(self):
        self.valves = self.Valves()
        # {user_id: [(timestamp, count), ...]}
        self._minute_counts: dict = defaultdict(list)
        self._day_counts: dict = defaultdict(list)

    def _cleanup(self, user_id: str, now: float):
        self._minute_counts[user_id] = [
            t for t in self._minute_counts[user_id] if now - t < 60
        ]
        self._day_counts[user_id] = [
            t for t in self._day_counts[user_id] if now - t < 86400
        ]

    async def inlet(
        self,
        body: dict,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> dict:
        if not __user__:
            return body

        role = __user__.get("role", "user")
        exempt_roles = [r.strip() for r in self.valves.EXEMPT_ROLES.split(",")]
        if role in exempt_roles:
            return body

        user_id = __user__.get("id", "anonymous")
        now = time.time()
        self._cleanup(user_id, now)

        if self.valves.MAX_REQUESTS_PER_MINUTE > 0:
            count_min = len(self._minute_counts[user_id])
            if count_min >= self.valves.MAX_REQUESTS_PER_MINUTE:
                raise Exception(
                    f"Rate limit: {self.valves.MAX_REQUESTS_PER_MINUTE} requests/minute exceeded. Please wait before sending another message."
                )

        if self.valves.MAX_REQUESTS_PER_DAY > 0:
            count_day = len(self._day_counts[user_id])
            if count_day >= self.valves.MAX_REQUESTS_PER_DAY:
                raise Exception(
                    f"Daily limit: {self.valves.MAX_REQUESTS_PER_DAY} requests/day exceeded. Limit resets at midnight."
                )

        self._minute_counts[user_id].append(now)
        self._day_counts[user_id].append(now)
        return body

    async def outlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        return body

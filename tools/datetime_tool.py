"""
title: Date & Time
author: local-ai-stack
description: Get current date and time in any timezone, calculate date differences, parse and format dates.
required_open_webui_version: 0.4.0
requirements: pytz
version: 1.0.0
licence: MIT
"""

from datetime import datetime, date, timedelta
from typing import Callable, Any, Optional
from pydantic import BaseModel, Field
import time


class Tools:
    class Valves(BaseModel):
        DEFAULT_TIMEZONE: str = Field(
            default="America/New_York",
            description="Default timezone when none is specified (IANA format)",
        )

    def __init__(self):
        self.valves = self.Valves()

    def get_current_datetime(
        self,
        timezone: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get the current date and time in a specified timezone.
        :param timezone: IANA timezone name (e.g. 'America/New_York', 'Europe/London', 'Asia/Tokyo'). Leave empty for UTC.
        :return: Current date and time with day of week
        """
        try:
            import pytz
            tz_name = timezone.strip() if timezone.strip() else self.valves.DEFAULT_TIMEZONE
            tz = pytz.timezone(tz_name)
            now = datetime.now(tz)
            return (
                f"**Current Date & Time ({tz_name}):**\n"
                f"- Date: {now.strftime('%A, %B %d, %Y')}\n"
                f"- Time: {now.strftime('%I:%M:%S %p')}\n"
                f"- ISO 8601: {now.isoformat()}\n"
                f"- UTC Offset: {now.strftime('%z')}\n"
                f"- Unix Timestamp: {int(now.timestamp())}"
            )
        except Exception:
            now_utc = datetime.utcnow()
            return (
                f"**Current Date & Time (UTC):**\n"
                f"- Date: {now_utc.strftime('%A, %B %d, %Y')}\n"
                f"- Time: {now_utc.strftime('%I:%M:%S %p')}\n"
                f"- ISO 8601: {now_utc.isoformat()}Z"
            )

    def calculate_date_difference(
        self,
        date1: str,
        date2: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Calculate the difference between two dates.
        :param date1: First date in YYYY-MM-DD format
        :param date2: Second date in YYYY-MM-DD format
        :return: Difference in days, weeks, months, and years
        """
        try:
            d1 = datetime.strptime(date1.strip(), "%Y-%m-%d").date()
            d2 = datetime.strptime(date2.strip(), "%Y-%m-%d").date()
            delta = abs((d2 - d1).days)
            weeks = delta // 7
            months = delta // 30
            years = delta // 365

            earlier, later = (d1, d2) if d1 <= d2 else (d2, d1)
            direction = "after" if d2 >= d1 else "before"

            return (
                f"**Date Difference: {date1} → {date2}**\n"
                f"- Days: {delta:,}\n"
                f"- Weeks: {weeks:,} (+ {delta % 7} days)\n"
                f"- Months: ~{months:,}\n"
                f"- Years: ~{years:,}\n"
                f"- {later.strftime('%B %d, %Y')} is {delta} days {direction} {earlier.strftime('%B %d, %Y')}"
            )
        except ValueError as e:
            return f"Date parse error: {e}. Use YYYY-MM-DD format."

    def add_days_to_date(
        self,
        start_date: str,
        days: int,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Add or subtract days from a date to find a future or past date.
        :param start_date: Starting date in YYYY-MM-DD format (use 'today' for today)
        :param days: Number of days to add (negative to subtract)
        :return: The resulting date with day of week
        """
        try:
            if start_date.lower().strip() == "today":
                d = date.today()
            else:
                d = datetime.strptime(start_date.strip(), "%Y-%m-%d").date()

            result = d + timedelta(days=days)
            direction = "after" if days >= 0 else "before"
            abs_days = abs(days)

            return (
                f"**Date Calculation:**\n"
                f"- Start: {d.strftime('%A, %B %d, %Y')}\n"
                f"- {abs_days} days {direction}\n"
                f"- Result: **{result.strftime('%A, %B %d, %Y')}** ({result.isoformat()})"
            )
        except ValueError as e:
            return f"Date parse error: {e}. Use YYYY-MM-DD format or 'today'."

    def get_timezone_list(self, region: str = "", __user__: Optional[dict] = None) -> str:
        """
        List common timezones, optionally filtered by region.
        :param region: Region filter (e.g. 'America', 'Europe', 'Asia', 'Pacific'). Leave empty for all.
        :return: List of matching timezone names
        """
        common_timezones = {
            "America": [
                "America/New_York", "America/Chicago", "America/Denver",
                "America/Los_Angeles", "America/Anchorage", "America/Honolulu",
                "America/Toronto", "America/Vancouver", "America/Mexico_City",
                "America/Sao_Paulo", "America/Buenos_Aires",
            ],
            "Europe": [
                "Europe/London", "Europe/Paris", "Europe/Berlin",
                "Europe/Rome", "Europe/Madrid", "Europe/Amsterdam",
                "Europe/Stockholm", "Europe/Moscow", "Europe/Istanbul",
            ],
            "Asia": [
                "Asia/Tokyo", "Asia/Shanghai", "Asia/Seoul",
                "Asia/Singapore", "Asia/Dubai", "Asia/Kolkata",
                "Asia/Bangkok", "Asia/Hong_Kong", "Asia/Taipei",
            ],
            "Pacific": [
                "Pacific/Auckland", "Pacific/Sydney", "Pacific/Fiji",
                "Pacific/Honolulu",
            ],
            "UTC": ["UTC", "Etc/GMT", "Etc/GMT+5", "Etc/GMT-5"],
        }

        if region:
            region = region.strip().title()
            zones = common_timezones.get(region, [])
            if not zones:
                return f"Unknown region: {region}. Try: America, Europe, Asia, Pacific, UTC"
            return f"**{region} Timezones:**\n" + "\n".join(f"- {z}" for z in zones)

        lines = ["**Common Timezones by Region:**"]
        for reg, zones in common_timezones.items():
            lines.append(f"\n**{reg}:**")
            lines.extend(f"  - {z}" for z in zones)
        return "\n".join(lines)

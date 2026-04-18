"""
title: Developer Utilities
author: local-ai-stack
description: Essential developer tools — cryptographic hashing, UUID generation, Base64 encode/decode, JSON formatting, regex testing, URL encoding, and color conversion. All run locally, no network needed.
required_open_webui_version: 0.4.0
requirements:
version: 1.0.0
licence: MIT
"""

import hashlib
import uuid
import base64
import json
import re
import urllib.parse
from pydantic import BaseModel
from typing import Optional


class Tools:
    class Valves(BaseModel):
        pass

    def __init__(self):
        self.valves = self.Valves()

    def hash_text(
        self,
        text: str,
        algorithm: str = "sha256",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Compute a cryptographic hash of any text string.
        :param text: The text to hash
        :param algorithm: Hash algorithm: md5, sha1, sha256, sha512, sha3_256, blake2b
        :return: Hex digest of the hash
        """
        algo = algorithm.lower().replace("-", "_")
        supported = ("md5", "sha1", "sha256", "sha512", "sha224", "sha384", "sha3_256", "sha3_512", "blake2b", "blake2s")
        if algo not in supported:
            return f"Unsupported algorithm: {algorithm}. Supported: {', '.join(supported)}"
        h = hashlib.new(algo, text.encode("utf-8")).hexdigest()
        return f"**{algorithm.upper()}** of `{text[:50]}{'...' if len(text)>50 else ''}`:\n```\n{h}\n```"

    def generate_uuid(
        self,
        version: int = 4,
        count: int = 1,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Generate one or more UUIDs.
        :param version: UUID version: 1 (time-based) or 4 (random). Default: 4
        :param count: Number of UUIDs to generate (1–20)
        :return: Generated UUID(s)
        """
        count = max(1, min(20, count))
        uuids = []
        for _ in range(count):
            if version == 1:
                uuids.append(str(uuid.uuid1()))
            else:
                uuids.append(str(uuid.uuid4()))
        if count == 1:
            return f"**UUID v{version}:**\n```\n{uuids[0]}\n```"
        return f"**{count} UUID v{version}s:**\n```\n" + "\n".join(uuids) + "\n```"

    def base64_encode(
        self,
        text: str,
        decode: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Encode text to Base64 or decode Base64 back to text.
        :param text: The string to encode or decode
        :param decode: Set to True to decode Base64 to text (default: False = encode)
        :return: Encoded or decoded result
        """
        try:
            if decode:
                # Add padding if needed
                padded = text.strip() + "=" * (-len(text.strip()) % 4)
                result = base64.b64decode(padded).decode("utf-8", errors="replace")
                return f"**Base64 Decoded:**\n```\n{result}\n```"
            else:
                result = base64.b64encode(text.encode("utf-8")).decode("ascii")
                return f"**Base64 Encoded:**\n```\n{result}\n```"
        except Exception as e:
            return f"Base64 error: {str(e)}"

    def format_json(
        self,
        json_text: str,
        indent: int = 2,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Validate and pretty-format a JSON string.
        :param json_text: The raw JSON string to format
        :param indent: Indentation spaces (default: 2)
        :return: Pretty-formatted JSON or error message with location
        """
        try:
            parsed = json.loads(json_text)
            formatted = json.dumps(parsed, indent=indent, ensure_ascii=False)
            keys = len(parsed) if isinstance(parsed, dict) else len(parsed) if isinstance(parsed, list) else 1
            type_name = type(parsed).__name__
            return f"**Valid JSON** ({type_name}, {keys} top-level items):\n```json\n{formatted}\n```"
        except json.JSONDecodeError as e:
            return f"**Invalid JSON:** {e}\nLine {e.lineno}, column {e.colno}: `{e.doc[max(0,e.pos-20):e.pos+20]}`"

    def test_regex(
        self,
        pattern: str,
        text: str,
        flags: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Test a regular expression pattern against a string and show all matches.
        :param pattern: The regex pattern to test (e.g. r"\\d+\\.\\d+", r"https?://\\S+")
        :param text: The text to test the pattern against
        :param flags: Optional flags: i (case-insensitive), m (multiline), s (dotall)
        :return: All matches found with their positions
        """
        try:
            re_flags = 0
            for f in flags.lower():
                if f == "i": re_flags |= re.IGNORECASE
                elif f == "m": re_flags |= re.MULTILINE
                elif f == "s": re_flags |= re.DOTALL
                elif f == "x": re_flags |= re.VERBOSE

            compiled = re.compile(pattern, re_flags)
            matches = list(compiled.finditer(text))

            if not matches:
                return f"**Pattern:** `{pattern}`\n**Result:** No matches found in the given text."

            lines = [f"**Pattern:** `{pattern}` — **{len(matches)} match(es)**\n"]
            for i, m in enumerate(matches[:10], 1):
                groups = m.groups()
                lines.append(f"Match {i}: `{m.group(0)}` at position {m.start()}–{m.end()}")
                if groups:
                    for j, g in enumerate(groups, 1):
                        lines.append(f"  Group {j}: `{g}`")

            if len(matches) > 10:
                lines.append(f"...and {len(matches)-10} more matches")

            return "\n".join(lines)

        except re.error as e:
            return f"**Invalid regex:** {e}"
        except Exception as e:
            return f"Regex error: {str(e)}"

    def url_encode(
        self,
        text: str,
        decode: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        URL-encode a string (percent-encoding) or decode a URL-encoded string.
        :param text: The string to encode or decode
        :param decode: Set True to decode URL encoding (default: False = encode)
        :return: Encoded or decoded result
        """
        try:
            if decode:
                result = urllib.parse.unquote(text)
                return f"**URL Decoded:**\n`{result}`"
            else:
                result = urllib.parse.quote(text, safe="")
                return f"**URL Encoded:**\n`{result}`"
        except Exception as e:
            return f"URL encoding error: {str(e)}"

    def color_convert(
        self,
        color: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Convert between HEX, RGB, and HSL color formats.
        :param color: Color in any format: HEX (#FF5733), RGB (255,87,51), or HSL (11,100%,60%)
        :return: Color in all three formats
        """
        try:
            color = color.strip()
            r = g = b = 0

            if color.startswith("#"):
                c = color.lstrip("#")
                if len(c) == 3:
                    c = "".join(ch*2 for ch in c)
                r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
            elif color.lower().startswith("rgb"):
                nums = re.findall(r"\d+", color)
                r, g, b = int(nums[0]), int(nums[1]), int(nums[2])
            else:
                nums = re.findall(r"[\d.]+", color)
                if len(nums) == 3:
                    h, s, l_val = float(nums[0]), float(nums[1]), float(nums[2])
                    s /= 100; l_val /= 100
                    c_val = (1 - abs(2*l_val - 1)) * s
                    x = c_val * (1 - abs((h/60) % 2 - 1))
                    m = l_val - c_val/2
                    if h < 60:   r1,g1,b1 = c_val,x,0
                    elif h < 120: r1,g1,b1 = x,c_val,0
                    elif h < 180: r1,g1,b1 = 0,c_val,x
                    elif h < 240: r1,g1,b1 = 0,x,c_val
                    elif h < 300: r1,g1,b1 = x,0,c_val
                    else:         r1,g1,b1 = c_val,0,x
                    r, g, b = int((r1+m)*255), int((g1+m)*255), int((b1+m)*255)

            # HEX
            hex_val = f"#{r:02X}{g:02X}{b:02X}"
            # HSL
            r1, g1, b1 = r/255, g/255, b/255
            cmax, cmin = max(r1,g1,b1), min(r1,g1,b1)
            delta = cmax - cmin
            l_val = (cmax + cmin) / 2
            s_val = 0 if delta == 0 else delta / (1 - abs(2*l_val - 1))
            if delta == 0: h_val = 0
            elif cmax == r1: h_val = 60 * (((g1-b1)/delta) % 6)
            elif cmax == g1: h_val = 60 * ((b1-r1)/delta + 2)
            else:            h_val = 60 * ((r1-g1)/delta + 4)

            return (
                f"**Color:** {color}\n"
                f"- **HEX:** `{hex_val}`\n"
                f"- **RGB:** `rgb({r}, {g}, {b})`\n"
                f"- **HSL:** `hsl({h_val:.0f}, {s_val*100:.0f}%, {l_val*100:.0f}%)`"
            )

        except Exception as e:
            return f"Color conversion error: {str(e)}\nSupported formats: #FF5733, rgb(255,87,51), hsl(11,100%,60%)"

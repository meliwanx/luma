"""Public settings the installed clients need, separate from the product brand."""

from __future__ import annotations

import os
import re

# Tencent Cloud's public Agent Runtime data-plane suffix. It is not an
# organization-specific host. Deployments replace it with BROWSER_LIVE_HOST_SUFFIXES.
DEFAULT_BROWSER_LIVE_HOST_SUFFIX = ".tencentags.com"

_SUFFIX = re.compile(r"^\.[a-z0-9.-]+$")


def browser_live_host_suffixes() -> list[str]:
    """Host suffixes a client may open for a browser live view.

    An unset or empty ``BROWSER_LIVE_HOST_SUFFIXES`` keeps the public default.
    A configured list replaces that default. Invalid items are dropped, and if
    nothing valid remains the default is used again.
    """

    raw = os.environ.get("BROWSER_LIVE_HOST_SUFFIXES")
    if raw is None or not raw.strip():
        return [DEFAULT_BROWSER_LIVE_HOST_SUFFIX]
    parsed = _parse_suffixes(raw)
    return parsed or [DEFAULT_BROWSER_LIVE_HOST_SUFFIX]


def _parse_suffixes(raw: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in raw.split(","):
        text = item.strip().lower().rstrip(".")
        if not text or any(mark in text for mark in ("/", "\\", "@", " ", ":")):
            continue
        if ".." in text:
            continue
        if not text.startswith("."):
            text = "." + text
        if not _SUFFIX.fullmatch(text):
            continue
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result

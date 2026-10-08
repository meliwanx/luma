"""Product display brand.

Wire-protocol identifiers stay fixed so clients that are already shipped keep
working. Do not rename them when the product display name changes:

- tool names prefixed with ``luma.`` (and the ``luma-ui`` widget fence)
- request headers ``X-Luma-*``
- Redis key prefixes
- the session cookie ``luma_session``

Those strings are a compatibility contract, not copy. Display names, prompts,
notification titles and export filename prefixes come from ``get_brand()``.

Process identifiers also stay put: advisory-lock names, the database
application name (``DB_APPLICATION_NAME``), the systemd unit
(``LUMA_SERVICE_NAME``) and runtime User-Agent strings. Renaming them would
break locks, metrics and existing deployments.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional


class BrandConfigError(ValueError):
    """Raised when brand environment or JSON configuration is unusable."""


_log = logging.getLogger(__name__)
_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


_FIELDS = (
    "product_name",
    "assistant_name",
    "tagline",
    "company_name",
    "support_url",
    "logo_url",
    "primary_color",
)

_ENV_NAMES = {
    "product_name": "BRAND_PRODUCT_NAME",
    "assistant_name": "BRAND_ASSISTANT_NAME",
    "tagline": "BRAND_TAGLINE",
    "company_name": "BRAND_COMPANY_NAME",
    "support_url": "BRAND_SUPPORT_URL",
    "logo_url": "BRAND_LOGO_URL",
    "primary_color": "BRAND_PRIMARY_COLOR",
}

_DEFAULTS = {
    "product_name": "Luma",
    "assistant_name": "",
    "tagline": "你的个人 AI 助理",
    "company_name": "",
    "support_url": "",
    "logo_url": "",
    "primary_color": "#2563EB",
}

_LIMITS = {
    "product_name": 80,
    "assistant_name": 80,
    "tagline": 200,
    "company_name": 120,
    "support_url": 500,
    "logo_url": 500,
    "primary_color": 32,
}

_ASSET_NAMES = ("logo.svg", "favicon.svg")
_BUILTIN_ASSETS = Path(__file__).resolve().parent / "brand_assets"
_SLUG = re.compile(r"[^a-z0-9]+")
_TOKEN_ENV = tuple(_ENV_NAMES.values()) + ("BRAND_CONFIG",)

_cache_lock = threading.Lock()
_cache_token: Optional[tuple] = None
_cache_brand: Optional["Brand"] = None


@dataclass(frozen=True)
class Brand:
    product_name: str
    assistant_name: str
    tagline: str
    company_name: str
    support_url: str
    logo_url: Optional[str]
    primary_color: str

    def public_dict(self) -> Dict[str, Optional[str]]:
        """Return only the public brand fields, in a stable order."""
        return {
            "product_name": self.product_name,
            "assistant_name": self.assistant_name,
            "tagline": self.tagline,
            "company_name": self.company_name,
            "support_url": self.support_url,
            "logo_url": self.logo_url,
            "primary_color": self.primary_color,
        }


class LiveText:
    """Text rebuilt on each read so a brand change is visible without a restart."""

    def __init__(self, render: Callable[[], str]) -> None:
        self._render = render

    def __str__(self) -> str:
        return self._render()

    def __repr__(self) -> str:
        return repr(str(self))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, LiveText):
            return str(self) == str(other)
        return str(self) == other

    def __hash__(self) -> int:
        return hash(str(self))

    def __contains__(self, item: object) -> bool:
        return str(item) in str(self)

    def __add__(self, other: object) -> str:
        return str(self) + str(other)

    def __radd__(self, other: object) -> str:
        return str(other) + str(self)

    def __len__(self) -> int:
        return len(str(self))

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(str(self))


def clear_brand_cache() -> None:
    """Drop the cached brand. Tests use this after rewriting a config file."""
    global _cache_token, _cache_brand
    with _cache_lock:
        _cache_token = None
        _cache_brand = None


def get_brand() -> Brand:
    """Return the process brand. Environment wins over ``BRAND_CONFIG`` JSON."""
    global _cache_token, _cache_brand
    with _cache_lock:
        token = _config_token()
        if _cache_brand is not None and token == _cache_token:
            return _cache_brand
        brand = _load_brand()
        _cache_token = token
        _cache_brand = brand
        return brand


def product_slug() -> str:
    """Filesystem-safe prefix derived from the product name. Default is ``luma``."""
    raw = get_brand().product_name.casefold()
    slug = _SLUG.sub("-", raw).strip("-")
    return (slug or "assistant")[:48]


def resolve_brand_asset(name: str) -> Optional[Path]:
    """Resolve ``logo.svg`` or ``favicon.svg``.

    ``BRAND_ASSETS_DIR`` wins when it exists and contains the file. A missing
    directory, or a missing file inside it, falls back to the built-in artwork.
    """
    if name not in _ASSET_NAMES:
        return None
    root = _assets_dir()
    if root is not None:
        candidate = _safe_file(root, name)
        if candidate is not None:
            return candidate
    builtin = _safe_file(_BUILTIN_ASSETS, name)
    return builtin


def _config_token() -> tuple:
    env_values = tuple(os.environ.get(name) for name in _TOKEN_ENV)
    path = os.environ.get("BRAND_CONFIG", "").strip()
    stamp: Optional[tuple] = None
    if path:
        try:
            stat = os.stat(path)
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None
    return (env_values, path, stamp)


def _load_brand() -> Brand:
    values = dict(_DEFAULTS)
    path = os.environ.get("BRAND_CONFIG", "").strip()
    if path:
        _overlay(values, _read_json(path))
    env_values = {}
    for field, env_name in _ENV_NAMES.items():
        if env_name in os.environ:
            env_values[field] = os.environ[env_name]
    _overlay(values, env_values)
    if not values["assistant_name"]:
        values["assistant_name"] = values["product_name"]
    logo = values["logo_url"].strip()
    # An unset logo stays null. Clients then use the built-in wordmark only
    # when the product name is Luma, and a text mark otherwise.
    values["logo_url"] = logo or None
    values["primary_color"] = _coerce_primary_color(values["primary_color"])
    return Brand(
        product_name=values["product_name"],
        assistant_name=values["assistant_name"],
        tagline=values["tagline"],
        company_name=values["company_name"],
        support_url=values["support_url"],
        logo_url=values["logo_url"],
        primary_color=values["primary_color"],
    )


def _coerce_primary_color(value: str) -> str:
    text = value.strip()
    if _HEX_COLOR.fullmatch(text):
        return text
    if text:
        _log.warning(
            "brand primary_color %r is not #RRGGBB; using %s",
            text,
            _DEFAULTS["primary_color"],
        )
    return _DEFAULTS["primary_color"]


def _overlay(values: Dict[str, str], source: Dict[str, object]) -> None:
    for field in _FIELDS:
        if field not in source:
            continue
        values[field] = _clean(field, source[field])


def _clean(field: str, value: object) -> str:
    if not isinstance(value, str):
        raise BrandConfigError("brand field %s must be a string" % field)
    text = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise BrandConfigError("brand field %s contains control characters" % field)
    if len(text) > _LIMITS[field]:
        raise BrandConfigError("brand field %s is too long" % field)
    if field == "product_name" and not text:
        raise BrandConfigError("brand field product_name is empty")
    return text


def _read_json(path: str) -> Dict[str, object]:
    try:
        raw = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise BrandConfigError("BRAND_CONFIG file is not readable") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BrandConfigError("BRAND_CONFIG is not valid JSON") from exc
    if not isinstance(data, dict):
        raise BrandConfigError("BRAND_CONFIG must be a JSON object")
    return data


def _assets_dir() -> Optional[Path]:
    raw = os.environ.get("BRAND_ASSETS_DIR", "").strip()
    if not raw:
        return None
    try:
        root = Path(raw).expanduser().resolve()
    except OSError:
        return None
    if not root.is_dir():
        return None
    return root


def _safe_file(root: Path, name: str) -> Optional[Path]:
    try:
        base = root.resolve()
        candidate = (base / name).resolve()
        candidate.relative_to(base)
    except (OSError, ValueError):
        return None
    if candidate.is_file():
        return candidate
    return None

"""Instantiate login providers from AUTH_PROVIDERS and publish their safe config."""

from __future__ import annotations

from typing import Any

_ALLOWED = ("password", "sso")
_INVALID_PROVIDERS = "AUTH_PROVIDERS must be a comma-separated list of: password, sso"
_EMPTY_PROVIDERS = "AUTH_PROVIDERS must list at least one provider"


def _provider_selection() -> tuple[list[str], str | None]:
    """Parse AUTH_PROVIDERS without raising, so import can mount routes safely."""

    from ..auth import _env

    raw = _env("AUTH_PROVIDERS", "password")
    if not raw:
        return [], _EMPTY_PROVIDERS
    names: list[str] = []
    for part in raw.split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name not in _ALLOWED:
            return [], _INVALID_PROVIDERS
        if name not in names:
            names.append(name)
    if not names:
        return [], _EMPTY_PROVIDERS
    return names, None


def enabled_provider_names() -> list[str]:
    names, error = _provider_selection()
    if error:
        raise RuntimeError(error)
    return names


def _provider_classes() -> dict[str, Any]:
    from .password import PasswordProvider
    from .sso import SsoProvider

    return {"password": PasswordProvider, "sso": SsoProvider}


def build_providers() -> list[Any]:
    classes = _provider_classes()
    return [classes[name]() for name in enabled_provider_names()]


def validate_provider_configuration() -> None:
    """Fail startup when an enabled provider is missing required settings."""

    names = enabled_provider_names()
    if "password" in names:
        from ..auth import _env

        if _env("AUTH_REGISTRATION", "invite").lower() not in {"open", "invite", "closed"}:
            raise RuntimeError("AUTH_REGISTRATION must be open, invite, or closed")
    if "sso" in names:
        from .sso import missing_sso_settings

        missing = [item for item in missing_sso_settings() if item != "AUTH_SESSION_SECRET"]
        if missing:
            raise RuntimeError("SSO provider requires " + ", ".join(missing))


def public_providers() -> dict[str, Any]:
    """Login page payload. Provider secrets are not copied into this object."""

    from ..auth import _env

    account_label = _env("AUTH_ACCOUNT_LABEL") or "账号"
    sso_label = _env("AUTH_SSO_LABEL") or "单点登录"
    payload: dict[str, Any] = {
        "providers": [],
        "registration_open": False,
        "requires_invite": False,
        "bootstrap_required": False,
        "account_label": account_label,
        "sso_label": sso_label,
    }
    for provider in build_providers():
        config = provider.public_config()
        payload["providers"].extend(config.get("entries") or [])
        for key in ("registration_open", "requires_invite", "bootstrap_required", "account_label", "sso_label"):
            if key in config and config[key] is not None:
                payload[key] = config[key]
    return payload


def mount_providers(app: Any) -> None:
    # Invalid configuration is reported by validate_provider_configuration at
    # startup. Importing the application must not raise for a bad env value.
    names, error = _provider_selection()
    if error:
        return
    classes = _provider_classes()
    for name in names:
        app.include_router(classes[name]().routes())

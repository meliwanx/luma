"""Best-effort mobile push adapters.

The push layer is deliberately independent from notification persistence.  A
provider outage (or a missing provider credential) is swallowed here so a
notification can still be written to the durable inbox and SSE stream.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .db import get_connection

logger = logging.getLogger(__name__)

_DISABLED_LOGGED: set[str] = set()
_DISABLED_LOCK = threading.Lock()
_SENSITIVE_KEY = re.compile(r"(?:token|secret|password|passwd|private|api[_-]?key|credential)", re.I)


def _log_disabled_once(provider: str) -> None:
    with _DISABLED_LOCK:
        if provider in _DISABLED_LOGGED:
            return
        _DISABLED_LOGGED.add(provider)
    logger.info("%s push provider is disabled because credentials are not configured", provider.upper())


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _apns_configured() -> bool:
    required = all(_env(name) for name in ("APNS_KEY_ID", "APNS_TEAM_ID", "APNS_BUNDLE_ID", "APNS_KEY_PATH"))
    return required and Path(_env("APNS_KEY_PATH")).is_file()


def _fcm_configured() -> bool:
    path = _env("FCM_SERVICE_ACCOUNT_PATH")
    return bool(_env("FCM_PROJECT_ID") and path and Path(path).is_file())


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _short_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _safe_data(data: Any) -> dict[str, str]:
    """Keep provider payloads small and avoid copying credential-shaped values."""

    if not isinstance(data, dict):
        return {}
    output: dict[str, str] = {}
    for key, value in list(data.items())[:12]:
        key_text = _short_text(key, 64)
        if not key_text or _SENSITIVE_KEY.search(key_text):
            continue
        if isinstance(value, (str, int, float, bool)):
            output[key_text] = _short_text(value, 200)
    return output


def _payload(title: str, body: str, data: Any) -> tuple[str, str, dict[str, str]]:
    return _short_text(title, 120), _short_text(body, 500), _safe_data(data)


def _delete_device(device_id: str) -> None:
    try:
        with get_connection() as conn:
            conn.execute("DELETE FROM push_devices WHERE id = ?", (device_id,))
    except Exception:
        logger.debug("failed to delete invalid push device", exc_info=True)


def _list_devices(user_id: str, platform: str) -> list[dict[str, Any]]:
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT id, platform, token FROM push_devices WHERE user_id = ? AND platform = ?",
                (user_id, platform),
            ).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        logger.debug("failed to load push devices", exc_info=True)
        return []


def _jwt_es256(key_path: str, key_id: str, team_id: str) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    with open(key_path, "rb") as stream:
        key = serialization.load_pem_private_key(stream.read(), password=None)
    issued_at = int(time.time())
    header = _b64(json.dumps({"alg": "ES256", "kid": key_id}, separators=(",", ":")).encode("utf-8"))
    claims = _b64(json.dumps({"iss": team_id, "iat": issued_at}, separators=(",", ":")).encode("utf-8"))
    signing_input = (header + "." + claims).encode("ascii")
    signature = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r_value, s_value = decode_dss_signature(signature)
    raw_signature = r_value.to_bytes(32, "big") + s_value.to_bytes(32, "big")
    return header + "." + claims + "." + _b64(raw_signature)


def _send_apns(device: dict[str, Any], title: str, body: str, data: dict[str, str]) -> None:
    if not _apns_configured():
        _log_disabled_once("apns")
        return
    try:
        import httpx

        token = str(device.get("token") or "")
        if not token:
            return
        authorization = _jwt_es256(_env("APNS_KEY_PATH"), _env("APNS_KEY_ID"), _env("APNS_TEAM_ID"))
        payload: dict[str, Any] = {"aps": {"alert": {"title": title, "body": body}}}
        payload.update(data)
        headers = {
            "authorization": "bearer " + authorization,
            "apns-topic": _env("APNS_BUNDLE_ID"),
            "apns-push-type": "alert",
        }
        with httpx.Client(http2=True, timeout=10.0, follow_redirects=False, trust_env=False) as client:
            response = client.post("https://api.push.apple.com/3/device/" + token, headers=headers, json=payload)
        if response.status_code == 410:
            _delete_device(str(device.get("id") or ""))
        elif response.status_code >= 400:
            logger.warning("APNs push failed with status %s", response.status_code)
    except Exception:
        logger.warning("APNs push failed", exc_info=True)


def _load_fcm_credentials(path: str) -> dict[str, str]:
    with open(path, "r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("FCM service account must be a JSON object")
    email = str(value.get("client_email") or "").strip()
    private_key = str(value.get("private_key") or "")
    token_uri = str(value.get("token_uri") or "https://oauth2.googleapis.com/token").strip()
    if not email or not private_key:
        raise ValueError("FCM service account is missing client_email or private_key")
    return {"client_email": email, "private_key": private_key, "token_uri": token_uri}


def _jwt_rs256(credentials: dict[str, str]) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    issued_at = int(time.time())
    header = _b64(b'{"alg":"RS256","typ":"JWT"}')
    claims = _b64(
        json.dumps(
            {
                "iss": credentials["client_email"],
                "scope": "https://www.googleapis.com/auth/firebase.messaging",
                "aud": credentials["token_uri"],
                "iat": issued_at,
                "exp": issued_at + 3600,
            },
            separators=(",", ":"),
        ).encode("utf-8")
    )
    signing_input = (header + "." + claims).encode("ascii")
    key = serialization.load_pem_private_key(credentials["private_key"].encode("utf-8"), password=None)
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return header + "." + claims + "." + _b64(signature)


def _fcm_access_token(credentials: dict[str, str]) -> str:
    import httpx

    assertion = _jwt_rs256(credentials)
    with httpx.Client(timeout=10.0, follow_redirects=False, trust_env=False) as client:
        response = client.post(
            credentials["token_uri"],
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
    response.raise_for_status()
    value = response.json()
    token = value.get("access_token") if isinstance(value, dict) else None
    if not token:
        raise ValueError("FCM token exchange returned no access token")
    return str(token)


def _send_fcm(device: dict[str, Any], title: str, body: str, data: dict[str, str]) -> None:
    if not _fcm_configured():
        _log_disabled_once("fcm")
        return
    try:
        import httpx

        token = str(device.get("token") or "")
        if not token:
            return
        credentials = _load_fcm_credentials(_env("FCM_SERVICE_ACCOUNT_PATH"))
        access_token = _fcm_access_token(credentials)
        project_id = _env("FCM_PROJECT_ID")
        payload = {
            "message": {
                "token": token,
                "notification": {"title": title, "body": body},
                "data": data,
            }
        }
        url = "https://fcm.googleapis.com/v1/projects/{}/messages:send".format(project_id)
        with httpx.Client(timeout=10.0, follow_redirects=False, trust_env=False) as client:
            response = client.post(url, headers={"authorization": "Bearer " + access_token}, json=payload)
        # FCM uses a structured ``UNREGISTERED`` error for stale tokens.
        # Other 4xx responses (for example a misconfigured project) must not
        # destroy every device token, because the configuration may be fixed
        # without requiring users to register again.
        invalid = False
        try:
            error_value = response.json()
            invalid = "UNREGISTERED" in json.dumps(error_value, ensure_ascii=False).upper()
        except Exception:
            invalid = False
        if invalid:
            _delete_device(str(device.get("id") or ""))
        elif response.status_code >= 400:
            logger.warning("FCM push failed with status %s", response.status_code)
    except Exception:
        logger.warning("FCM push failed", exc_info=True)


def send_push(user_id: str, title: str, body: str, data: Optional[dict[str, Any]] = None) -> None:
    """Send a best-effort push to all registered devices for ``user_id``.

    This function never raises to its caller.  Device tokens are deleted when
    APNs or FCM explicitly reports that they are no longer valid.
    """

    try:
        title_text, body_text, payload_data = _payload(title, body, data)
        apns_configured = _apns_configured()
        fcm_configured = _fcm_configured()
        if not apns_configured and not fcm_configured:
            _log_disabled_once("push")
            return
        providers: tuple[tuple[str, Any, bool], ...] = (
            ("apns", _send_apns, apns_configured),
            ("fcm", _send_fcm, fcm_configured),
        )
        for provider, sender, configured in providers:
            if not configured:
                _log_disabled_once(provider)
                continue
            for device in _list_devices(user_id, provider):
                sender(device, title_text, body_text, payload_data)
    except Exception:
        logger.warning("push dispatch failed", exc_info=True)

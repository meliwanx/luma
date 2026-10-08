"""Enroll credentials for an upgraded legacy account, preserving ownership."""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException
from psycopg2 import IntegrityError

from app import auth
from app.db import get_connection


def _profile_job_number(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return ""
    if isinstance(value, dict):
        return str(value.get("job_number") or "").strip()
    return ""


def _can_enroll(row: Any) -> bool:
    """Only a credential-less legacy_ row with no SSO identity can be enrolled."""

    if row is None or row["password_hash"]:
        return False
    provider = str(row.get("auth_provider") or "").strip().lower()
    if provider:
        return False
    if str(row.get("job_number") or "").strip() or _profile_job_number(row.get("profile")):
        return False
    return str(row.get("user_id") or "").startswith("legacy_")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("user_id", help="Existing ownership identifier")
    parser.add_argument("--username", required=True)
    parser.add_argument("--admin", action="store_true", help="Grant the administrator role")
    args = parser.parse_args(argv)
    if re.fullmatch(r"[a-zA-Z0-9_.-]{3,32}", args.username) is None:
        parser.error("username must be 3-32 characters from letters, digits, underscore, dot or dash")
    password = getpass.getpass("New password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        print("Passwords do not match", file=sys.stderr)
        return 1
    try:
        auth.validate_password(password, args.username)
        encoded = auth.hash_password(password)
        with get_connection() as conn:
            row = conn.execute(
                "SELECT user_id, password_hash, auth_provider, job_number, profile "
                "FROM users WHERE user_id = ? FOR UPDATE",
                (args.user_id,),
            ).fetchone()
            if not _can_enroll(row):
                print("Only an existing account without credentials can be enrolled", file=sys.stderr)
                return 1
            conn.execute(
                "UPDATE users SET username = ?, password_hash = ?, password_changed_at = ?, status = 'active', "
                "auth_provider = 'password', "
                "role = CASE WHEN ? THEN 'admin' ELSE role END, session_version = session_version + 1 WHERE user_id = ?",
                (args.username, encoded, auth._timestamp(), args.admin, args.user_id),
            )
    except HTTPException:
        print("Password does not meet the account password rules", file=sys.stderr)
        return 1
    except IntegrityError:
        print("Username is already in use", file=sys.stderr)
        return 1
    try:
        auth.revoke_user_sessions(args.user_id)
    except HTTPException:
        print("Credentials saved; database session invalidation is active, but cache cleanup is unavailable", file=sys.stderr)
    print("Legacy account enrolled; its ownership identifier is unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Server-side account administration: python -m app.cli create-admin."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
import threading
from typing import Any, Optional, Sequence

from fastapi import HTTPException
from psycopg2 import IntegrityError

from . import auth
from .db import ensure_db, get_connection

_migration_lock = threading.Lock()


def create_admin_account(
    username: str, password: str, email: Optional[str] = None,
    display_name: Optional[str] = None, *, force_additional_admin: bool = False,
) -> dict[str, Any]:
    """Migrate and create an admin without a bootstrap token or login session."""
    # Use the FastAPI migration entry point without Alembic's console logs.
    # Serialize changes to the logging threshold for in-process CLI callers.
    with _migration_lock:
        previous_level = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        try:
            ensure_db()
        finally:
            logging.disable(previous_level)
    email, display_name, encoded = auth._prepare_account(username, password, email, display_name)
    try:
        with get_connection() as conn:
            # Keep detection and insertion in one transaction under the same
            # lock as HTTP registration, including across worker processes.
            conn.execute("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE")
            has_users = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
            if has_users and not force_additional_admin:
                raise HTTPException(
                    status_code=403,
                    detail="数据库已有用户；如需额外管理员，请使用 --force-additional-admin",
                )
            row = auth._insert_account(conn, username, email, display_name, encoded, "admin", logged_in=False)
    except IntegrityError:
        raise HTTPException(status_code=409, detail="用户名或邮箱已被使用") from None
    return auth.public_user(row)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-admin", help="Create a server administrator")
    create.add_argument("--username", required=True)
    create.add_argument("--email")
    create.add_argument("--display-name")
    create.add_argument("--password-stdin", action="store_true", help="Read one password line from stdin")
    create.add_argument(
        "--force-additional-admin", action="store_true",
        help="Allow another administrator when the database already has users",
    )
    args = parser.parse_args(argv)
    try:
        if args.password_stdin:
            line = sys.stdin.readline()
            if not line:
                print("未从 stdin 读取到密码", file=sys.stderr)
                return 1
            password = line.rstrip("\r\n")
        else:
            password = getpass.getpass("Password: ")
            confirmation = getpass.getpass("Confirm password: ")
            if password != confirmation:
                print("两次输入的密码不一致", file=sys.stderr)
                return 1
        user = create_admin_account(
            args.username, password, args.email, args.display_name,
            force_additional_admin=args.force_additional_admin,
        )
    except HTTPException as exc:
        print(exc.detail, file=sys.stderr)
        return {403: 2, 409: 3}.get(exc.status_code, 1)
    except (EOFError, KeyboardInterrupt):
        print("已取消管理员创建", file=sys.stderr)
        return 1
    except Exception:
        # Database exceptions can contain connection settings or bound values.
        print("管理员创建失败；请检查数据库配置与迁移状态", file=sys.stderr)
        return 1
    print("username=%s role=%s" % (user["username"], user["role"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

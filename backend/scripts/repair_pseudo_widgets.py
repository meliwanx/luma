"""Repair legacy assistant messages that contain fake luma tool-call markup.

Run from ``backend`` as ``python scripts/repair_pseudo_widgets.py`` for a
dry-run, or add ``--apply`` to persist the repaired messages.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.db import ensure_db, get_connection  # noqa: E402
from app.widgets import extract_widgets, strip_pseudo_markup  # noqa: E402


def _candidates() -> list[dict[str, str]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, user_id, session_id, content FROM messages "
            "WHERE role = ? AND (LOWER(content) LIKE ? OR LOWER(content) LIKE ? OR LOWER(content) LIKE ?) "
            "ORDER BY id",
            ("assistant", "%<tool_call>%", "%<function_calls>%", "%<function=luma%"),
        ).fetchall()
    return [dict(row) for row in rows]


def _has_component(conn, message: dict[str, str]) -> bool:
    if "[[widget:" in message["content"]:
        return True
    return conn.execute(
        "SELECT 1 FROM widgets WHERE message_id = ? LIMIT 1", (message["id"],)
    ).fetchone() is not None


def repair(*, apply: bool) -> list[str]:
    candidates = _candidates()
    changed: list[str] = []
    if not apply:
        for message in candidates:
            # Extraction stores widgets, so dry-run only previews the markup
            # removal. The apply path performs the real conversion.
            new_content = strip_pseudo_markup(message["content"])
            if new_content != message["content"]:
                changed.append(message["id"])
        return changed

    for message in candidates:
        with get_connection() as conn:
            has_component = _has_component(conn, message)
        if has_component:
            new_content = strip_pseudo_markup(message["content"])
        else:
            new_content, _, _ = extract_widgets(
                message["content"], user_id=message["user_id"],
                session_id=message["session_id"], message_id=message["id"],
            )
        if new_content == message["content"]:
            continue
        with get_connection() as conn:
            conn.execute(
                "UPDATE messages SET content = ? WHERE id = ?",
                (new_content, message["id"]),
            )
        changed.append(message["id"])
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair legacy pseudo widget markup")
    parser.add_argument("--apply", action="store_true", help="write repaired message content")
    args = parser.parse_args()
    ensure_db()
    changed = repair(apply=args.apply)
    mode = "applied" if args.apply else "dry-run"
    print(f"{mode}: {len(changed)} message(s)")
    if changed:
        print("ids: " + ", ".join(changed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

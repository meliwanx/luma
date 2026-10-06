"""Bound model-visible tool results without cutting through JSON values."""

import json
import os
from typing import Any


def tool_result_max_chars() -> int:
    try:
        value = int(os.getenv("AGENT_TOOL_RESULT_MAX_CHARS", "48000"))
    except (TypeError, ValueError):
        value = 48000
    return max(4000, min(200000, value))


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def _bounded_json(value: Any, limit: int) -> str:
    # The parsed value is private to this formatter; callers' event data stays
    # intact. A top-level array needs an envelope to carry truncation metadata.
    if isinstance(value, list):
        value = {"items": value}
    if not isinstance(value, dict):
        return _compact({"_truncated": {"kept": 0, "total": 1}})

    arrays = []
    fields = []

    def collect(node: dict) -> None:
        for key, item in node.items():
            if key == "_truncated":
                continue
            if isinstance(item, list):
                arrays.append((key not in {"rows", "data", "items", "records", "results"}, len(_compact(item)), node, key, item))
            elif isinstance(item, dict):
                collect(item)
            else:
                fields.append((len(_compact({key: item})), node, key))

    collect(value)
    arrays.sort(key=lambda item: (item[0], -item[1]))
    previous = value.get("_truncated")
    omitted, omitted_fields = 0, 0
    if isinstance(previous, dict):
        kept, total = previous.get("kept"), previous.get("total")
        if isinstance(kept, int) and isinstance(total, int):
            omitted = max(0, total - kept)
        if isinstance(previous.get("omitted_fields"), int):
            omitted_fields = max(0, previous["omitted_fields"])
    total = omitted + (sum(len(item[4]) for item in arrays) if arrays else len(fields))
    removed_fields = 0

    def mark() -> None:
        kept = sum(len(parent[key]) for _priority, _size, parent, key, _rows in arrays) if arrays else len(fields) - removed_fields
        marker = {"kept": kept, "total": total}
        if omitted_fields + removed_fields:
            marker["omitted_fields"] = omitted_fields + removed_fields
        value["_truncated"] = marker

    # First budget the metadata with empty arrays. If it alone is too large,
    # omit whole scalar fields before deciding how many complete rows fit.
    for _priority, _size, parent, key, _rows in arrays:
        parent[key] = []
    mark()
    for _size, parent, key in sorted(fields, key=lambda item: -item[0]):
        if len(_compact(value)) <= limit:
            break
        del parent[key]
        removed_fields += 1
        mark()
    if len(_compact(value)) > limit:
        # Even container keys can exhaust the budget. Keep an honest, legal
        # envelope when no useful structure can fit without cutting strings.
        marker = {"kept": 0, "total": total}
        if omitted_fields + removed_fields:
            marker["omitted_fields"] = omitted_fields + removed_fields
        return _compact({"_truncated": marker})

    for _priority, _size, parent, key, rows in arrays:
        parent[key] = rows
    mark()
    if len(_compact(value)) <= limit:
        return _compact(value)
    for _priority, _size, parent, key, rows in arrays:
        parent[key] = []
        mark()
        if len(_compact(value)) > limit:
            continue
        low, high = 0, len(rows)
        while low < high:
            middle = (low + high + 1) // 2
            parent[key] = rows[:middle]
            mark()
            if len(_compact(value)) <= limit:
                low = middle
            else:
                high = middle - 1
        parent[key] = rows[:low]
        mark()
        return _compact(value)
    return _compact(value)


def tool_result_text(value: Any) -> str:
    limit = tool_result_max_chars()
    try:
        text = value if isinstance(value, str) else _compact(value)
    except (TypeError, ValueError):
        text = str(value)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        if len(text) <= limit:
            return text
        suffix = "…（已截断，共 %d 字）" % len(text)
        return text[:limit - len(suffix)] + suffix
    compact = _compact(parsed)
    return compact if len(compact) <= limit else _bounded_json(parsed, limit)

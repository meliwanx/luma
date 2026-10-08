"""Validated, user-owned conversation widgets and their interaction state."""

from __future__ import annotations

import json
import logging
import math
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from .brand import LiveText, get_brand
from .db import get_connection
from . import mcp
from .tool_results import tool_result_text
from .services.browser_events import BROWSER_INSTRUCTION, BROWSER_LIVE_TEXT


_SYSTEM_PROMPT_TEMPLATE = """你是 __BRAND_ASSISTANT__，用户的个人 AI 助理。回答简洁、直接，使用轻量 Markdown。需要用户在几个方案中选择、补充几项信息、查看可执行步骤清单或计划、并列查看几个推荐对象时，可以使用对话组件。闲聊、单一事实问答、用户只要一段文字时不要用组件。每条回复最多一个组件；先写一两句正文，再给一个语言标记为 luma-ui 的围栏代码块。JSON 必须是单行或合法多行，不能有注释；绝不输出 HTML。组件只用于帮助呈现和收集输入，不要虚构用户已提交的内容。
 luma-ui 不是工具或函数，绝不要用 tool_call / function_calls / invoke 之类的格式，直接在正文里输出 ```luma-ui 代码块。
历史消息里形如「〔历史组件记录：…〕」的文字是系统对之前已展示组件的摘要，只供你了解用户做过的选择；你自己绝不能输出这种格式。每次需要组件时，都必须重新输出一个完整的 luma-ui 代码块，否则用户看不到组件。

choice 示例：
```luma-ui
{"type":"choice","title":"先做哪件？","options":[{"id":"report","label":"写周报","description":"约 30 分钟"},{"id":"visit","label":"客户回访"}],"multiple":false}
```
form 示例（kind 只能是 text/textarea/number/date/select；select 需要 options）：
```luma-ui
{"type":"form","title":"补充信息","fields":[{"id":"city","label":"城市","kind":"text","required":true},{"id":"date","label":"日期","kind":"date"},{"id":"level","label":"预算","kind":"select","options":["经济","舒适","高端"]}],"submit_label":"提交"}
```
checklist 示例：
```luma-ui
{"type":"checklist","title":"执行步骤","items":[{"id":"goal","label":"确认目标","note":"写下一句话目标"},{"id":"plan","label":"拆分任务"}],"allow_create_tasks":true}
```
cards 示例（尽量写上 subtitle/body/tag；希望用户点选一张继续时设 selectable:true）：
```luma-ui
{"type":"cards","title":"推荐","items":[{"id":"park","title":"森林公园","subtitle":"车程 40 分钟","body":"草坪大，适合野餐和放风筝。","tag":"户外"}],"selectable":true}
```"""


def _render_system_prompt() -> str:
    body = _SYSTEM_PROMPT_TEMPLATE.replace("__BRAND_ASSISTANT__", get_brand().assistant_name)
    return body + "\n\n" + BROWSER_INSTRUCTION


SYSTEM_PROMPT = LiveText(_render_system_prompt)

FENCE = re.compile(r"(?m)^```luma-ui[ \t]*\r?\n(?P<body>.*?)(?:^```[ \t]*(?:\r?\n|$)|\Z)", re.DOTALL)
MARKER = re.compile(r"\[\[widget:(wgt_[A-Za-z0-9_-]+)\]\]")
VALID_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
TRAILING_COMMA = re.compile(r",\s*([}\]])")
HISTORY_RECORD_PREFIX = "〔历史组件记录"

logger = logging.getLogger(__name__)


class HistoryRecordFilter:
    """Drop context-only widget records without buffering ordinary replies."""

    def __init__(self) -> None:
        self._pending = ""
        self._depth = 0

    def feed(self, chunk: str) -> str:
        output = []
        for char in chunk:
            if self._depth:
                if char == "〔":
                    self._depth += 1
                elif char == "〕":
                    self._depth -= 1
                continue
            self._pending += char
            while self._pending and not HISTORY_RECORD_PREFIX.startswith(self._pending):
                output.append(self._pending[0])
                self._pending = self._pending[1:]
            if self._pending == HISTORY_RECORD_PREFIX:
                self._pending = ""
                self._depth = 1
        return "".join(output)

    def finish(self) -> str:
        # An unfinished full record is discarded; a partial opener alone is
        # ordinary text and can now be released.
        remaining = "" if self._depth else self._pending
        self._pending = ""
        self._depth = 0
        return remaining


def strip_history_records(content: str) -> str:
    """Remove only internal record fragments, including truncated records."""

    cleaner = HistoryRecordFilter()
    return cleaner.feed(content) + cleaner.finish()


def _text(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _optional_text(source: dict[str, Any], key: str, limit: int) -> dict[str, str]:
    value = _text(source.get(key), limit)
    return {key: value} if value else {}


def _items(raw: Any, prefix: str, maximum: int, label_key: str, label_limit: int) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("items must be a list")
    result: list[dict[str, Any]] = []
    used: set[str] = set()
    for index, entry in enumerate(raw, 1):
        if not isinstance(entry, dict):
            continue
        label = _text(entry.get(label_key), label_limit)
        if not label:
            continue
        item_id = entry.get("id")
        if not isinstance(item_id, str) or not VALID_ID.fullmatch(item_id) or item_id in used:
            item_id = f"{prefix}{len(result) + 1}"
            suffix = len(result) + 1
            while item_id in used:
                suffix += 1
                item_id = f"{prefix}{suffix}"
        used.add(item_id)
        result.append({"id": item_id, label_key: label})
        if len(result) >= maximum:
            break
    return result


def _bool(source: dict[str, Any], key: str, default: bool) -> bool:
    # Models sometimes write "true"/1; anything not a real boolean keeps the default.
    value = source.get(key, default)
    return value if isinstance(value, bool) else default


def _normalize(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("widget must be an object")
    kind = raw.get("type")
    if kind not in {"choice", "form", "checklist", "cards"}:
        raise ValueError("unknown widget type")
    spec: dict[str, Any] = {"type": kind, **_optional_text(raw, "title", 60)}
    if "fallback" in raw and not isinstance(raw["fallback"], str):
        raise ValueError("fallback must be text")
    spec.update(_optional_text(raw, "fallback", 500))
    if kind == "choice":
        options = _items(raw.get("options"), "opt", 8, "label", 40)
        if len(options) < 2:
            raise ValueError("choice needs at least two options")
        for item, original in zip(options, [x for x in raw["options"] if isinstance(x, dict) and _text(x.get("label"), 40)]):
            item.update(_optional_text(original, "description", 80))
        spec.update(options=options, multiple=_bool(raw, "multiple", False))
        if spec["multiple"]:
            spec["submit_label"] = _text(raw.get("submit_label"), 12) or "确定"
    elif kind == "form":
        fields = _items(raw.get("fields"), "f", 8, "label", 30)
        if not fields:
            raise ValueError("form needs a field")
        originals = [x for x in raw["fields"] if isinstance(x, dict) and _text(x.get("label"), 30)][:8]
        for field, original in zip(fields, originals):
            field_kind = original.get("kind")
            if field_kind not in {"text", "textarea", "number", "date", "select"}:
                field_kind = "text"
            field["kind"] = field_kind
            field["required"] = _bool(original, "required", False)
            field.update(_optional_text(original, "placeholder", 40))
            if field_kind == "select":
                options = original.get("options")
                cleaned = list(dict.fromkeys(filter(None, (_text(option, 30) for option in options)))) if isinstance(options, list) else []
                if len(cleaned) >= 2:
                    field["options"] = cleaned[:12]
                else:
                    field["kind"] = "text"
        spec.update(fields=fields, submit_label=_text(raw.get("submit_label"), 12) or "提交")
    elif kind == "checklist":
        items = _items(raw.get("items"), "item", 12, "label", 80)
        if not items:
            raise ValueError("checklist needs an item")
        originals = [x for x in raw["items"] if isinstance(x, dict) and _text(x.get("label"), 80)][:12]
        for item, original in zip(items, originals):
            item.update(_optional_text(original, "note", 80))
            item["done"] = _bool(original, "done", False)
        spec.update(items=items, allow_create_tasks=_bool(raw, "allow_create_tasks", True))
    else:
        items = _items(raw.get("items"), "item", 6, "title", 40)
        if not items:
            raise ValueError("cards needs an item")
        originals = [x for x in raw["items"] if isinstance(x, dict) and _text(x.get("title"), 40)][:6]
        for item, original in zip(items, originals):
            for key, limit in (("subtitle", 60), ("body", 200), ("tag", 12)):
                item.update(_optional_text(original, key, limit))
        spec.update(items=items, selectable=_bool(raw, "selectable", False))
    return spec


def _fallback(spec: dict[str, Any]) -> str:
    if spec.get("fallback"):
        return spec["fallback"]
    kind = spec["type"]
    if kind == "choice":
        value = "请从以下选项中选择：" + " / ".join(item["label"] for item in spec["options"])
    elif kind == "form":
        value = "请补充：" + "、".join(field["label"] for field in spec["fields"])
    elif kind == "checklist":
        value = "待办清单：" + "；".join(item["label"] for item in spec["items"])
    else:
        value = "推荐选项：" + " / ".join(item["title"] for item in spec["items"])
    return value[:500]


def _initial_state(spec: dict[str, Any]) -> dict[str, Any]:
    kind = spec["type"]
    if kind in {"choice", "cards"}:
        return {"status": "open", "selected": []}
    if kind == "form":
        return {"status": "open", "values": {}}
    return {"done": {item["id"]: True for item in spec["items"] if item.get("done")}, "tasks_created": False, "task_ids": []}


def _public(row: Any) -> dict[str, Any]:
    state = json.loads(row["state_json"])
    state = {key: value for key, value in state.items() if not str(key).startswith("_")}
    return {"id": row["id"], "type": row["type"], "spec": json.loads(row["spec_json"]),
            "state": state, "fallback": row["fallback"]}


# Only these openers prove the model faked a tool call; a bare <parameter> may be legitimate prose.
# Keep the whitespace tolerance in sync with the Web/Flutter parsers.
_PSEUDO_STRONG = re.compile(
    r"<\s*(?:tool_call|function_calls|function\s*=\s*luma[^>]*|invoke\s+name\s*=\s*[\"']?\s*luma[^>]*?)\s*>",
    re.I,
)
_PSEUDO_WEAK_OPEN = re.compile(
    r"<\s*(?:parameter\b|invoke\b|function\s*=|tool_call\b|function_calls\b)[^>]*>",
    re.I,
)
_PSEUDO_CLOSE_AT_START = re.compile(
    r"\s*</\s*(?:parameter|invoke|function_calls|tool_call|function)\s*>",
    re.I,
)
_MARKDOWN_FENCE_LINE = re.compile(r"(?m)^[ \t]*```([^\r\n]*)(?:\r?\n|$)")


def _balanced_object_end(content: str, brace: int) -> int:
    depth = 0
    quote = False
    escaped = False
    for index in range(brace, len(content)):
        char = content[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = False
            continue
        if char == '"':
            quote = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return -1


def _inline_code_spans(content: str) -> list[tuple[int, int]]:
    """Return single-backtick inline-code ranges, line by line.

    A backtick adjacent to another backtick belongs to a fence/run and is not
    treated as an inline delimiter, matching the client parsers.
    """
    ranges: list[tuple[int, int]] = []
    line_start = 0
    while line_start <= len(content):
        line_end = content.find("\n", line_start)
        end = len(content) if line_end < 0 else line_end
        opened = -1
        for index in range(line_start, end):
            if content[index] != "`":
                continue
            previous_is_backtick = index > line_start and content[index - 1] == "`"
            next_is_backtick = index + 1 < end and content[index + 1] == "`"
            if previous_is_backtick or next_is_backtick:
                continue
            if opened < 0:
                opened = index
            else:
                ranges.append((opened, index + 1))
                opened = -1
        line_start = len(content) + 1 if line_end < 0 else line_end + 1
    return ranges


def _inside_ranges(index: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= index < end for start, end in ranges)


def _pseudo_close_end(content: str, start: int) -> int:
    cursor = start
    found = False
    while True:
        match = _PSEUDO_CLOSE_AT_START.match(content, cursor)
        if not match:
            return cursor if found else -1
        found = True
        cursor = match.end()


def _pseudo_analysis(content: str, open_end: int) -> tuple[str, int, Any]:
    """Classify what follows a strong opener.

    Only a JSON object immediately after whitespace and weak wrapper tags is a
    pseudo call.  This deliberately leaves explanatory prose untouched.
    """
    cursor = open_end
    while True:
        whitespace = re.match(r"\s*", content[cursor:])
        cursor += len(whitespace.group(0))
        weak = _PSEUDO_WEAK_OPEN.match(content, cursor)
        if not weak:
            break
        cursor = weak.end()
    if cursor >= len(content):
        return "tail", len(content), None
    if content[cursor] != "{":
        return "text", open_end, None
    json_end = _balanced_object_end(content, cursor)
    if json_end < 0:
        return "truncated", len(content), None
    close_end = _pseudo_close_end(content, json_end)
    end = close_end if close_end >= 0 else json_end
    try:
        raw: Any = json.loads(TRAILING_COMMA.sub(r"\1", content[cursor:json_end]))
    except ValueError:
        raw = None
    return "pseudo", end, raw


def _pseudo_spans(content: str, skip: list[tuple[int, int]]) -> list[tuple[int, int, Any]]:
    """Find fake tool-call wrappers around a JSON object, outside real widget fences."""
    spans: list[tuple[int, int, Any]] = []
    inline = _inline_code_spans(content)
    position = 0
    for match in _PSEUDO_STRONG.finditer(content):
        start = match.start()
        if (start < position or _inside_ranges(start, skip) or _inside_ranges(start, inline)):
            continue
        kind, end, raw = _pseudo_analysis(content, match.end())
        if kind != "pseudo":
            continue
        spans.append((start, end, raw))
        position = end
    return spans


def _markdown_fence_spans(content: str) -> list[tuple[int, int]]:
    """Return every Markdown triple-backtick fence, including unclosed ones."""
    ranges: list[tuple[int, int]] = []
    opening: int | None = None
    for match in _MARKDOWN_FENCE_LINE.finditer(content):
        if opening is None:
            opening = match.start()
        elif not match.group(1).strip():
            ranges.append((opening, match.end()))
            opening = None
    if opening is not None:
        ranges.append((opening, len(content)))
    return ranges


def strip_pseudo_markup(content: str) -> str:
    """Remove fake luma tool-call wrappers while preserving Markdown code.

    Strong openers are the only evidence that a wrapper is synthetic.  A
    following JSON object and any immediately following closing tags are
    removed; an incomplete object is hidden through the end of the message.
    Strong markers followed by prose remain untouched.
    """
    if not isinstance(content, str) or not content:
        return content
    fences = _markdown_fence_spans(content)
    inline = _inline_code_spans(content)
    spans: list[tuple[int, int]] = []
    covered_until = 0
    for match in _PSEUDO_STRONG.finditer(content):
        start = match.start()
        if start < covered_until or _inside_ranges(start, fences) or _inside_ranges(start, inline):
            continue
        kind, end, _ = _pseudo_analysis(content, match.end())
        if kind == "text":
            continue
        spans.append((start, end))
        covered_until = end
    if not spans:
        return content
    for start, end in sorted(spans, key=lambda item: item[0], reverse=True):
        content = content[:start] + content[end:]
    return content


def _unwrap(raw: Any) -> Any:
    """Accept {"spec": {...}} style arguments that models use for fake tool calls."""
    for _ in range(2):
        if not isinstance(raw, dict) or "type" in raw:
            return raw
        inner = next((raw[key] for key in ("spec", "arguments", "parameters", "input", "widget") if isinstance(raw.get(key), dict)), None)
        if inner is None and len(raw) == 1 and isinstance(next(iter(raw.values())), dict):
            inner = next(iter(raw.values()))
        if inner is None:
            return raw
        raw = inner
    return raw


def _store(spec: dict[str, Any], *, user_id: str, session_id: str, message_id: str) -> dict[str, Any]:
    widget_id = "wgt_" + uuid.uuid4().hex
    fallback = _fallback(spec)
    state = _initial_state(spec)
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO widgets(id,user_id,session_id,message_id,type,spec_json,state_json,fallback,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (widget_id, user_id, session_id, message_id, spec["type"],
             json.dumps(spec, ensure_ascii=False), json.dumps(state, ensure_ascii=False), fallback, timestamp, timestamp),
        )
    return {"id": widget_id, "type": spec["type"], "spec": spec, "state": state, "fallback": fallback}


def extract_widgets(content: str, *, user_id: str, session_id: str, message_id: str) -> tuple[str, list[dict[str, Any]], int]:
    """Strip every widget fence, storing only the first valid component."""
    widgets: list[dict[str, Any]] = []
    errors = 0
    replacements: list[tuple[int, int, str]] = []
    fences = list(FENCE.finditer(content))
    for match in fences:
        raw: Any = None
        try:
            body = match.group("body")
            try:
                raw = json.loads(body)
            except ValueError:
                raw = json.loads(TRAILING_COMMA.sub(r"\1", body))
            if widgets:
                raise ValueError("one widget per reply")
            spec = _normalize(_unwrap(raw))
        except (ValueError, TypeError) as error:
            errors += 1
            logger.warning("widget rejected: %s", type(error).__name__ + ": " + str(error)[:120])
            fallback = _text(raw.get("fallback"), 500) if isinstance(raw, dict) else ""
            replacements.append((match.start(), match.end(), fallback))
            continue
        widget = _store(spec, user_id=user_id, session_id=session_id, message_id=message_id)
        widgets.append(widget)
        replacements.append((match.start(), match.end(), f"[[widget:{widget['id']}]]"))
    pseudo = _pseudo_spans(content, _markdown_fence_spans(content))
    for start, end, raw in pseudo:
        # A real fence wins; otherwise the first fake call becomes the component.
        if widgets:
            replacements.append((start, end, ""))
            continue
        try:
            spec = _normalize(_unwrap(raw))
        except (ValueError, TypeError):
            errors += 1
            replacements.append((start, end, ""))
            continue
        widget = _store(spec, user_id=user_id, session_id=session_id, message_id=message_id)
        widgets.append(widget)
        replacements.append((start, end, f"[[widget:{widget['id']}]]"))
    for start, end, replacement in sorted(replacements, key=lambda item: item[0], reverse=True):
        content = content[:start] + replacement + content[end:]
    content = strip_pseudo_markup(content)
    return content, widgets, errors


def widgets_for_messages(
    user_id: str,
    message_ids: list[str],
    connection: Any = None,
) -> dict[str, list[dict[str, Any]]]:
    if not message_ids:
        return {}
    result: dict[str, list[dict[str, Any]]] = {}
    # SQLite's parameter limit varies by version; keep batches comfortably below it.
    def read_rows(conn: Any) -> None:
        # Keep batches bounded so a large message history does not create an
        # unwieldy SQL statement.
        for offset in range(0, len(message_ids), 400):
            batch = message_ids[offset:offset + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT * FROM widgets WHERE user_id = ? AND message_id IN ({placeholders}) ORDER BY created_at, id",
                (user_id, *batch),
            ).fetchall()
            for row in rows:
                result.setdefault(row["message_id"], []).append(_public(row))
    if connection is not None:
        read_rows(connection)
    else:
        with get_connection() as conn:
            read_rows(conn)
    return result


def describe_for_model(
    content: str,
    widgets_by_id: dict[str, dict[str, Any]],
    connection: Any = None,
) -> str:
    def describe(match: re.Match[str]) -> str:
        widget = widgets_by_id.get(match.group(1))
        if not widget:
            return ""
        spec, state = widget["spec"], widget["state"]
        kind = widget["type"]
        if kind == "confirm":
            pending = state.get("_pending", {})
            try:
                if connection is not None:
                    private_row = connection.execute("SELECT state_json FROM widgets WHERE id = ?", (widget["id"],)).fetchone()
                else:
                    with get_connection() as conn:
                        private_row = conn.execute("SELECT state_json FROM widgets WHERE id = ?", (widget["id"],)).fetchone()
                if private_row:
                    private_state = json.loads(private_row["state_json"])
                    pending = private_state.get("_pending", pending)
                    state = private_state
            except Exception:
                pass
            connector = str(pending.get("connector_name") or spec.get("connector") or "连接器")
            tool = str(pending.get("tool_title") or spec.get("tool") or "工具")
            status = {"pending": "等待确认", "running": "执行中", "done": "已执行", "failed": "执行失败", "cancelled": "已取消"}.get(str(state.get("status", "pending")), "等待确认")
            # Raw results stay private and enter a resumed run as tool data.
            # Embedding them in assistant history invited verbatim model echoes.
            return f"〔历史组件记录：确认卡 {connector}/{tool} {status}〕"
        title = f"「{spec['title']}」" if spec.get("title") else ""
        if kind in {"choice", "cards"}:
            entries = spec["options"] if kind == "choice" else spec["items"]
            key = "label" if kind == "choice" else "title"
            text = "选项：" + " / ".join(item[key] for item in entries)
            selected = [item[key] for item in entries if item["id"] in state.get("selected", [])]
            if selected:
                text += "；用户已选：" + "、".join(selected)
        elif kind == "form":
            text = "字段：" + " / ".join(field["label"] for field in spec["fields"])
            if state.get("status") == "submitted":
                text += "；用户已提交：" + "、".join(
                    f"{field['label']}={state.get('values', {}).get(field['id'], '')}" for field in spec["fields"]
                    if field["id"] in state.get("values", {}))
        else:
            text = "清单：" + " / ".join(item["label"] for item in spec["items"])
            done = [item["label"] for item in spec["items"] if state.get("done", {}).get(item["id"])]
            if done:
                text += "；已完成：" + "、".join(done)
            if state.get("tasks_created"):
                text += "；已转为任务"
        return f"〔历史组件记录：{kind}{title}{text}〕"
    return MARKER.sub(describe, content)


def create_confirm_widget(user_id: str, session_id: str, message_id: str, *, connector_id: str,
                          connector_name: str, tool: str, title: str, arguments: dict[str, Any]) -> dict[str, Any]:
    details = []
    for key, value in list(arguments.items())[:10]:
        shown = str(value)[:200]
        shown = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]{8,}", "Bearer [已隐藏]", shown)
        shown = re.sub(r"(?i)\b(?:simmcp_|sk-)[A-Za-z0-9._~-]{8,}", "[已隐藏]", shown)
        details.append({"label": str(key)[:80], "value": shown})
    body = f"{get_brand().assistant_name} 想调用「{connector_name}」的「{title}」，这个操作可能会修改数据。"
    spec = {"type": "confirm", "title": "需要你确认", "body": body, "details": details, "confirm_label": "确认执行", "cancel_label": "取消"}
    state = {"status": "pending", "_pending": {"connector_id": connector_id, "tool": tool, "arguments": arguments, "connector_name": connector_name, "tool_title": title}}
    widget_id = "wgt_" + uuid.uuid4().hex
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute("INSERT INTO widgets(id,user_id,session_id,message_id,type,spec_json,state_json,fallback,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (widget_id, user_id, session_id, message_id, "confirm", json.dumps(spec, ensure_ascii=False), json.dumps(state, ensure_ascii=False), f"请确认执行「{title}」", timestamp, timestamp))
    return {"id": widget_id, "type": "confirm", "spec": spec, "state": {"status": "pending"}, "fallback": f"请确认执行「{title}」"}


def _invalid(detail: str) -> None:
    raise HTTPException(status_code=422, detail=detail)


def _confirm_event(user_id: str, row: Any, action: str) -> tuple[dict[str, Any], str]:
    """Run a pending write tool at most once; the remote call happens outside any DB transaction."""
    widget_id = row["id"]
    spec = json.loads(row["spec_json"])
    state = json.loads(row["state_json"])
    if state.get("status") != "pending":
        raise HTTPException(status_code=409, detail="Widget already handled")
    pending = state.get("_pending") if isinstance(state.get("_pending"), dict) else {}
    title = pending.get("tool_title") or spec.get("tool") or "工具"
    state["status"] = "running" if action == "confirm" else "cancelled"
    with get_connection() as conn:
        cursor = conn.execute("UPDATE widgets SET state_json = ?, updated_at = ? WHERE id = ? AND user_id = ? AND state_json = ?", (json.dumps(state, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), widget_id, user_id, row["state_json"]))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=409, detail="Widget changed, please retry")
    widget = _public(row)
    if action == "cancel":
        widget["state"] = {"status": "cancelled"}
        return widget, f"取消执行「{title}」"
    try:
        # Re-enter the same registry and Sentinel used by the agent loop. The
        # widget state is the authority for connector and arguments; no client
        # supplied value is consulted here.
        from .agent.loop import AgentContext
        from .agent.policy import decide
        from .agent.tools import registry_for
        import asyncio
        import inspect

        pending_tool = str(pending.get("tool") or "")
        pending_connector_id = str(pending.get("connector_id") or "")
        tools, _ = registry_for(user_id, mode="interactive")
        target = None
        for item in tools:
            metadata = item.get("metadata", {}) if isinstance(item, dict) else getattr(item, "metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}
            item_name = str(item.get("name", "") if isinstance(item, dict) else getattr(item, "name", ""))
            item_connector_id = str(metadata.get("connector_id") or "")
            item_mcp_name = str(metadata.get("mcp_name") or "")
            if pending_connector_id:
                # MCP confirmations are bound to both connector and remote
                # tool.  A same-named tool on another connector is not valid.
                if item_connector_id == pending_connector_id and item_mcp_name == pending_tool:
                    target = item
                    break
            elif not item_connector_id and item_name == pending_tool:
                # A missing connector id denotes a built-in tool.  Never let
                # it resolve to an MCP tool whose display name happens to fit.
                target = item
                break
        if target is None:
            raise mcp.MCPError("连接器工具不存在")
        if pending_tool in {"browser.click", "browser.submit"} and not pending.get("browser_fingerprint"):
            raise mcp.MCPError("浏览器确认已过期，请重新发起操作")
        ctx = AgentContext(user_id=user_id, session_id=row["session_id"], assistant_message_id=row["message_id"], mode="interactive")
        ctx.confirmed = True  # type: ignore[attr-defined]
        if pending_tool in {"browser.click", "browser.submit"} and pending.get("browser_fingerprint"):
            ctx.browser_expected_fingerprint = str(pending["browser_fingerprint"])  # type: ignore[attr-defined]

        async def execute_confirmed() -> Any:
            try:
                arguments = pending.get("arguments") if isinstance(pending.get("arguments"), dict) else {}
                decision = await decide(ctx, target, arguments)
                if decision.decision != "allow":
                    raise mcp.MCPError("策略拒绝执行")
                if getattr(target, "risk", "") == "code":
                    from .agent.policy import mark_code_approved

                    await mark_code_approved(ctx)
                value = target.executor(ctx, arguments)
                return await value if inspect.isawaitable(value) else value
            finally:
                if pending_tool.startswith("browser."):
                    # The synchronous widget endpoint owns a temporary event
                    # loop. Inspect and execute on that same loop, then close
                    # its CDP transports before asyncio.run tears it down.
                    from .services.browser import close_connections

                    await close_connections()

        result_obj = asyncio.run(execute_confirmed())
        result = getattr(result_obj, "text", result_obj)
        result_data = getattr(result_obj, "data", None)
        if isinstance(result_data, dict) and result_data.get("kind") == "browser_live":
            result = BROWSER_LIVE_TEXT
        state.update(status="done", _result=tool_result_text(result))
    except Exception as exc:
        state.update(status="failed", _result=tool_result_text(mcp.safe_error(exc)))
    with get_connection() as conn:
        conn.execute("UPDATE widgets SET state_json = ?, updated_at = ? WHERE id = ? AND user_id = ?", (json.dumps(state, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), widget_id, user_id))
    widget["state"] = {"status": state["status"]}
    return widget, f"已确认执行「{title}」"


def apply_event(user_id: str, widget_id: str, action: str, value: Any) -> tuple[dict[str, Any], str | None]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM widgets WHERE id = ? AND user_id = ?", (widget_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Widget not found")
    if row["type"] == "confirm":
        if action not in {"confirm", "cancel"}:
            _invalid("Unsupported action")
        return _confirm_event(user_id, row, action)
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM widgets WHERE id = ? AND user_id = ?", (widget_id, user_id)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Widget not found")
        widget = _public(row)
        spec, state, kind = widget["spec"], json.loads(row["state_json"]), row["type"]
        message: str | None = None
        if kind == "choice" and action == "submit":
            if state["status"] == "submitted":
                raise HTTPException(status_code=409, detail="Widget already submitted")
            options = {item["id"]: item["label"] for item in spec["options"]}
            if (not isinstance(value, list) or not value or
                (not spec["multiple"] and len(value) != 1) or
                any(not isinstance(item, str) or item not in options for item in value) or len(set(value)) != len(value)):
                _invalid("Invalid choice")
            state.update(status="submitted", selected=value, submitted_at=datetime.now(timezone.utc).isoformat())
            message = "我选择了：" + "、".join(options[item] for item in value)
        elif kind == "form" and action == "submit":
            if state["status"] == "submitted":
                raise HTTPException(status_code=409, detail="Widget already submitted")
            fields = {field["id"]: field for field in spec["fields"]}
            if not isinstance(value, dict) or any(key not in fields for key in value):
                _invalid("Invalid form fields")
            values: dict[str, str] = {}
            for field_id, field in fields.items():
                raw = value.get(field_id, "")
                if field["kind"] == "number" and isinstance(raw, (int, float)) and not isinstance(raw, bool):
                    text = str(raw)[:500]
                elif isinstance(raw, str):
                    text = raw.strip()[:500]
                else:
                    _invalid("Form values must be strings")
                if field["required"] and not text:
                    _invalid("Required field is empty")
                if text and field["kind"] == "select" and text not in field["options"]:
                    _invalid("Invalid select value")
                if text and field["kind"] == "number":
                    try:
                        number = float(text)
                        if not math.isfinite(number):
                            raise ValueError
                    except ValueError:
                        _invalid("Invalid number")
                if field_id in value:
                    values[field_id] = text
            state.update(status="submitted", values=values, submitted_at=datetime.now(timezone.utc).isoformat())
            title = spec.get("title", "表单")
            message = "已提交「" + title + "」：\n" + "\n".join(
                f"- {field['label']}：{values[field['id']]}" for field in spec["fields"] if field["id"] in values)
        elif kind == "checklist" and action == "toggle":
            ids = {item["id"] for item in spec["items"]}
            if (not isinstance(value, dict) or set(value) != {"item_id", "done"} or
                not isinstance(value["item_id"], str) or value["item_id"] not in ids or
                not isinstance(value["done"], bool)):
                _invalid("Invalid checklist toggle")
            state["done"][value["item_id"]] = value["done"]
        elif kind == "checklist" and action == "create_tasks":
            if not spec["allow_create_tasks"] or value is not None:
                _invalid("Task creation is unavailable")
            if state["tasks_created"]:
                raise HTTPException(status_code=409, detail="Tasks already created")
            timestamp = datetime.now(timezone.utc).isoformat()
            for item in spec["items"]:
                if state["done"].get(item["id"]):
                    continue
                task_id = "task_" + uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO tasks(id,user_id,title,description,status,due_at,created_at,updated_at,metadata_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (task_id, user_id, item["label"], item.get("note", ""), "todo", None, timestamp, timestamp,
                     json.dumps({"source": "widget", "widget_id": widget_id}, ensure_ascii=False)),
                )
                state["task_ids"].append(task_id)
            state["tasks_created"] = True
        elif kind == "cards" and action == "select":
            if not spec["selectable"]:
                _invalid("Cards are not selectable")
            if state["status"] == "submitted":
                raise HTTPException(status_code=409, detail="Widget already submitted")
            cards = {item["id"]: item["title"] for item in spec["items"]}
            if not isinstance(value, str) or value not in cards:
                _invalid("Invalid card")
            state.update(status="submitted", selected=[value], submitted_at=datetime.now(timezone.utc).isoformat())
            message = "我选择了：" + cards[value]
        else:
            _invalid("Invalid widget action")
        # Compare-and-swap on the previous state so concurrent submits cannot both
        # succeed; the raise rolls back any tasks inserted above.
        cursor = conn.execute(
            "UPDATE widgets SET state_json = ?, updated_at = ? WHERE id = ? AND user_id = ? AND state_json = ?",
            (json.dumps(state, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), widget_id, user_id,
             row["state_json"]))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=409, detail="Widget changed, please retry")
        widget["state"] = {key: value for key, value in state.items() if not str(key).startswith("_")}
    return widget, message

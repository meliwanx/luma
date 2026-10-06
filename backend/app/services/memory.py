"""Long-term memory retrieval and asynchronous extraction helpers.

The service keeps matching intentionally small and deterministic.  Production
schemas do not require optional PostgreSQL extensions, so Chinese bigrams and
ASCII words are scored in Python before the selected rows are injected into a
conversation context.
"""

from __future__ import annotations

from contextvars import copy_context

import json
import logging
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence

from .. import provider
from ..model_calls import call_context
from ..db import get_connection

logger = logging.getLogger(__name__)


def provider_complete(messages: Sequence[dict[str, str]], *, temperature: float = 0.0) -> Optional[str]:
    """Patchable provider seam used by background extraction and tests."""

    try:
        return provider.complete(messages, temperature=temperature)
    except TypeError:
        # Tiny test doubles and compatible adapters may only accept messages.
        return provider.complete(messages)

_HAN_RE = re.compile(r"[\u3400-\u9fff]+")
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*")
_SENSITIVE_RE = re.compile(
    r"(?:password|passwd|pwd|secret|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|"
    r"bearer|\btoken\b|\bkey\b|credential|令牌|private[_ -]?key|密钥|秘钥|密码|口令|验证码|校验码|身份证|银行卡|信用卡)"
    r"|(?:sk|rk)-[A-Za-z0-9][A-Za-z0-9._~-]{8,}"
    r"|simmcp_[A-Za-z0-9._~-]{8,}|(?:gh[pousr]|xox[baprs])-?[A-Za-z0-9._-]{12,}"
    r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,}"
    r"|(?<!\d)\d{17}[\dXx](?!\d)|(?<!\d)\d{15,19}(?!\d)"
    r"|(?<!\d)(?:\d[ -]?){15,18}\d(?!\d)",
    re.IGNORECASE,
)
_CODE_RE = re.compile(r"(?:验证码|校验码|verification\s+code)\s*[:：]?\s*\d{4,8}", re.IGNORECASE)

# A process-local guard complements the database/user setting.  It only avoids
# scheduling duplicate work in one worker; every write is still idempotent by
# similarity checks in the transaction.
_IN_FLIGHT: set[tuple[str, str]] = set()
_IN_FLIGHT_LOCK = threading.Lock()

# Memory extraction and conversation summarisation share one bounded executor.
# Keeping the queue in this module means both detached paths obey the same
# process-level cap instead of creating one thread per assistant message.
_MEMORY_EXECUTOR: Optional[Any] = None
_MEMORY_EXECUTOR_LOCK = threading.Lock()
_MEMORY_PENDING = 0
_MEMORY_FUTURES: set[Any] = set()
_MEMORY_SHUTTING_DOWN = False


def _int_env(name: str, default: int, minimum: int = 0, maximum: Optional[int] = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    return min(value, maximum) if maximum is not None else value


def _memory_workers() -> int:
    return _int_env("MEMORY_WORKERS", 2, 1, 32)


def _memory_queue_limit() -> int:
    # ``MEMORY_MAX_PENDING`` is the documented name.  The aliases keep local
    # deployments that used an earlier draft setting bounded as well.
    for name in (
        "MEMORY_MAX_PENDING",
        "MEMORY_QUEUE_MAX",
        "MEMORY_QUEUE_LIMIT",
        "MEMORY_PENDING_LIMIT",
        "MEMORY_MAX_PENDING_TASKS",
    ):
        if name in os.environ:
            return _int_env(name, 50, 0, 10000)
    return 50


def _background_done(future: Any) -> None:
    global _MEMORY_PENDING
    with _MEMORY_EXECUTOR_LOCK:
        _MEMORY_FUTURES.discard(future)
        _MEMORY_PENDING = max(0, _MEMORY_PENDING - 1)


def submit_memory_background(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Submit detached memory work, dropping it when the bounded queue is full.

    The function intentionally returns ``None`` when work is dropped.  Callers
    must not wait for this best-effort work or let a queue refusal affect a
    chat response.
    """

    global _MEMORY_EXECUTOR, _MEMORY_PENDING
    with _MEMORY_EXECUTOR_LOCK:
        if _MEMORY_SHUTTING_DOWN:
            return None
        if _MEMORY_PENDING >= _memory_queue_limit():
            logger.debug("memory background queue full; dropping task")
            return None
        if _MEMORY_EXECUTOR is None:
            _MEMORY_EXECUTOR = ThreadPoolExecutor(max_workers=_memory_workers(), thread_name_prefix="luma-memory")
        _MEMORY_PENDING += 1
        try:
            future = _MEMORY_EXECUTOR.submit(copy_context().run, function, *args, **kwargs)
        except Exception:
            _MEMORY_PENDING = max(0, _MEMORY_PENDING - 1)
            logger.debug("memory background task submission failed", exc_info=True)
            return None
        _MEMORY_FUTURES.add(future)
    future.add_done_callback(_background_done)
    return future


def drain_memory_workers() -> None:
    """Wait for submitted work before a caller resets its database rows."""

    with _MEMORY_EXECUTOR_LOCK:
        futures = tuple(_MEMORY_FUTURES)
    if futures:
        wait(futures)


def shutdown_memory_workers() -> None:
    """Release the executor after running callbacks have finished writing."""

    global _MEMORY_EXECUTOR, _MEMORY_PENDING, _MEMORY_SHUTTING_DOWN
    with _MEMORY_EXECUTOR_LOCK:
        _MEMORY_SHUTTING_DOWN = True
        executor = _MEMORY_EXECUTOR
        _MEMORY_EXECUTOR = None
    try:
        if executor is not None:
            # Queued work is best effort; running callbacks must release all
            # database transactions before the lifespan can finish.
            try:
                executor.shutdown(wait=True, cancel_futures=True)
            except TypeError:  # pragma: no cover - compatible test doubles
                executor.shutdown(wait=True)
    finally:
        with _MEMORY_EXECUTOR_LOCK:
            _MEMORY_PENDING = 0
            _MEMORY_FUTURES.clear()
            _MEMORY_SHUTTING_DOWN = False


def memory_top_k() -> int:
    return _int_env("MEMORY_TOP_K", 12, 1, 100)


def _normalise_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def memory_terms(value: Any) -> set[str]:
    """Return Chinese character bigrams and English/number word terms."""

    text = _normalise_text(value)
    terms: set[str] = set()
    for sequence in _HAN_RE.findall(text):
        if len(sequence) == 1:
            terms.add(sequence)
        else:
            terms.update(sequence[index : index + 2] for index in range(len(sequence) - 1))
    terms.update(match.group(0) for match in _WORD_RE.finditer(text))
    return terms


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        result = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def memory_score(row: dict[str, Any], query: str) -> float:
    """Score a memory against a query without database extensions."""

    query_terms = memory_terms(query)
    if not query_terms:
        overlap = 0.0
    else:
        terms = memory_terms(row.get("content", ""))
        overlap = len(query_terms.intersection(terms)) / float(len(query_terms))
    category = str(row.get("category") or "general").strip().lower()
    category_weight = {
        "fact": 1.12,
        "事实": 1.12,
        "preference": 1.10,
        "偏好": 1.10,
        "inferred": 0.96,
        "推断": 0.96,
    }.get(category, 0.9)
    updated = _parse_timestamp(row.get("updated_at") or row.get("created_at"))
    age_days = 0.0 if updated is None else max(0.0, (datetime.now(timezone.utc) - updated).total_seconds() / 86400.0)
    # Keep recency deliberately light so relevance remains the dominant signal.
    recency = 1.0 / (1.0 + age_days / 365.0)
    try:
        raw_importance = int(row.get("importance") or 3)
    except (TypeError, ValueError):
        raw_importance = 3
    importance = max(1, min(5, raw_importance)) / 5.0
    return overlap * category_weight + recency * 0.05 + importance * 0.02


def _is_pinned(row: dict[str, Any]) -> bool:
    value = row.get("pinned", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _is_explicit_category(value: Any) -> bool:
    """Whether a memory is a user-confirmable fact or preference.

    The API stores the canonical English category names, but older clients and
    imported workspaces may use the Chinese labels shown in the UI.  Treating
    both spellings alike keeps pinned memories durable across upgrades.
    """

    return str(value or "").strip().lower() in {"fact", "preference", "事实", "偏好"}


def get_relevant_memories(conn: Any, user_id: str, query: str, top_k: Optional[int] = None) -> list[dict[str, Any]]:
    """Return pinned fact/preference memories plus the best query matches."""

    rows = conn.execute(
        "SELECT * FROM memories WHERE user_id = ? ORDER BY updated_at DESC",
        (user_id,),
    ).fetchall()
    records = [dict(row) for row in rows]
    # Pinning is an explicit user choice and always wins ordering, regardless
    # of whether an older imported row has a canonical category value.
    pinned = [row for row in records if _is_pinned(row)]
    pinned_ids = {str(row.get("id")) for row in pinned}
    limit = memory_top_k() if top_k is None else max(0, int(top_k))
    remaining = [row for row in records if str(row.get("id")) not in pinned_ids]
    remaining.sort(key=lambda row: (memory_score(row, query), str(row.get("updated_at") or "")), reverse=True)
    # Pinned rows are always included; top_k applies to the non-pinned set.
    return pinned + remaining[:limit]


# A descriptive alias keeps callers readable and supports tests written against
# either name while the context service remains independent of persistence.
retrieve_memories = get_relevant_memories


def contains_sensitive_information(content: str) -> bool:
    """Return true for credentials, identity/payment numbers, or verification codes."""

    text = str(content or "")
    return bool(_SENSITIVE_RE.search(text) or _CODE_RE.search(text))


def _extract_json_array(value: Any) -> list[Any]:
    """Parse a model response that may contain markdown or surrounding prose."""

    if not isinstance(value, str):
        return []
    text = value.strip()
    if not text:
        return []
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text).strip()
    decoder = json.JSONDecoder()
    candidates = [text]
    tolerant = re.sub(r",\s*([}\]])", r"\1", text)
    if tolerant != text:
        candidates.append(tolerant)
    for candidate in candidates:
        for start in [index for index, char in enumerate(candidate) if char == "["]:
            try:
                parsed, _ = decoder.raw_decode(candidate[start:])
            except (TypeError, ValueError):
                continue
            return parsed if isinstance(parsed, list) else []
    return []


parse_memory_response = _extract_json_array


def _item_kind(item: dict[str, Any]) -> Optional[str]:
    kind = str(item.get("kind") or item.get("category") or "").strip().lower()
    kind = {"事实": "fact", "偏好": "preference"}.get(kind, kind)
    return kind if kind in {"fact", "preference"} else None


def _similarity(left: str, right: str) -> float:
    a, b = memory_terms(left), memory_terms(right)
    if not a or not b:
        return 1.0 if _normalise_text(left) == _normalise_text(right) else 0.0
    intersection = len(a.intersection(b))
    jaccard = intersection / float(len(a.union(b)))
    # A model often restates an existing memory with one qualifier (for
    # example, ``我喜欢跑步`` → ``我特别喜欢跑步``).  The overlap coefficient
    # recognises that as a duplicate while still requiring most of the shorter
    # statement to match.
    containment = intersection / float(min(len(a), len(b)))
    return max(jaccard, containment)


def _user_auto_extract_enabled(conn: Any, user_id: str) -> bool:
    raw = os.getenv("MEMORY_AUTO_EXTRACT", "true").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    try:
        row = conn.execute("SELECT memory_auto_extract FROM users WHERE user_id = ?", (user_id,)).fetchone()
    except Exception:
        # A partially upgraded worker should fail open to the environment
        # default; the migration adds this column before the feature is used.
        return True
    if row is None:
        return True
    value = row.get("memory_auto_extract") if isinstance(row, dict) else row[0]
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _memory_summary(rows: Sequence[dict[str, Any]], limit: int = 30) -> str:
    values = []
    for row in rows[:limit]:
        content = str(row.get("content") or "")
        if contains_sensitive_information(content):
            continue
        values.append("- %s [%s]" % (content[:500], str(row.get("category") or "general")))
    return "\n".join(values)


def _messages_for_extraction(conn: Any, user_id: str, session_id: str, assistant_message_id: str) -> tuple[Optional[str], Optional[str]]:
    assistant = conn.execute(
        "SELECT id,content,created_at FROM messages WHERE id = ? AND user_id = ? AND session_id = ? AND role = 'assistant'",
        (assistant_message_id, user_id, session_id),
    ).fetchone()
    if assistant is None:
        return None, None
    assistant_data = dict(assistant)
    user = conn.execute(
        "SELECT content FROM messages WHERE user_id = ? AND session_id = ? AND role = 'user' AND created_at <= ? ORDER BY created_at DESC LIMIT 1",
        (user_id, session_id, assistant_data.get("created_at")),
    ).fetchone()
    return (str(user["content"]) if user is not None else "", str(assistant_data.get("content") or ""))


def extract_and_store_memories(user_id: str, session_id: str, assistant_message_id: str) -> int:
    """Extract up to three inferred memories and persist accepted rows.

    The function is synchronous so it can also be exercised directly by a
    worker/test; ``schedule_memory_extraction`` runs it in a background task.
    It never raises provider/parse errors to the chat request.
    """

    if os.getenv("LUMA_PROVIDER", "").strip().lower() == "local":
        return 0
    with get_connection() as conn:
        if not _user_auto_extract_enabled(conn, user_id):
            return 0
        user_text, assistant_text = _messages_for_extraction(conn, user_id, session_id, assistant_message_id)
        if user_text is None or assistant_text is None:
            return 0
        rows = [dict(row) for row in conn.execute("SELECT id,content,category,importance,metadata_json FROM memories WHERE user_id = ?", (user_id,)).fetchall()]
        prompt = (
            "从下面这一轮对话中提取用户可能希望长期记住的信息。只输出 JSON 数组，不要 Markdown。"
            "每项必须是 {content, category, kind}，kind 只能是 fact 或 preference；最多 3 项，没有就输出 []。"
            "不要提取密码、密钥、token、验证码、身份证号、银行卡号等敏感信息。\n"
            "已有记忆：\n%s\n用户：\n%s\n助手：\n%s"
            % (_memory_summary(rows), user_text[:10000], assistant_text[:10000])
        )
    # Do not hold a PostgreSQL connection while waiting for the provider.  A
    # provider timeout can last tens of seconds, and the service deliberately
    # runs with a small pool shared by both API workers.
    try:
        with call_context(user_id=user_id, session_id=session_id, message_id=assistant_message_id, purpose="memory_extract"):
            response = provider_complete([{"role": "system", "content": "你是严格的长期记忆提取器。"}, {"role": "user", "content": prompt}])
    except Exception as exc:
        logger.warning("memory extraction failed: %s", type(exc).__name__)
        return 0
    items = _extract_json_array(response)
    candidates: list[tuple[str, str]] = []
    for item in items[:3]:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        kind = _item_kind(item)
        if not content or not kind or len(content) > 20_000 or contains_sensitive_information(content):
            continue
        candidates.append((content, kind))
    if not candidates:
        return 0

    # Re-read current rows after the provider call so a concurrent request (or
    # a user edit) wins over the stale snapshot used to construct the prompt.
    with get_connection() as conn:
        if not _user_auto_extract_enabled(conn, user_id):
            return 0
        latest_rows = [dict(row) for row in conn.execute(
            "SELECT content FROM memories WHERE user_id = ?", (user_id,)
        ).fetchall()]
        existing = [str(row.get("content") or "") for row in latest_rows]
        accepted = 0
        for content, kind in candidates:
            if any(_similarity(content, old) >= 0.85 for old in existing):
                continue
            memory_id = "mem_" + uuid.uuid4().hex
            timestamp = datetime.now(timezone.utc).isoformat()
            metadata = {"kind": kind, "source_message_id": assistant_message_id}
            conn.execute(
                "INSERT INTO memories(id,user_id,content,category,importance,created_at,updated_at,metadata_json,pinned) VALUES (?,?,?,?,?,?,?,?,?)",
                (memory_id, user_id, content, "inferred", 3, timestamp, timestamp, json.dumps(metadata, ensure_ascii=False), False),
            )
            existing.append(content)
            accepted += 1
        return accepted


# Compatibility aliases useful to job runners and tests.
extract_memories = extract_and_store_memories
extract_memory_candidates = _extract_json_array


def _background_extraction(user_id: str, session_id: str, assistant_message_id: str) -> None:
    key = (session_id, assistant_message_id)
    with _IN_FLIGHT_LOCK:
        if key in _IN_FLIGHT:
            return
        _IN_FLIGHT.add(key)
    try:
        extract_and_store_memories(user_id, session_id, assistant_message_id)
    except Exception as exc:
        logger.warning("memory extraction failed: %s", type(exc).__name__)
    finally:
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.discard(key)


def schedule_memory_extraction(user_id: str, session_id: str, assistant_message_id: str) -> Any:
    """Schedule extraction without delaying the assistant response."""

    if os.getenv("LUMA_PROVIDER", "").strip().lower() == "local":
        return None
    return submit_memory_background(_background_extraction, user_id, session_id, assistant_message_id)


__all__ = [
    "contains_sensitive_information", "extract_and_store_memories", "extract_memories",
    "extract_memory_candidates", "provider_complete",
    "get_relevant_memories", "memory_score", "memory_terms", "parse_memory_response",
    "retrieve_memories", "schedule_memory_extraction", "submit_memory_background",
    "drain_memory_workers", "shutdown_memory_workers",
]

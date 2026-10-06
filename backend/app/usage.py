"""Content-free usage aggregates, calculated in PostgreSQL rather than memory."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional


USER_RANGES = {"7d": 7, "30d": 30, "90d": 90}
ADMIN_RANGES = {"24h": 1, "7d": 7, "30d": 30}
_INTEGER_FIELDS = {"calls", "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "reasoning_tokens"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aggregate_row(row: Any) -> dict[str, Any]:
    # PostgreSQL SUM(BIGINT) returns Decimal; Pydantic's JSON serializer can
    # encode it as a string, so token counts must cross the API as integers.
    return {key: int(value or 0) if key in _INTEGER_FIELDS else value for key, value in dict(row).items()}


def _rows(conn: Any, sql: str, params: Any = ()) -> list[dict[str, Any]]:
    return [_aggregate_row(row) for row in conn.execute(sql, params).fetchall()]


def _row(conn: Any, sql: str, params: Any = ()) -> dict[str, Any]:
    return _aggregate_row(conn.execute(sql, params).fetchone())


def _number(value: Any) -> Optional[float]:
    return round(float(value), 3) if value is not None else None


def _window(range_value: str, ranges: dict[str, int]) -> tuple[datetime, datetime]:
    end = _now()
    return end - timedelta(days=ranges[range_value]), end


def _fill_series(
    rows: list[dict[str, Any]], start: datetime, end: datetime, hourly: bool = False
) -> list[dict[str, Any]]:
    """Keep empty UTC buckets visible, including the two partial end buckets."""

    key = "bucket" if hourly else "date"
    step = timedelta(hours=1) if hourly else timedelta(days=1)
    current = start.replace(minute=0, second=0, microsecond=0) if hourly else start.replace(hour=0, minute=0, second=0, microsecond=0)
    by_bucket = {row[key]: row for row in rows}
    result = []
    while current <= end:
        bucket = current.isoformat(timespec="hours") if hourly else current.date().isoformat()
        result.append(by_bucket.get(bucket, {
            key: bucket, "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        }))
        current += step
    return result


def user_usage(conn: Any, user_id: str, range_value: str) -> dict[str, Any]:
    start, end = _window(range_value, USER_RANGES)
    where = " WHERE mc.user_id = ? AND mc.created_at >= ? AND mc.created_at <= ?"
    params = (user_id, start, end)
    totals = _row(
        conn,
        "SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
        "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, COALESCE(SUM(total_tokens), 0) AS total_tokens, "
        "COALESCE(AVG(CASE WHEN estimated THEN 1.0 ELSE 0.0 END), 0) AS estimated_ratio "
        "FROM model_calls mc" + where,
        params,
    )
    totals["estimated_ratio"] = float(totals["estimated_ratio"])
    daily = _rows(
        conn,
        "SELECT (created_at AT TIME ZONE ?)::date::text AS date, COUNT(*) AS calls, "
        "SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens, "
        "SUM(total_tokens) AS total_tokens FROM model_calls mc" + where + " GROUP BY date ORDER BY date",
        ("UTC",) + params,
    )
    purposes = _rows(
        conn,
        "SELECT purpose, COUNT(*) AS calls, SUM(total_tokens) AS total_tokens FROM model_calls mc" + where
        + " GROUP BY purpose ORDER BY total_tokens DESC, purpose",
        params,
    )
    models = _rows(
        conn,
        "SELECT model, COUNT(*) AS calls, SUM(total_tokens) AS total_tokens FROM model_calls mc" + where
        + " GROUP BY model ORDER BY total_tokens DESC, model",
        params,
    )
    performance = _row(
        conn,
        "SELECT AVG(first_token_ms) AS first_token_ms_avg, "
        "percentile_cont(?) WITHIN GROUP (ORDER BY duration_ms) AS duration_ms_p50, "
        "percentile_cont(?) WITHIN GROUP (ORDER BY duration_ms) AS duration_ms_p95 "
        "FROM model_calls mc" + where,
        (0.5, 0.95) + params,
    )
    performance = {key: _number(value) for key, value in performance.items()}
    sessions = _rows(
        conn,
        "SELECT mc.session_id, s.title, COUNT(*) AS calls, SUM(mc.total_tokens) AS total_tokens "
        "FROM model_calls mc LEFT JOIN sessions s ON s.id = mc.session_id AND s.user_id = mc.user_id"
        + where + " AND mc.session_id IS NOT NULL GROUP BY mc.session_id, s.title "
        "ORDER BY total_tokens DESC, mc.session_id LIMIT ?",
        params + (10,),
    )
    for session in sessions:
        session["title"] = session["title"] or "已删除的会话"
    return {
        "range": range_value,
        "totals": totals,
        "daily": _fill_series(daily, start, end),
        "purposes": purposes,
        "models": models,
        "performance": performance,
        "top_sessions": sessions,
    }


def _distribution(row: dict[str, Any], prefix: str, quantiles: tuple[str, ...]) -> dict[str, Any]:
    return {key: _number(row[prefix + "_" + key]) for key in ("avg",) + quantiles}


def _user_label(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.pop(key, None) for key in ("user_id", "username", "display_name")}


def admin_models(
    conn: Any, range_value: str, model: Optional[str] = None, user_id: Optional[str] = None
) -> dict[str, Any]:
    start, end = _window(range_value, ADMIN_RANGES)
    where = " WHERE mc.created_at >= ? AND mc.created_at <= ?"
    params: tuple[Any, ...] = (start, end)
    if user_id:
        where += " AND mc.user_id = ?"
        params += (user_id,)
    available_models = [row["model"] for row in _rows(
        conn, "SELECT DISTINCT model FROM model_calls mc" + where + " ORDER BY model", params
    )]
    if model:
        where += " AND mc.model = ?"
        params += (model,)

    model_rows = _rows(
        conn,
        "SELECT model, COUNT(*) AS calls, AVG(CASE WHEN status = ? THEN 1.0 ELSE 0.0 END) AS success_rate, "
        "AVG(first_token_ms) AS first_avg, "
        "percentile_cont(?) WITHIN GROUP (ORDER BY first_token_ms) AS first_p50, "
        "percentile_cont(?) WITHIN GROUP (ORDER BY first_token_ms) AS first_p95, "
        "percentile_cont(?) WITHIN GROUP (ORDER BY first_token_ms) AS first_p99, "
        "AVG(duration_ms) AS duration_avg, "
        "percentile_cont(?) WITHIN GROUP (ORDER BY duration_ms) AS duration_p50, "
        "percentile_cont(?) WITHIN GROUP (ORDER BY duration_ms) AS duration_p95, "
        "percentile_cont(?) WITHIN GROUP (ORDER BY duration_ms) AS duration_p99, "
        "AVG(tokens_per_sec) AS speed_avg, "
        "percentile_cont(?) WITHIN GROUP (ORDER BY tokens_per_sec) AS speed_p50, "
        "SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens, "
        "SUM(total_tokens) AS total_tokens, AVG(total_tokens) AS tokens_per_call "
        "FROM model_calls mc" + where + " GROUP BY model ORDER BY calls DESC, model",
        ("ok", 0.5, 0.95, 0.99, 0.5, 0.95, 0.99, 0.5) + params,
    )
    error_rows = _rows(
        conn,
        "SELECT model, COALESCE(error_type, status) AS error_type, COUNT(*) AS calls FROM model_calls mc"
        + where + " AND status <> ? GROUP BY model, COALESCE(error_type, status)",
        params + ("ok",),
    )
    errors: dict[str, dict[str, int]] = {}
    for row in error_rows:
        errors.setdefault(row["model"], {})[row["error_type"]] = row["calls"]
    models = []
    for row in model_rows:
        models.append({
            **{key: row[key] for key in ("model", "calls", "prompt_tokens", "completion_tokens", "total_tokens")},
            "success_rate": float(row["success_rate"]),
            "error_types": errors.get(row["model"], {}),
            "first_token_ms": _distribution(row, "first", ("p50", "p95", "p99")),
            "duration_ms": _distribution(row, "duration", ("p50", "p95", "p99")),
            "tokens_per_sec": _distribution(row, "speed", ("p50",)),
            "tokens_per_call": _number(row["tokens_per_call"]),
        })

    hourly = range_value == "24h"
    series = _rows(
        conn,
        "SELECT date_trunc(?, created_at AT TIME ZONE ?) AS bucket, COUNT(*) AS calls, "
        "percentile_cont(?) WITHIN GROUP (ORDER BY first_token_ms) AS first_token_ms_p95, "
        "AVG(CASE WHEN status = ? THEN 0.0 ELSE 1.0 END) AS error_rate, "
        "SUM(total_tokens) AS total_tokens FROM model_calls mc" + where + " GROUP BY bucket ORDER BY bucket",
        ("hour" if hourly else "day", "UTC", 0.95, "ok") + params,
    )
    by_bucket = {}
    for row in series:
        date = row["bucket"].replace(tzinfo=timezone.utc)
        row["bucket"] = date.isoformat(timespec="hours") if hourly else date.date().isoformat()
        row["first_token_ms_p95"] = _number(row["first_token_ms_p95"])
        row["error_rate"] = float(row["error_rate"])
        by_bucket[row["bucket"]] = row
    empty = _fill_series([], start, end, hourly=hourly)
    series = [by_bucket.get(row.get("bucket", row.get("date")), {
        "bucket": row.get("bucket", row.get("date")), "calls": 0,
        "first_token_ms_p95": None, "error_rate": 0, "total_tokens": 0,
    }) for row in empty]

    users = _rows(
        conn,
        "SELECT mc.user_id, u.username, u.display_name, COUNT(*) AS calls, "
        "SUM(mc.total_tokens) AS total_tokens, AVG(mc.first_token_ms) AS first_token_ms_avg "
        "FROM model_calls mc LEFT JOIN users u ON u.user_id = mc.user_id" + where
        + " GROUP BY mc.user_id, u.username, u.display_name "
        "ORDER BY total_tokens DESC, mc.user_id",
        params,
    )
    for row in users:
        label = _user_label(row)
        row["user_id"], row["user"] = label["user_id"], label
        row["first_token_ms_avg"] = _number(row["first_token_ms_avg"])

    cutoff = _row(
        conn,
        "SELECT percentile_cont(?) WITHIN GROUP (ORDER BY duration_ms) AS p95 FROM model_calls mc" + where,
        (0.95,) + params,
    )["p95"]
    slow_calls = []
    if cutoff is not None:
        slow_calls = _rows(
            conn,
            "SELECT mc.id, mc.user_id, u.username, u.display_name, mc.session_id, "
            "mc.purpose, mc.model, mc.first_token_ms, mc.duration_ms, mc.tokens_per_sec, "
            "mc.prompt_tokens, mc.completion_tokens, mc.total_tokens, mc.status, mc.created_at "
            "FROM model_calls mc LEFT JOIN users u ON u.user_id = mc.user_id" + where
            + " AND mc.duration_ms > ? ORDER BY mc.created_at DESC, mc.id DESC LIMIT ?",
            params + (cutoff, 50),
        )
        for row in slow_calls:
            label = _user_label(row)
            row["user_id"], row["user"] = label["user_id"], label
            for field in ("first_token_ms", "duration_ms", "tokens_per_sec"):
                row[field] = _number(row[field])
            row["created_at"] = row["created_at"].astimezone(timezone.utc).isoformat()
    return {
        "range": range_value, "models": models, "series": series, "users": users,
        "slow_calls": slow_calls, "available_models": available_models,
    }

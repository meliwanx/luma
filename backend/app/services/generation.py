"""Background generation tasks and resumable event streams.

Provider work is intentionally owned by this process rather than by the HTTP
request that started it. Redis Streams are the cross-worker transport; a small
in-process history and queue provide a useful fallback when Redis is down.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import OrderedDict
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

from starlette.concurrency import run_in_threadpool

from ..db import get_connection
from ..model_calls import merged_metadata_sql
from ..widgets import HistoryRecordFilter, strip_history_records
from .browser_events import without_browser_credentials


Event = Tuple[str, Dict[str, Any]]
Runner = Callable[[], AsyncIterator[Event]]
StoredEvent = Tuple[str, str, Dict[str, Any]]


class GenerationManager:
    """Own one background task per assistant message."""

    def __init__(self) -> None:
        self.tasks: Dict[str, asyncio.Task[Any]] = {}
        self.histories: Dict[str, List[StoredEvent]] = {}
        self.queues: Dict[str, List[asyncio.Queue[StoredEvent]]] = {}
        self.cancel_events: Dict[str, asyncio.Event] = {}
        self.counters: Dict[str, int] = {}
        self.epochs: Dict[str, str] = {}
        # Redis cancellation is deliberately cached.  A generation can emit
        # hundreds of deltas, so checking Redis for every delta needlessly
        # consumes the thread pool and the Redis connection.
        self._cancel_cache: Dict[str, Tuple[float, bool]] = {}
        self._history_order: "OrderedDict[str, float]" = OrderedDict()
        self._cleanup_handles: Dict[str, asyncio.Handle] = {}
        self._timeout_handles: Dict[str, asyncio.Handle] = {}
        self._timed_out: Dict[str, bool] = {}
        self.redis_disabled_until = 0.0
        # Python 3.9 requires an active event loop when constructing a lock;
        # defer creation so lightweight manager instances can be built by
        # synchronous callers and tests as well.
        self._lock: Optional[asyncio.Lock] = None

    def _state_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @staticmethod
    def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
        try:
            value = float(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            value = default
        return max(minimum, value)

    @staticmethod
    def _key(message_id: str) -> str:
        return "luma:gen:" + message_id

    @staticmethod
    def _cancel_key(message_id: str) -> str:
        return "luma:gen:cancel:" + message_id

    @staticmethod
    def _alive_key(message_id: str) -> str:
        return "luma:gen:alive:" + message_id

    def _cancel_cleanup(self, message_id: str) -> None:
        handle = self._cleanup_handles.pop(message_id, None)
        if handle is not None:
            handle.cancel()

    def _cancel_timeout(self, message_id: str) -> None:
        handle = self._timeout_handles.pop(message_id, None)
        if handle is not None:
            handle.cancel()

    async def _cleanup_message(self, message_id: str) -> None:
        """Drop local state after the short same-worker resume window."""
        async with self._state_lock():
            self.histories.pop(message_id, None)
            self.counters.pop(message_id, None)
            self.epochs.pop(message_id, None)
            self.cancel_events.pop(message_id, None)
            self._cancel_cache.pop(message_id, None)
            self._history_order.pop(message_id, None)
            self._timed_out.pop(message_id, None)
            self.queues.pop(message_id, None)
            self._cancel_timeout(message_id)
            self._cleanup_handles.pop(message_id, None)

    def _schedule_cleanup(self, message_id: str) -> None:
        """Keep completed events briefly for same-worker resume."""
        self._cancel_cleanup(message_id)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        delay = self._env_float("GENERATION_HISTORY_RETENTION_SECONDS", 60.0, 0.0)
        self._cleanup_handles[message_id] = loop.call_later(
            delay, lambda: asyncio.create_task(self._cleanup_message(message_id))
        )

    async def _evict_old_histories(self) -> None:
        """Bound local history to 200 message ids in a long-lived worker."""
        async with self._state_lock():
            while len(self.histories) > 200 and self._history_order:
                old_id, _ = self._history_order.popitem(last=False)
                self.histories.pop(old_id, None)
                self.counters.pop(old_id, None)
                self.epochs.pop(old_id, None)
                self.cancel_events.pop(old_id, None)
                self._cancel_cache.pop(old_id, None)
                self._timed_out.pop(old_id, None)
                if old_id not in self.tasks:
                    self.queues.pop(old_id, None)
                handle = self._cleanup_handles.pop(old_id, None)
                if handle is not None:
                    handle.cancel()

    async def is_local(self, message_id: str) -> bool:
        """Whether this worker can serve a generation from its local state."""
        epoch = self.epochs.get(message_id)
        if epoch:
            # A confirmation continuation keeps the message id but owns a
            # new stream. Another worker's old done event must not win.
            if await self._message_epoch(message_id) != epoch:
                self._cancel_cleanup(message_id)
                await self._cleanup_message(message_id)
                return False
        async with self._state_lock():
            return message_id in self.tasks or message_id in self.histories

    async def _message_epoch(self, message_id: str) -> Optional[str]:
        def read() -> Optional[str]:
            with get_connection() as conn:
                row = conn.execute("SELECT metadata_json FROM messages WHERE id = ?", (message_id,)).fetchone()
            if row is None:
                return None
            return json.loads(row["metadata_json"] or "{}").get("generation_epoch")

        return await run_in_threadpool(read)

    async def _redis(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Call the synchronous Redis adapter off the event loop."""
        from .. import db

        if not os.getenv("REDIS_HOST", "").strip():
            raise RuntimeError("Redis is not configured")

        def call() -> Any:
            client = db._redis_client()
            return getattr(client, method)(*args, **kwargs)

        return await run_in_threadpool(call)

    async def _redis_quick(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if time.monotonic() < self.redis_disabled_until:
            raise RuntimeError("Redis is unavailable")
        try:
            return await asyncio.wait_for(self._redis(method, *args, **kwargs), timeout=0.5)
        except Exception:
            self.redis_disabled_until = time.monotonic() + 5.0
            raise

    async def _heartbeat(self, message_id: str) -> None:
        """Keep a short-lived marker while a generation owns provider work."""
        key = self._alive_key(message_id)
        try:
            while True:
                try:
                    await self._redis_quick("setex", key, 30, "1")
                except Exception:
                    # Redis is optional for same-worker streams.  Recovery
                    # falls back to the conservative ten-minute cutoff when
                    # the marker cannot be refreshed.
                    pass
                await asyncio.sleep(10.0)
        except asyncio.CancelledError:
            raise
        finally:
            try:
                await self._redis_quick("delete", key)
            except Exception:
                pass

    async def _publish_cancel_marker(self, message_id: str) -> None:
        try:
            await self._redis_quick("setex", self._cancel_key(message_id), 3600, "1")
        except Exception:
            pass

    async def publish(self, message_id: str, event: str, data: Dict[str, Any]) -> str:
        """Publish one event and return its stream id."""
        # SSE hydrates this marker after checking message ownership. Neither
        # Redis Streams nor the local replay history holds browser credentials.
        data = without_browser_credentials(data)
        if message_id in self.epochs:
            data = {**data, "generation_epoch": self.epochs[message_id]}
        payload = json.dumps(data, ensure_ascii=False)
        stream_id: Optional[str] = None
        try:
            result = await self._redis_quick(
                "xadd",
                self._key(message_id),
                {"event": event, "data": payload},
                maxlen=5000,
                approximate=True,
            )
            stream_id = result.decode() if isinstance(result, bytes) else str(result)
            if event in {"done", "error"}:
                # Expiry is best effort; losing it must not replace a real
                # Redis id with a local id and break cross-worker resume.
                try:
                    await self._redis_quick("expire", self._key(message_id), 3600)
                except Exception:
                    pass
        except Exception:
            # Redis is optional. Monotonic local ids support same-worker resume.
            async with self._state_lock():
                current = max(self.counters.get(message_id, 0), len(self.histories.get(message_id, []))) + 1
                self.counters[message_id] = current
                stream_id = str(current)

        # Keep a bounded local mirror even when Redis is available. It allows
        # immediate same-worker subscribers and fallback after Redis failure.
        async with self._state_lock():
            history = self.histories.setdefault(message_id, [])
            self._history_order.pop(message_id, None)
            self._history_order[message_id] = time.monotonic()
            item: StoredEvent = (stream_id or "", event, data)
            history.append(item)
            if len(history) > 5000:
                del history[:-5000]
            for queue in list(self.queues.get(message_id, [])):
                queue.put_nowait(item)
        await self._evict_old_histories()
        if event == "done":
            # The terminal event is retained for a bounded period so a
            # disconnected same-worker client can resume without Redis.
            self._schedule_cleanup(message_id)
        return stream_id or ""

    async def start(
        self,
        message_id: str,
        runner: Runner,
        timeout_seconds: Optional[float] = None,
        restart: bool = False,
        epoch: Optional[str] = None,
    ) -> asyncio.Task[Any]:
        """Schedule ``runner`` independently of the HTTP request.

        Consecutive ``delta`` events are coalesced here, after the provider
        runner has yielded them.  This keeps Redis XADD and local subscriber
        fan-out bounded without changing the SSE contract exposed to clients.
        """

        async def run() -> None:
            terminal = False
            heartbeat: Optional[asyncio.Task[Any]] = None
            iterator: Any = None

            def mark_incomplete(reason: Optional[str] = None) -> None:
                with get_connection() as conn:
                    if reason:
                        row = conn.execute(
                            "SELECT metadata_json FROM messages WHERE id = ? AND status = ?",
                            (message_id, "streaming"),
                        ).fetchone()
                        metadata: Dict[str, Any] = {}
                        if row is not None:
                            try:
                                decoded = json.loads(row.get("metadata_json") or "{}")
                                if isinstance(decoded, dict):
                                    metadata = decoded
                            except (TypeError, ValueError):
                                pass
                        metadata["incomplete"] = True
                        metadata["error"] = reason
                        conn.execute(
                            "UPDATE messages SET metadata_json = " + merged_metadata_sql() + ", status = ? "
                            "FROM (SELECT ?::jsonb AS metadata) incoming WHERE id = ? AND status = ?",
                            ("incomplete", json.dumps(metadata, ensure_ascii=False), message_id, "streaming"),
                        )
                    else:
                        conn.execute(
                            "UPDATE messages SET status = ? WHERE id = ? AND status = ?",
                            ("incomplete", message_id, "streaming"),
                        )

            async def publish_aggregated() -> None:
                nonlocal pending_delta, pending_bytes, flush_task
                if pending_delta is None:
                    return
                if flush_task is not None and not flush_task.done():
                    flush_task.cancel()
                flush_task = None
                content = pending_delta
                pending_delta = None
                pending_bytes = 0
                await self.publish(message_id, "delta", {"content": content})

            pending_delta: Optional[str] = None
            pending_bytes = 0
            flush_task: Optional[asyncio.Task[Any]] = None
            next_task: Optional[asyncio.Task[Any]] = None
            reply_filter = HistoryRecordFilter()
            try:
                heartbeat = asyncio.create_task(self._heartbeat(message_id), name="luma-generation-heartbeat-" + message_id)
                iterator = runner().__aiter__()
                next_task = asyncio.create_task(iterator.__anext__())
                while True:
                    waiters = {next_task}
                    if pending_delta is not None and flush_task is None:
                        flush_task = asyncio.create_task(asyncio.sleep(0.08))
                    if flush_task is not None:
                        waiters.add(flush_task)
                    done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                    if flush_task is not None and flush_task in done:
                        # Retrieve the result so an unexpected timer failure
                        # cannot become an unobserved task exception.
                        flush_task.result()
                        await publish_aggregated()
                        continue
                    if next_task not in done:
                        continue
                    try:
                        event, data = next_task.result()
                    except StopAsyncIteration:
                        break
                    next_task = asyncio.create_task(iterator.__anext__())
                    if event == "delta" and isinstance(data, dict):
                        content = data.get("content", "")
                        if content:
                            text = reply_filter.feed(str(content))
                            if not text:
                                continue
                            pending_delta = (pending_delta or "") + text
                            pending_bytes += len(text.encode("utf-8"))
                            if pending_bytes >= 2048:
                                await publish_aggregated()
                        else:
                            await publish_aggregated()
                            await self.publish(message_id, event, data)
                        continue
                    # Status/tool/widget/error/done events retain their own
                    # boundaries; flush preceding text before publishing one.
                    if event in {"done", "error"}:
                        tail = reply_filter.finish()
                        if tail:
                            pending_delta = (pending_delta or "") + tail
                        if isinstance(data, dict) and isinstance(data.get("content"), str):
                            data = {**data, "content": strip_history_records(data["content"])}
                    await publish_aggregated()
                    await self.publish(message_id, event, data)
                    terminal = terminal or event == "done"
                await publish_aggregated()
                # A cooperative runner may simply return after observing the
                # cancel flag. Ensure subscribers still receive a terminal
                # status and the durable row is marked incomplete.
                if not terminal and await self.is_cancelled(message_id):
                    try:
                        await run_in_threadpool(mark_incomplete, "timeout" if self._timed_out.get(message_id) else None)
                    except Exception:
                        pass
                    reason = "timeout" if self._timed_out.get(message_id) else "cancelled"
                    await self.publish(message_id, "status", {"status": "incomplete", "reason": reason})
                    done_data: Dict[str, Any] = {"id": message_id, "status": "incomplete"}
                    if reason == "timeout":
                        done_data["metadata"] = {"incomplete": True, "error": "timeout"}
                    await self.publish(message_id, "done", done_data)
                elif not terminal:
                    # Keep a malformed/short runner from leaving subscribers
                    # parked forever. Normal chat runners always emit done.
                    await self.publish(message_id, "done", {"id": message_id, "status": "complete"})
            except asyncio.CancelledError:
                # Shutdown or a deadline can interrupt a provider await before
                # the runner persists its partial result; preserve the latest
                # checkpoint and expose the reason to the client.
                try:
                    await asyncio.shield(
                        run_in_threadpool(mark_incomplete, "timeout" if self._timed_out.get(message_id) else None)
                    )
                except Exception:
                    pass
                reason = "timeout" if self._timed_out.get(message_id) else "shutdown"
                try:
                    await self.publish(message_id, "status", {"status": "incomplete", "reason": reason})
                    done_data: Dict[str, Any] = {"id": message_id, "status": "incomplete"}
                    if reason == "timeout":
                        done_data["metadata"] = {"incomplete": True, "error": "timeout"}
                    await self.publish(message_id, "done", done_data)
                except Exception:
                    pass
                raise
            except Exception as exc:
                # Keep an unexpected orchestration failure from leaving a
                # visible streaming row and a subscriber waiting forever.
                error_type = type(exc).__name__
                friendly = "模型暂时不可用，请稍后重试"

                def mark_error() -> Dict[str, Any]:
                    with get_connection() as conn:
                        row = conn.execute("SELECT metadata_json FROM messages WHERE id = ?", (message_id,)).fetchone()
                        metadata: Dict[str, Any] = {}
                        if row is not None:
                            try:
                                decoded = json.loads(row.get("metadata_json") or "{}")
                                if isinstance(decoded, dict):
                                    metadata = decoded
                            except (TypeError, ValueError):
                                pass
                        metadata.update(provider="error", error=error_type)
                        conn.execute(
                            "UPDATE messages SET content = ?, metadata_json = " + merged_metadata_sql() + ", status = ? "
                            "FROM (SELECT ?::jsonb AS metadata) incoming WHERE id = ? AND status = ?",
                            (
                                friendly,
                                "error",
                                json.dumps(metadata, ensure_ascii=False),
                                message_id,
                                "streaming",
                            ),
                        )
                    return metadata

                error_metadata: Dict[str, Any] = {"provider": "error", "error": error_type}
                try:
                    error_metadata = await run_in_threadpool(mark_error)
                except Exception:
                    pass
                try:
                    await self.publish(message_id, "error", {"code": "generation_failed", "error": error_type})
                    await self.publish(message_id, "done", {"id": message_id, "status": "error", "content": friendly, "metadata": error_metadata})
                except Exception:
                    pass
            finally:
                if next_task is not None and not next_task.done():
                    next_task.cancel()
                    try:
                        await next_task
                    except (asyncio.CancelledError, StopAsyncIteration):
                        pass
                if flush_task is not None and not flush_task.done():
                    flush_task.cancel()
                    try:
                        await flush_task
                    except asyncio.CancelledError:
                        pass
                if heartbeat is not None:
                    heartbeat.cancel()
                    try:
                        await heartbeat
                    except asyncio.CancelledError:
                        pass
                self._cancel_timeout(message_id)
                current = asyncio.current_task()
                if self.tasks.get(message_id) is current:
                    self.tasks.pop(message_id, None)

        old = self.tasks.get(message_id)
        if old is not None and not old.done():
            if not restart:
                return old
            # The previous confirmation stream may still be finishing its
            # cleanup after publishing done. Never overlap two owners.
            await old
        if restart:
            self._cancel_cleanup(message_id)
            await self._cleanup_message(message_id)
            try:
                await self._redis_quick("delete", self._key(message_id), self._cancel_key(message_id))
            except Exception:
                pass
        self._cancel_timeout(message_id)
        self._timed_out.pop(message_id, None)
        self._cancel_cache.pop(message_id, None)
        self.cancel_events[message_id] = asyncio.Event()
        if epoch:
            self.epochs[message_id] = epoch
        task = asyncio.create_task(run(), name="luma-generation-" + message_id)
        self.tasks[message_id] = task
        if timeout_seconds is None:
            try:
                timeout_seconds = float(os.getenv("GENERATION_MAX_SECONDS", "600"))
            except (TypeError, ValueError):
                timeout_seconds = 600.0
        timeout_seconds = max(0.0, float(timeout_seconds))
        if timeout_seconds > 0:
            loop = asyncio.get_running_loop()

            def deadline() -> None:
                if task.done():
                    return
                self._timed_out[message_id] = True
                self.cancel_events.setdefault(message_id, asyncio.Event()).set()
                self._cancel_cache[message_id] = (time.monotonic(), True)
                task.cancel()
                asyncio.create_task(self._publish_cancel_marker(message_id))

            self._timeout_handles[message_id] = loop.call_later(timeout_seconds, deadline)
        return task

    async def is_cancelled(self, message_id: str) -> bool:
        event = self.cancel_events.get(message_id)
        if event is not None and event.is_set():
            return True
        now = time.monotonic()
        cached = self._cancel_cache.get(message_id)
        if cached is not None and now - cached[0] < 1.0:
            return cached[1]
        try:
            value = await self._redis_quick("get", self._cancel_key(message_id))
            if isinstance(value, bytes):
                value = value.decode("utf-8", "ignore")
            cancelled = str(value).strip().lower() in {"1", "true", "yes", "on"}
            self._cancel_cache[message_id] = (now, cancelled)
            return cancelled
        except Exception:
            self._cancel_cache[message_id] = (now, False)
            return False

    def is_timed_out(self, message_id: str) -> bool:
        """Return whether the generation deadline fired locally."""
        return bool(self._timed_out.get(message_id))

    async def cancel(self, message_id: str) -> None:
        self.cancel_events.setdefault(message_id, asyncio.Event()).set()
        self._cancel_cache[message_id] = (time.monotonic(), True)
        await self._publish_cancel_marker(message_id)

    async def _local_items(self, message_id: str, after: Optional[str]) -> List[StoredEvent]:
        async with self._state_lock():
            items = list(self.histories.get(message_id, []))
        if not after:
            return items
        for index, item in enumerate(items):
            if item[0] == after:
                return items[index + 1 :]
        try:
            marker = int(after)
        except (TypeError, ValueError):
            return items
        return [item for item in items if item[0].isdigit() and int(item[0]) > marker]

    async def _redis_items(self, message_id: str, after: Optional[str]) -> Tuple[bool, List[StoredEvent]]:
        try:
            minimum = "-" if not after else "(" + after
            rows = await self._redis_quick("xrange", self._key(message_id), min=minimum, max="+")
        except Exception:
            return False, []
        result: List[StoredEvent] = []
        for stream_id, fields in rows or []:
            if isinstance(stream_id, bytes):
                stream_id = stream_id.decode("utf-8", "replace")
            if not isinstance(fields, dict):
                continue
            event = fields.get("event", "")
            data = fields.get("data", "{}")
            if isinstance(event, bytes):
                event = event.decode("utf-8", "replace")
            if isinstance(data, bytes):
                data = data.decode("utf-8", "replace")
            try:
                decoded = json.loads(data)
            except (TypeError, ValueError):
                decoded = {}
            result.append((str(stream_id), str(event), decoded if isinstance(decoded, dict) else {}))
        return True, result

    async def _message_status(self, message_id: str) -> Optional[str]:
        """Read a terminal marker without blocking the event loop."""
        def read() -> Optional[str]:
            with get_connection() as conn:
                row = conn.execute("SELECT status FROM messages WHERE id = ?", (message_id,)).fetchone()
            return str(row["status"]) if row is not None and row.get("status") else None

        try:
            return await run_in_threadpool(read)
        except Exception:
            return None

    async def _wait_local_completion(self, message_id: str) -> None:
        """A terminal stream event also waits for the local runner cleanup."""
        task = self.tasks.get(message_id)
        if task is not None and task is not asyncio.current_task():
            # Subscriber cancellation must not cancel its durable generation.
            # publish uses nonblocking queue delivery, so the producer does
            # not depend on this consumer advancing past the terminal event.
            await asyncio.gather(asyncio.shield(task), return_exceptions=True)

    async def subscribe(
        self, message_id: str, after: Optional[str] = None
    ) -> AsyncIterator[StoredEvent]:
        """Yield events after ``after`` until a terminal event is seen."""
        # A generation owned by this worker already has the same stream ids
        # mirrored locally.  Avoid an XRANGE thread-pool round trip for every
        # event in that case; Redis is reserved for cross-worker resumes.
        local = await self.is_local(message_id)
        epoch = await self._message_epoch(message_id)

        def matches_epoch(item: StoredEvent) -> bool:
            return not epoch or item[2].get("generation_epoch") == epoch

        if local:
            use_redis = False
            redis_ok = False
            initial = await self._local_items(message_id, after)
        else:
            redis_ok, initial = await self._redis_items(message_id, after)
            # A cross-worker subscriber must continue probing Redis even when
            # the first request fails.  A local queue cannot receive events
            # owned by another worker, while the HTTP layer checks the durable
            # row every 15 seconds for a terminal fallback.
            use_redis = True
            if not redis_ok:
                initial = []
        initial = [item for item in initial if matches_epoch(item)]
        for item in initial:
            if item[1] == "done":
                await self._wait_local_completion(message_id)
            yield item
        # ``error`` is a regular diagnostic event. The producer follows it
        # with ``done`` carrying the persisted terminal message, so do not
        # close a subscriber on the error event itself.
        if initial and initial[-1][1] == "done":
            return

        # A caller may resume from the terminal event id itself. There are no
        # events *after* that id, so close immediately instead of waiting for a
        # queue item that can never arrive.
        if after and not initial:
            if use_redis:
                _, all_events = await self._redis_items(message_id, None)
            else:
                all_events = await self._local_items(message_id, None)
            if all_events and matches_epoch(all_events[-1]) and all_events[-1][1] == "done" and all_events[-1][0] == after:
                await self._wait_local_completion(message_id)
                return

        # If this worker has no task and there is no stream history, a terminal
        # database row means the Redis retention window has elapsed (or Redis
        # is unavailable). Let the HTTP layer emit the durable final message.
        # A still-streaming row may belong to another worker, so keep polling
        # Redis in that case instead of returning prematurely.
        if not initial and message_id not in self.tasks:
            status = await self._message_status(message_id)
            if status in {"complete", "incomplete", "error"}:
                return

        queue: asyncio.Queue[StoredEvent] = asyncio.Queue()
        cursor = initial[-1][0] if initial else after
        next_redis_probe = 0.0
        missed: List[StoredEvent] = []
        async with self._state_lock():
            self.queues.setdefault(message_id, []).append(queue)
            latest = list(self.histories.get(message_id, []))
            if not use_redis:
                if cursor:
                    for index, item in enumerate(latest):
                        if item[0] == cursor:
                            missed = latest[index + 1 :]
                            break
                    if not missed:
                        try:
                            marker = int(cursor)
                        except (TypeError, ValueError):
                            # An unknown cursor can only be safely resumed by
                            # replaying the bounded local history.
                            missed = latest
                        else:
                            missed = [
                                item for item in latest
                                if item[0].isdigit() and int(item[0]) > marker
                            ]
                else:
                    missed = latest
        try:
            for item in missed:
                if not matches_epoch(item):
                    continue
                cursor = item[0]
                if item[1] == "done":
                    await self._wait_local_completion(message_id)
                yield item
                if item[1] == "done":
                    return
            while True:
                if use_redis:
                    ok, latest_redis = await self._redis_items(message_id, cursor)
                    if not ok:
                        await asyncio.sleep(0.25)
                        continue
                    for item in latest_redis:
                        cursor = item[0]
                        if not matches_epoch(item):
                            continue
                        if item[1] == "done":
                            await self._wait_local_completion(message_id)
                        yield item
                        if item[1] == "done":
                            return
                    # Cross-worker polling is deliberately slower than local
                    # queue delivery to keep Redis and the thread pool cool.
                    await asyncio.sleep(0.25)
                    continue
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # A worker crash can leave a local queue without a
                    # producer.  Check the durable row periodically so a
                    # subscriber does not wait forever for a terminal event.
                    if message_id not in self.tasks:
                        status = await self._message_status(message_id)
                        if status in {"complete", "incomplete", "error"}:
                            return
                    continue
                if cursor and item[0] == cursor:
                    continue
                if not matches_epoch(item):
                    continue
                cursor = item[0]
                if item[1] == "done":
                    await self._wait_local_completion(message_id)
                yield item
                if item[1] == "done":
                    return
        finally:
            async with self._state_lock():
                subscribers = self.queues.get(message_id, [])
                if queue in subscribers:
                    subscribers.remove(queue)
                if not subscribers:
                    self.queues.pop(message_id, None)

    async def shutdown(self) -> None:
        """Cancel and await all in-process generations."""
        tasks = list(self.tasks.values())
        for message_id in list(self.tasks):
            await self.cancel(message_id)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def recover_stale(self) -> None:
        """Recover crashed streaming rows with a Redis-aware age cutoff."""

        def recover() -> None:
            # Keep the advisory transaction open across the candidate scan and
            # Redis checks so two workers cannot both reap the same rows.
            with get_connection() as conn:
                lock = conn.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtext(?)) AS locked",
                    ("luma-generation-recovery",),
                ).fetchone()
                lock_value = lock.get("locked") if isinstance(lock, dict) else (lock[0] if lock else False)
                if not lock or not bool(lock_value):
                    return

                redis_client: Any = None
                redis_available = False
                if os.getenv("REDIS_HOST", "").strip():
                    try:
                        from .. import db

                        redis_client = db._redis_client()
                        redis_available = bool(redis_client.ping())
                    except Exception:
                        redis_client = None

                if not redis_available:
                    conn.execute(
                        "UPDATE messages SET status = ? WHERE status = ? "
                        "AND created_at::timestamptz < CURRENT_TIMESTAMP - INTERVAL '10 minutes'",
                        ("incomplete", "streaming"),
                    )
                    return

                rows = conn.execute(
                    "SELECT id FROM messages WHERE status = ? "
                    "AND created_at::timestamptz < CURRENT_TIMESTAMP - INTERVAL '2 minutes'",
                    ("streaming",),
                ).fetchall()
                stale_ids: List[str] = []
                try:
                    for row in rows:
                        message_id = str(row["id"])
                        if not redis_client.exists(self._alive_key(message_id)):
                            stale_ids.append(message_id)
                except Exception:
                    # If Redis becomes unavailable during the scan, use the
                    # conservative ten-minute rule for this pass.
                    conn.execute(
                        "UPDATE messages SET status = ? WHERE status = ? "
                        "AND created_at::timestamptz < CURRENT_TIMESTAMP - INTERVAL '10 minutes'",
                        ("incomplete", "streaming"),
                    )
                    return
                for message_id in stale_ids:
                    conn.execute(
                        "UPDATE messages SET status = ? WHERE id = ? AND status = ?",
                        ("incomplete", message_id, "streaming"),
                    )

        try:
            await run_in_threadpool(recover)
        except Exception:
            # Startup remains available while an older schema is migrated.
            return


manager = GenerationManager()


async def startup_recover() -> None:
    await manager.recover_stale()


async def shutdown() -> None:
    await manager.shutdown()

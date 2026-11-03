"""Server-sent events: every saved change becomes one small 'change' message.

A message carries the new revision and what kind of thing changed, never family
data; clients refetch /api/state when the revision moves. A dropped message costs a
refetch on the next one, never a wrong screen.
"""

import asyncio
import json


class EventBroker:
    def __init__(self, max_queue: int = 50):
        if max_queue < 1:
            raise ValueError("The event queue must hold at least one message.")
        self.max_queue = max_queue
        self._queues = set()
        self._loop = None

    def bind(self, loop) -> None:
        self._loop = loop

    @property
    def listeners(self) -> int:
        return len(self._queues)

    def publish(self, event: dict) -> None:
        """Thread-safe: the store calls this from worker threads after each save."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        message = {"revision": event["revision"], "kinds": list(event.get("kinds", ()))}
        if "signal" in event:
            message["signal"] = event["signal"]      # a WebRTC relay carries its payload, never family data
        try:
            loop.call_soon_threadsafe(self._fan_out, message)
        except RuntimeError:
            if not loop.is_closed():
                raise

    def _fan_out(self, event: dict) -> None:
        for queue in list(self._queues):
            if queue.full():
                queue.get_nowait()      # a stalled client loses the oldest; it refetches anyway
            queue.put_nowait(event)

    def subscribe(self) -> asyncio.Queue:
        queue = asyncio.Queue(maxsize=self.max_queue)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue) -> None:
        self._queues.discard(queue)


# Retain the name used by the implementation plan and later phases.
EventBus = EventBroker


def sse(event: str, data: dict, *, event_id=None) -> str:
    lines = [] if event_id is None else [f"id: {event_id}"]
    lines += [f"event: {event}", "data: " + json.dumps(data, separators=(",", ":"))]
    return "\n".join(lines) + "\n\n"


async def stream(bus: EventBus, revision, *, ping_seconds: float = 20, retry_ms: int = 3000,
                 is_disconnected=None):
    """The /api/events body. `revision()` reads the store's current revision."""
    queue = bus.subscribe()
    try:
        yield f"retry: {retry_ms}\n\n"
        yield sse("hello", {"revision": revision()})
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=ping_seconds)
            except TimeoutError:
                if is_disconnected is not None and await is_disconnected():
                    return
                yield ": ping\n\n"      # keeps the WebView and any proxy from timing the stream out
                continue
            yield sse("change", event, event_id=event.get("revision"))
    finally:
        bus.unsubscribe(queue)

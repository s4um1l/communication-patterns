"""Chapter 07 -- Pub/Sub: In-Process Event Broker

An educational pub/sub broker built on pure asyncio. No external dependencies.

This broker implements the core pub/sub primitives:
    - Topics: named channels that events are published to
    - Subscribers: async callbacks registered against topic patterns
    - Pattern matching: exact ("order.placed") and wildcard ("order.*")
    - Error isolation: one subscriber's failure does not affect others
    - Bounded queues: backpressure via per-subscriber message buffers
    - Metrics: track publish/deliver/fail counts and queue depths

Production systems use Redis, Kafka, or RabbitMQ for this. We use asyncio
queues to keep the focus on the PATTERN, not the infrastructure. The concepts
are identical -- only the transport changes.

Key design decisions:
    - Each subscriber gets its own asyncio.Queue (bounded, default 100)
    - Publishing is non-blocking: events go into queues, not directly to handlers
    - A background worker per subscriber drains its queue and invokes the callback
    - If a subscriber is slow, its queue fills up. When full: message dropped (logged)
    - Wildcard matching uses fnmatch-style patterns: "order.*" matches "order.placed"
"""

from __future__ import annotations

import asyncio
import fnmatch
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# A subscriber callback: receives (topic, event_data) and returns nothing.
# Must be an async function.
SubscriberCallback = Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]


@dataclass
class Event:
    """A single pub/sub event."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    topic: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class Subscription:
    """A registered subscriber."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    topic_pattern: str = ""
    callback: SubscriberCallback = None  # type: ignore[assignment]
    name: str = ""  # human-readable name for logging
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=100))
    _worker_task: asyncio.Task | None = field(default=None, repr=False)


@dataclass
class BrokerMetrics:
    """Observable metrics for the broker."""

    messages_published: int = 0
    messages_delivered: int = 0
    messages_failed: int = 0
    messages_dropped: int = 0  # dropped due to full queues
    active_subscribers: int = 0
    topics_seen: set = field(default_factory=set)

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of all metrics."""
        return {
            "messages_published": self.messages_published,
            "messages_delivered": self.messages_delivered,
            "messages_failed": self.messages_failed,
            "messages_dropped": self.messages_dropped,
            "active_subscribers": self.active_subscribers,
            "topics_seen": sorted(self.topics_seen),
        }


# ---------------------------------------------------------------------------
# Event Broker
# ---------------------------------------------------------------------------


class EventBroker:
    """In-process pub/sub event broker.

    Usage:
        broker = EventBroker()
        await broker.start()

        # Subscribe
        async def on_order(topic, data):
            print(f"Got {topic}: {data}")

        await broker.subscribe("order.*", on_order, name="kitchen")

        # Publish
        await broker.publish("order.placed", {"order_id": "abc123"})

        # Cleanup
        await broker.stop()
    """

    def __init__(self, *, name: str = "EventBroker") -> None:
        self.name = name
        self._subscriptions: list[Subscription] = []
        self._metrics = BrokerMetrics()
        self._running = False
        self._event_log: list[Event] = []  # for debugging / replay
        self._max_log_size = 1000

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the broker. Must be called before publishing or subscribing."""
        self._running = True
        print(f"  [{self.name}] Broker started")

    async def stop(self) -> None:
        """Stop the broker and drain all subscriber queues."""
        self._running = False

        # Cancel all subscriber worker tasks
        for sub in self._subscriptions:
            if sub._worker_task and not sub._worker_task.done():
                sub._worker_task.cancel()
                try:
                    await sub._worker_task
                except asyncio.CancelledError:
                    pass

        self._subscriptions.clear()
        self._metrics.active_subscribers = 0
        print(f"  [{self.name}] Broker stopped")

    # ------------------------------------------------------------------
    # Subscribe
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        topic_pattern: str,
        callback: SubscriberCallback,
        *,
        name: str = "",
        queue_size: int = 100,
    ) -> str:
        """Register a subscriber for a topic pattern.

        Args:
            topic_pattern: Exact topic ("order.placed") or wildcard ("order.*").
            callback: Async function(topic, data) called for each matching event.
            name: Human-readable name for logging.
            queue_size: Max events buffered for this subscriber.

        Returns:
            Subscription ID (use to unsubscribe).
        """
        if not self._running:
            raise RuntimeError("Broker is not running. Call await broker.start() first.")

        sub = Subscription(
            topic_pattern=topic_pattern,
            callback=callback,
            name=name or f"sub_{len(self._subscriptions)}",
            queue=asyncio.Queue(maxsize=queue_size),
        )

        # Start a background worker that drains this subscriber's queue
        sub._worker_task = asyncio.create_task(
            self._subscriber_worker(sub),
            name=f"worker-{sub.name}",
        )

        self._subscriptions.append(sub)
        self._metrics.active_subscribers = len(self._subscriptions)

        print(
            f"  [{self.name}] Subscribed: {sub.name!r} "
            f"on pattern {sub.topic_pattern!r} (id={sub.id})"
        )
        return sub.id

    async def unsubscribe(self, subscription_id: str) -> bool:
        """Remove a subscriber by ID. Returns True if found and removed."""
        for i, sub in enumerate(self._subscriptions):
            if sub.id == subscription_id:
                if sub._worker_task and not sub._worker_task.done():
                    sub._worker_task.cancel()
                    try:
                        await sub._worker_task
                    except asyncio.CancelledError:
                        pass
                self._subscriptions.pop(i)
                self._metrics.active_subscribers = len(self._subscriptions)
                print(f"  [{self.name}] Unsubscribed: {sub.name!r} (id={sub.id})")
                return True
        return False

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    async def publish(self, topic: str, data: dict[str, Any]) -> Event:
        """Publish an event to a topic.

        This is the publisher's ONLY cost: create the event and enqueue it
        to matching subscribers. The actual callback invocation happens
        asynchronously in each subscriber's worker task.

        Returns the Event object (with assigned ID and timestamp).
        """
        if not self._running:
            raise RuntimeError("Broker is not running. Call await broker.start() first.")

        event = Event(topic=topic, data=data)

        # Log the event
        self._event_log.append(event)
        if len(self._event_log) > self._max_log_size:
            self._event_log = self._event_log[-self._max_log_size:]

        self._metrics.messages_published += 1
        self._metrics.topics_seen.add(topic)

        # Route to matching subscribers
        matched = 0
        for sub in self._subscriptions:
            if self._matches(sub.topic_pattern, topic):
                try:
                    sub.queue.put_nowait(event)
                    matched += 1
                except asyncio.QueueFull:
                    self._metrics.messages_dropped += 1
                    print(
                        f"  [{self.name}] DROPPED event {event.id} for "
                        f"{sub.name!r} (queue full, size={sub.queue.qsize()})"
                    )

        print(
            f"  [{self.name}] Published: topic={topic!r} "
            f"id={event.id} -> {matched} subscriber(s)"
        )
        return event

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    @property
    def metrics(self) -> BrokerMetrics:
        return self._metrics

    def get_queue_depths(self) -> dict[str, int]:
        """Return current queue depth for each subscriber."""
        return {sub.name: sub.queue.qsize() for sub in self._subscriptions}

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _matches(pattern: str, topic: str) -> bool:
        """Check if a topic matches a subscription pattern.

        Supports:
            - Exact match: "order.placed" matches "order.placed"
            - Wildcard: "order.*" matches "order.placed", "order.confirmed"
            - Deep wildcard: "order.#" matches "order.placed", "order.status.changed"
            - Universal: "*" matches everything

        Uses fnmatch for glob-style matching. The '#' wildcard is translated
        to '**' for recursive matching.
        """
        # Translate AMQP-style '#' (match multiple segments) to fnmatch '**'
        translated = pattern.replace("#", "**")
        return fnmatch.fnmatch(topic, translated)

    async def _subscriber_worker(self, sub: Subscription) -> None:
        """Background worker that drains a subscriber's queue.

        Each subscriber gets its own worker. This provides error isolation:
        if one subscriber's callback raises an exception, only that
        subscriber is affected. Other subscribers continue processing.

        This is the key architectural insight: the broker does not call
        callbacks inline during publish(). It enqueues events and lets
        each worker process them independently. This means:
            1. publish() returns immediately (non-blocking)
            2. Slow subscribers don't block fast ones
            3. Crashing subscribers don't crash the broker
        """
        print(f"  [{self.name}] Worker started for {sub.name!r}")

        while True:
            try:
                event = await sub.queue.get()

                try:
                    await sub.callback(event.topic, event.data)
                    self._metrics.messages_delivered += 1
                except Exception as exc:
                    # Error isolation: log the error but keep processing.
                    # In production, this is where you would:
                    #   1. Increment a failure counter
                    #   2. Send to dead letter queue after N failures
                    #   3. Trip a circuit breaker if failures are too frequent
                    self._metrics.messages_failed += 1
                    print(
                        f"  [{self.name}] ERROR in {sub.name!r} "
                        f"processing {event.topic}: {exc}"
                    )

                sub.queue.task_done()

            except asyncio.CancelledError:
                print(f"  [{self.name}] Worker stopped for {sub.name!r}")
                break

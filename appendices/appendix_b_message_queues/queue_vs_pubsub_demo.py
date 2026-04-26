"""Queue vs Pub/Sub -- see the difference in one script.

This demo runs two messaging patterns side-by-side using pure asyncio:

1. **Message Queue** (competing consumers): 3 workers share 10 messages.
   Each message goes to exactly ONE worker. Workers compete for work.

2. **Pub/Sub** (fan-out): 3 subscribers each receive ALL 10 messages.
   Every subscriber gets a copy. This is a broadcast.

Run with:
    uv run python -m appendices.appendix_b_message_queues.queue_vs_pubsub_demo
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

@dataclass
class Message:
    id: int
    payload: str
    created_at: float = field(default_factory=time.time)


def _header(text: str) -> None:
    width = 64
    print()
    print("=" * width)
    print(f"  {text}")
    print("=" * width)


def _sub_header(text: str) -> None:
    print(f"\n--- {text} ---\n")


# ---------------------------------------------------------------------------
# Part 1: Message Queue (competing consumers)
# ---------------------------------------------------------------------------

class MessageQueue:
    """A simple async message queue with competing consumers.

    Each message is delivered to exactly ONE consumer -- the next one
    that calls .get(). This is the "work queue" pattern.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._queue: asyncio.Queue[Message | None] = asyncio.Queue()

    async def put(self, message: Message) -> None:
        await self._queue.put(message)

    async def get(self) -> Message | None:
        return await self._queue.get()

    async def stop(self, num_consumers: int) -> None:
        """Send poison pills to shut down all consumers."""
        for _ in range(num_consumers):
            await self._queue.put(None)


async def queue_worker(
    worker_id: str,
    queue: MessageQueue,
    results: dict[str, list[int]],
) -> None:
    """A competing consumer. Pulls messages from the queue one at a time."""
    while True:
        msg = await queue.get()
        if msg is None:  # poison pill
            break
        # Simulate variable processing time
        delay = random.uniform(0.01, 0.05)
        await asyncio.sleep(delay)
        results[worker_id].append(msg.id)
        print(f"  [QUEUE] {worker_id} processed message {msg.id}: {msg.payload}")


async def run_message_queue_demo() -> dict[str, list[int]]:
    """Demonstrate competing consumers on a message queue."""
    _header("PART 1: MESSAGE QUEUE (Competing Consumers)")
    print()
    print("  3 workers share 10 messages. Each message goes to ONE worker.")
    print("  Workers compete -- whoever is free next takes the next message.")
    print()

    queue = MessageQueue("order-processing")
    results: dict[str, list[int]] = defaultdict(list)

    # Start 3 workers
    workers = [
        asyncio.create_task(queue_worker(f"Worker-{i+1}", queue, results))
        for i in range(3)
    ]

    # Publish 10 messages
    _sub_header("Publishing 10 messages to queue")
    for i in range(10):
        msg = Message(id=i + 1, payload=f"Order #{i + 1}")
        await queue.put(msg)
        print(f"  [PRODUCER] Enqueued message {msg.id}")

    # Stop workers and wait
    await queue.stop(num_consumers=3)
    await asyncio.gather(*workers)

    # Summary
    _sub_header("Results: which worker got which messages?")
    for worker_id in sorted(results):
        msg_ids = results[worker_id]
        print(f"  {worker_id}: processed messages {msg_ids} ({len(msg_ids)} total)")

    total = sum(len(v) for v in results.values())
    print(f"\n  Total messages processed: {total} (each message processed ONCE)")

    return results


# ---------------------------------------------------------------------------
# Part 2: Pub/Sub (fan-out)
# ---------------------------------------------------------------------------

class PubSubBroker:
    """A simple async pub/sub broker with fan-out delivery.

    Each message is delivered to ALL subscribers. Every subscriber
    gets a copy. This is the broadcast pattern.
    """

    def __init__(self, topic: str) -> None:
        self.topic = topic
        self._subscribers: dict[str, asyncio.Queue[Message | None]] = {}

    def subscribe(self, subscriber_id: str) -> asyncio.Queue[Message | None]:
        """Register a subscriber and return their personal mailbox."""
        mailbox: asyncio.Queue[Message | None] = asyncio.Queue()
        self._subscribers[subscriber_id] = mailbox
        return mailbox

    async def publish(self, message: Message) -> None:
        """Fan-out: deliver a COPY to every subscriber's mailbox."""
        for mailbox in self._subscribers.values():
            await mailbox.put(message)

    async def stop(self) -> None:
        """Send poison pills to all subscribers."""
        for mailbox in self._subscribers.values():
            await mailbox.put(None)


async def pubsub_subscriber(
    subscriber_id: str,
    mailbox: asyncio.Queue[Message | None],
    results: dict[str, list[int]],
) -> None:
    """A fan-out subscriber. Receives every published message."""
    while True:
        msg = await mailbox.get()
        if msg is None:
            break
        delay = random.uniform(0.01, 0.05)
        await asyncio.sleep(delay)
        results[subscriber_id].append(msg.id)
        print(f"  [PUBSUB] {subscriber_id} received message {msg.id}: {msg.payload}")


async def run_pubsub_demo() -> dict[str, list[int]]:
    """Demonstrate fan-out pub/sub delivery."""
    _header("PART 2: PUB/SUB (Fan-Out)")
    print()
    print("  3 subscribers each receive ALL 10 messages.")
    print("  Every message is broadcast -- everyone gets a copy.")
    print()

    broker = PubSubBroker("order.placed")
    results: dict[str, list[int]] = defaultdict(list)

    # Register 3 subscribers
    subscribers = []
    for i in range(3):
        sub_id = f"Subscriber-{i+1}"
        mailbox = broker.subscribe(sub_id)
        task = asyncio.create_task(
            pubsub_subscriber(sub_id, mailbox, results)
        )
        subscribers.append(task)

    # Publish 10 messages
    _sub_header("Publishing 10 messages to topic 'order.placed'")
    for i in range(10):
        msg = Message(id=i + 1, payload=f"Order #{i + 1}")
        await broker.publish(msg)
        print(f"  [PUBLISHER] Published message {msg.id}")

    # Stop and wait
    await broker.stop()
    await asyncio.gather(*subscribers)

    # Summary
    _sub_header("Results: which subscriber got which messages?")
    for sub_id in sorted(results):
        msg_ids = results[sub_id]
        print(f"  {sub_id}: received messages {msg_ids} ({len(msg_ids)} total)")

    total = sum(len(v) for v in results.values())
    print(f"\n  Total message deliveries: {total} (each message delivered to ALL subscribers)")

    return results


# ---------------------------------------------------------------------------
# Part 3: Side-by-side comparison
# ---------------------------------------------------------------------------

async def run_comparison(
    queue_results: dict[str, list[int]],
    pubsub_results: dict[str, list[int]],
) -> None:
    """Print the key differences side by side."""
    _header("COMPARISON: Queue vs Pub/Sub")

    queue_total = sum(len(v) for v in queue_results.values())
    pubsub_total = sum(len(v) for v in pubsub_results.values())

    print(f"""
  10 messages published in each case.

  MESSAGE QUEUE (competing consumers):
    - Total deliveries:  {queue_total}
    - Each message went to:  exactly ONE worker
    - Pattern:  work distribution (like a checkout line)
    - Use case: task processing, job queues, load balancing

  PUB/SUB (fan-out):
    - Total deliveries:  {pubsub_total}
    - Each message went to:  ALL subscribers ({len(pubsub_results)})
    - Pattern:  broadcast (like a radio station)
    - Use case: event notification, decoupling services

  THE KEY DIFFERENCE:
    Queue:   10 messages -> {queue_total} deliveries  (distributed)
    Pub/Sub: 10 messages -> {pubsub_total} deliveries (replicated)

  In production:
    - Redis Pub/Sub, Kafka consumer groups (across groups) = fan-out
    - RabbitMQ queues, Redis Streams consumer groups,
      Kafka consumer groups (within a group)       = competing consumers
    - Kafka combines both: fan-out across groups,
      competing consumers within each group.
""")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print()
    print("  Message Queues vs Pub/Sub -- Live Demonstration")
    print("  ================================================")
    print()
    print("  This demo shows two fundamentally different messaging patterns.")
    print("  Watch the output carefully: the SAME 10 messages are published")
    print("  in both cases, but the delivery behavior is completely different.")

    queue_results = await run_message_queue_demo()
    pubsub_results = await run_pubsub_demo()
    await run_comparison(queue_results, pubsub_results)


if __name__ == "__main__":
    asyncio.run(main())

"""Kafka simulation -- partitions, consumer groups, offsets, and rebalancing.

Pure Python simulation of Kafka's core concepts:
- Partitioned topics with configurable partition count
- Producer with hash-based and round-robin partitioning
- Consumer groups with partition assignment
- Offset tracking per consumer per partition
- Replay from any offset
- Consumer join/leave with rebalancing

Run with:
    uv run python -m appendices.appendix_b_message_queues.kafka_simulation
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import defaultdict
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Core Kafka-like data structures
# ---------------------------------------------------------------------------

@dataclass
class Record:
    """A single record in a partition log."""
    offset: int
    key: str | None
    value: str
    timestamp: float = field(default_factory=time.time)

    def __repr__(self) -> str:
        return f"Record(offset={self.offset}, key={self.key!r}, value={self.value!r})"


class Partition:
    """An append-only log of records.

    Like a Kafka partition: records are appended at the end,
    consumers read by offset, and records stay until retention expires.
    """

    def __init__(self, partition_id: int) -> None:
        self.id = partition_id
        self._log: list[Record] = []

    @property
    def latest_offset(self) -> int:
        return len(self._log)

    def append(self, key: str | None, value: str) -> Record:
        record = Record(offset=len(self._log), key=key, value=value)
        self._log.append(record)
        return record

    def read(self, offset: int, max_records: int = 1) -> list[Record]:
        """Read records starting from offset."""
        if offset < 0 or offset >= len(self._log):
            return []
        end = min(offset + max_records, len(self._log))
        return self._log[offset:end]

    def read_all(self) -> list[Record]:
        return list(self._log)


class Topic:
    """A partitioned topic -- the core Kafka abstraction.

    Messages are distributed across partitions by key hash (or round-robin
    if no key). Ordering is guaranteed within a partition, not across.
    """

    def __init__(self, name: str, num_partitions: int = 3) -> None:
        self.name = name
        self.partitions = [Partition(i) for i in range(num_partitions)]
        self._rr_counter = 0  # for round-robin

    def produce(self, key: str | None, value: str) -> tuple[int, Record]:
        """Append a record to the appropriate partition.

        Returns (partition_id, record).
        """
        if key is not None:
            # Hash-based partitioning: same key always goes to same partition
            h = int(hashlib.md5(key.encode()).hexdigest(), 16)
            partition_id = h % len(self.partitions)
        else:
            # Round-robin: spread evenly
            partition_id = self._rr_counter % len(self.partitions)
            self._rr_counter += 1

        record = self.partitions[partition_id].append(key, value)
        return partition_id, record

    def get_partition(self, partition_id: int) -> Partition:
        return self.partitions[partition_id]


# ---------------------------------------------------------------------------
# Consumer group with partition assignment and rebalancing
# ---------------------------------------------------------------------------

class Consumer:
    """A consumer within a consumer group.

    Tracks its own offset per assigned partition (just like real Kafka).
    """

    def __init__(self, consumer_id: str) -> None:
        self.id = consumer_id
        self.assigned_partitions: list[int] = []
        self.offsets: dict[int, int] = {}  # partition_id -> current offset
        self.messages_consumed: list[tuple[int, Record]] = []  # (partition, record)

    def assign(self, partitions: list[int]) -> None:
        """Assign partitions to this consumer (called during rebalancing)."""
        self.assigned_partitions = partitions
        # Preserve offsets for partitions we already had, start at 0 for new ones
        for p in partitions:
            if p not in self.offsets:
                self.offsets[p] = 0

    def poll(self, topic: Topic, max_records: int = 10) -> list[tuple[int, Record]]:
        """Read new records from assigned partitions."""
        results: list[tuple[int, Record]] = []
        for pid in self.assigned_partitions:
            offset = self.offsets.get(pid, 0)
            records = topic.get_partition(pid).read(offset, max_records)
            for record in records:
                results.append((pid, record))
                self.offsets[pid] = record.offset + 1
                self.messages_consumed.append((pid, record))
        return results

    def seek(self, partition_id: int, offset: int) -> None:
        """Reset offset for a partition (enables replay)."""
        self.offsets[partition_id] = offset

    def __repr__(self) -> str:
        return f"Consumer({self.id!r}, partitions={self.assigned_partitions})"


class ConsumerGroup:
    """Manages a group of consumers with automatic partition assignment.

    Like a Kafka consumer group: each partition is assigned to exactly
    one consumer. When consumers join or leave, partitions are rebalanced.
    """

    def __init__(self, group_id: str, topic: Topic) -> None:
        self.group_id = group_id
        self.topic = topic
        self.consumers: dict[str, Consumer] = {}
        self._committed_offsets: dict[int, int] = {}  # partition -> committed offset

    def add_consumer(self, consumer_id: str) -> Consumer:
        """Add a consumer to the group and trigger rebalancing."""
        consumer = Consumer(consumer_id)
        self.consumers[consumer_id] = consumer
        self._rebalance()
        return consumer

    def remove_consumer(self, consumer_id: str) -> None:
        """Remove a consumer and trigger rebalancing."""
        if consumer_id in self.consumers:
            # Save committed offsets before removing
            consumer = self.consumers[consumer_id]
            for pid in consumer.assigned_partitions:
                self._committed_offsets[pid] = consumer.offsets.get(pid, 0)
            del self.consumers[consumer_id]
            self._rebalance()

    def _rebalance(self) -> None:
        """Reassign partitions across consumers (range assignment strategy)."""
        if not self.consumers:
            return

        num_partitions = len(self.topic.partitions)
        consumer_list = sorted(self.consumers.keys())
        num_consumers = len(consumer_list)

        print(f"\n  [REBALANCE] Group '{self.group_id}': "
              f"{num_consumers} consumers, {num_partitions} partitions")

        # Range assignment: divide partitions as evenly as possible
        assignments: dict[str, list[int]] = {c: [] for c in consumer_list}
        for pid in range(num_partitions):
            consumer_idx = pid % num_consumers
            assignments[consumer_list[consumer_idx]].append(pid)

        # Apply assignments
        for consumer_id, partitions in assignments.items():
            consumer = self.consumers[consumer_id]
            old_partitions = set(consumer.assigned_partitions)
            new_partitions = set(partitions)

            # Restore committed offsets for newly assigned partitions
            for pid in new_partitions - old_partitions:
                if pid in self._committed_offsets:
                    consumer.offsets[pid] = self._committed_offsets[pid]

            consumer.assign(partitions)
            status = "unchanged" if old_partitions == new_partitions else "REASSIGNED"
            print(f"    {consumer_id}: partitions {partitions} [{status}]")

    def get_lag(self) -> dict[str, dict[int, int]]:
        """Calculate consumer lag (how far behind each consumer is)."""
        lag: dict[str, dict[int, int]] = {}
        for consumer_id, consumer in self.consumers.items():
            lag[consumer_id] = {}
            for pid in consumer.assigned_partitions:
                latest = self.topic.get_partition(pid).latest_offset
                current = consumer.offsets.get(pid, 0)
                lag[consumer_id][pid] = latest - current
        return lag


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _header(text: str) -> None:
    width = 68
    print()
    print("=" * width)
    print(f"  {text}")
    print("=" * width)


def _sub_header(text: str) -> None:
    print(f"\n--- {text} ---\n")


# ---------------------------------------------------------------------------
# Demo scenarios
# ---------------------------------------------------------------------------

def demo_partitioned_topic() -> Topic:
    """Demo 1: Producing to a partitioned topic."""
    _header("DEMO 1: Partitioned Topic")
    print()
    print("  A Kafka topic has multiple partitions. Each partition is an")
    print("  independent, append-only log. Messages with the same key")
    print("  always go to the same partition (ordering guarantee).")
    print()

    topic = Topic("orders", num_partitions=3)

    # Produce orders with order_id as key
    orders = [
        ("order-A", "OrderPlaced: Pizza, $15"),
        ("order-B", "OrderPlaced: Burger, $12"),
        ("order-C", "OrderPlaced: Sushi, $25"),
        ("order-A", "OrderConfirmed: Pizza"),
        ("order-B", "OrderConfirmed: Burger"),
        ("order-A", "OrderPreparing: Pizza"),
        ("order-C", "OrderConfirmed: Sushi"),
        ("order-B", "OrderPreparing: Burger"),
        ("order-A", "OrderReady: Pizza"),
        ("order-C", "OrderPreparing: Sushi"),
    ]

    _sub_header("Producing 10 events with key-based partitioning")
    for key, value in orders:
        pid, record = topic.produce(key, value)
        print(f"  key={key:8s} -> partition {pid}, offset {record.offset}: {value}")

    _sub_header("Partition contents (each is an ordered log)")
    for partition in topic.partitions:
        records = partition.read_all()
        print(f"  Partition {partition.id} ({len(records)} records):")
        for r in records:
            print(f"    [{r.offset}] key={r.key!r:10s} {r.value}")

    print()
    print("  NOTICE: All events for 'order-A' are in the SAME partition.")
    print("  This guarantees they will be consumed in order: Placed -> Confirmed")
    print("  -> Preparing -> Ready. Across partitions, no such guarantee.")

    return topic


def demo_consumer_groups(topic: Topic) -> None:
    """Demo 2: Consumer groups with partition assignment."""
    _header("DEMO 2: Consumer Groups")
    print()
    print("  Within a consumer group, each partition is assigned to exactly")
    print("  one consumer. This is how Kafka does competing consumers.")
    print("  Across groups, each group independently reads all messages (fan-out).")
    print()

    # -- Group 1: kitchen service (2 consumers initially) --
    _sub_header("Creating consumer group 'kitchen' with 2 consumers")
    kitchen_group = ConsumerGroup("kitchen", topic)
    k1 = kitchen_group.add_consumer("kitchen-1")
    k2 = kitchen_group.add_consumer("kitchen-2")

    # Each consumer polls their assigned partitions
    _sub_header("Kitchen consumers polling")
    for consumer in [k1, k2]:
        records = consumer.poll(topic)
        print(f"  {consumer.id} (partitions {consumer.assigned_partitions}):")
        for pid, record in records:
            print(f"    partition {pid}, offset {record.offset}: {record.value}")
        if not records:
            print("    (no records -- no assigned partitions with data)")

    # -- Group 2: analytics service (1 consumer, reads everything) --
    _sub_header("Creating consumer group 'analytics' with 1 consumer")
    analytics_group = ConsumerGroup("analytics", topic)
    a1 = analytics_group.add_consumer("analytics-1")

    records = a1.poll(topic)
    print(f"  {a1.id} (partitions {a1.assigned_partitions}):")
    for pid, record in records:
        print(f"    partition {pid}, offset {record.offset}: {record.value}")

    print()
    print("  KEY INSIGHT: Both 'kitchen' and 'analytics' groups read the")
    print("  same 10 messages. This is fan-out across groups. But within")
    print("  the 'kitchen' group, the 2 consumers split the work.")

    # -- Consumer lag --
    _sub_header("Consumer lag (how far behind)")
    for group_name, group in [("kitchen", kitchen_group), ("analytics", analytics_group)]:
        lag = group.get_lag()
        for consumer_id, partition_lag in lag.items():
            total_lag = sum(partition_lag.values())
            print(f"  [{group_name}] {consumer_id}: "
                  f"per-partition lag = {dict(partition_lag)}, total = {total_lag}")


def demo_rebalancing(topic: Topic) -> None:
    """Demo 3: What happens when consumers join and leave."""
    _header("DEMO 3: Rebalancing")
    print()
    print("  When a consumer joins or leaves, Kafka reassigns partitions.")
    print("  This is called rebalancing. Watch how partitions move.")

    group = ConsumerGroup("demo-group", topic)

    _sub_header("Step 1: Add consumer-1 (gets all 3 partitions)")
    c1 = group.add_consumer("consumer-1")

    _sub_header("Step 2: Add consumer-2 (partitions split)")
    c2 = group.add_consumer("consumer-2")

    _sub_header("Step 3: Add consumer-3 (one partition each)")
    c3 = group.add_consumer("consumer-3")

    _sub_header("Step 4: Add consumer-4 (one will be IDLE -- only 3 partitions!)")
    c4 = group.add_consumer("consumer-4")

    print()
    print("  NOTICE: consumer-4 has NO partitions. In Kafka, the maximum")
    print("  parallelism equals the number of partitions. Extra consumers are idle.")
    print("  This is why partition count must be planned at topic creation time.")

    _sub_header("Step 5: Remove consumer-2 (partitions redistribute)")
    group.remove_consumer("consumer-2")

    _sub_header("Step 6: Remove consumer-4 (was idle, nothing changes)")
    group.remove_consumer("consumer-4")

    # Final state
    _sub_header("Final consumer assignments")
    for cid, consumer in sorted(group.consumers.items()):
        print(f"  {cid}: partitions {consumer.assigned_partitions}")


def demo_replay(topic: Topic) -> None:
    """Demo 4: Offset-based replay."""
    _header("DEMO 4: Replay (Offset Reset)")
    print()
    print("  In Kafka, messages are NOT deleted when consumed. Consumers")
    print("  track their own offset. You can reset the offset to replay.")
    print()

    group = ConsumerGroup("replay-demo", topic)
    consumer = group.add_consumer("replayer")

    # Read all messages first
    _sub_header("First read: consume all messages")
    records = consumer.poll(topic)
    print(f"  Consumed {len(records)} records.")
    print(f"  Current offsets: {dict(consumer.offsets)}")

    # Try to read again -- nothing new
    _sub_header("Second read: try again (nothing new)")
    records = consumer.poll(topic)
    print(f"  Consumed {len(records)} records (expected: 0, already at end).")

    # Reset offset to replay partition 0 from the beginning
    _sub_header("Reset partition 0 offset to 0 (replay from beginning)")
    consumer.seek(partition_id=0, offset=0)
    print(f"  Offsets after reset: {dict(consumer.offsets)}")

    records = consumer.poll(topic)
    print(f"\n  Re-consumed {len(records)} records from partition 0:")
    for pid, record in records:
        print(f"    partition {pid}, offset {record.offset}: {record.value}")

    print()
    print("  THIS IS KAFKA'S SUPERPOWER: replay. A new service can start")
    print("  from offset 0 and reprocess all historical events. No other")
    print("  messaging system makes this as easy.")


def demo_multi_group_fanout(topic: Topic) -> None:
    """Demo 5: Multiple consumer groups = fan-out."""
    _header("DEMO 5: Multi-Group Fan-Out")
    print()
    print("  This shows the hybrid pattern: fan-out ACROSS groups,")
    print("  competing consumers WITHIN each group.")
    print()

    # Create 3 groups, each with different consumer counts
    groups_config = [
        ("kitchen", 2),
        ("billing", 1),
        ("analytics", 3),
    ]

    all_groups: list[tuple[str, ConsumerGroup, list[Consumer]]] = []
    for group_name, num_consumers in groups_config:
        _sub_header(f"Group '{group_name}' ({num_consumers} consumers)")
        group = ConsumerGroup(group_name, topic)
        consumers = []
        for i in range(num_consumers):
            c = group.add_consumer(f"{group_name}-{i+1}")
            consumers.append(c)
        all_groups.append((group_name, group, consumers))

    # Each group polls
    _sub_header("Each group reads ALL messages (fan-out across groups)")
    for group_name, group, consumers in all_groups:
        total_records = 0
        for consumer in consumers:
            records = consumer.poll(topic)
            total_records += len(records)
            partitions = consumer.assigned_partitions
            print(f"  [{group_name}] {consumer.id} "
                  f"(partitions {partitions}): {len(records)} records")
        print(f"  [{group_name}] TOTAL: {total_records} records")
        print()

    print("  RESULT: Each group consumed all 10 records (fan-out).")
    print("  Within each group, the work was split across consumers (competing).")
    print("  This is the standard Kafka production pattern.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print()
    print("  Kafka Simulation -- Pure Python")
    print("  =================================")
    print()
    print("  This simulation demonstrates Kafka's core concepts:")
    print("  partitions, consumer groups, offsets, rebalancing, and replay.")
    print("  No external dependencies -- everything is simulated in-process.")

    topic = demo_partitioned_topic()
    demo_consumer_groups(topic)
    demo_rebalancing(topic)
    demo_replay(topic)
    demo_multi_group_fanout(topic)

    _header("SUMMARY")
    print("""
  Kafka's key ideas, demonstrated above:

  1. PARTITIONED TOPICS: Messages are spread across partitions by key.
     Same key = same partition = guaranteed ordering for that entity.

  2. CONSUMER GROUPS: Within a group, partitions are assigned to consumers.
     One partition = one consumer (competing consumers for scaling).

  3. CROSS-GROUP FAN-OUT: Each consumer group independently reads all
     messages. Kitchen, billing, and analytics each get everything.

  4. REBALANCING: Adding/removing consumers triggers partition reassignment.
     Max parallelism = number of partitions. Extra consumers are idle.

  5. OFFSET-BASED REPLAY: Messages persist in the log. Consumers can
     reset their offset to re-read historical data at any time.

  These five concepts are what make Kafka fundamentally different from
  traditional message brokers like RabbitMQ or Redis.
""")


if __name__ == "__main__":
    main()

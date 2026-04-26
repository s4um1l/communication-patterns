# Chapter 07 -- Pub/Sub

## The Scene

FoodDash is scaling. Your `place_order` endpoint now calls 5 services in sequence: validate the order, create it in the database, charge the payment, notify the kitchen, match a driver, and send the customer a confirmation. Each call takes 50-200ms. Total: 500-1000ms before the customer sees "Order Confirmed."

```python
# This is what the order endpoint looks like today (simplified)
async def place_order(request):
    order = await validate_order(request)        # 50ms
    order = await save_to_db(order)              # 30ms
    receipt = await charge_payment(order)         # 200ms  <-- if billing is slow...
    await notify_kitchen(order)                   # 100ms  <-- ...kitchen waits
    driver = await match_driver(order)            # 150ms  <-- ...driver waits
    await send_confirmation(order, driver)        # 80ms   <-- ...customer waits
    await update_analytics(order)                 # 40ms
    return {"order_id": order.id, "status": "confirmed"}
    # Total: 650ms best case, 1000ms+ worst case
```

Worse: if the billing service is down, the entire order fails. The kitchen never hears about it. The driver is never matched. A single downstream failure cascades into a complete outage of order placement.

And it gets worse still. Every time you add a new service that cares about orders (loyalty points, fraud detection, inventory management), you have to modify the `place_order` function. The order service has become a god function that knows about every other service in the system. It is tightly coupled to all of them.

You need to decouple. The order service should do ONE thing: create the order and publish "order_placed". Everything else should react independently.

---

## The Pattern -- Pub/Sub

Pub/Sub (publish-subscribe) inverts the dependency. Instead of the order service calling each downstream service, it publishes an event to a **topic**. Services that care about that event **subscribe** to the topic. The **broker** routes messages from publishers to subscribers.

```
BEFORE (direct calls):
  Order Service ---> Kitchen Service
                ---> Billing Service
                ---> Driver Matching Service
                ---> Notification Service
                ---> Analytics Service

AFTER (pub/sub):
  Order Service ---> [BROKER: "order.placed"] ---> Kitchen Service
                                               ---> Billing Service
                                               ---> Driver Matching Service
                                               ---> Notification Service
                                               ---> Analytics Service
```

Three key properties:

1. **Publishers emit events to TOPICS, not to specific subscribers.** The order service says "an order was placed" -- it does not know or care who is listening. It could be 5 subscribers or 500.

2. **Subscribers register interest in topics.** The kitchen service says "I care about order.placed events" -- it does not know or care who publishes them.

3. **The BROKER routes messages.** It maintains the mapping of topics to subscribers and delivers messages. Publishers and subscribers never communicate directly.

This is fundamentally different from request-response. Request-response is a conversation: "Hey kitchen, here is an order, did you get it?" Pub/Sub is an announcement: "An order was placed." The publisher does not wait for a response. It is **fire-and-forget**.

```
Request-Response:
  Publisher: "Kitchen, process order #123"
  Kitchen:   "Got it, will be ready in 15 minutes"
  Publisher: "Great, now Billing, charge order #123"
  Billing:   "Payment processed, receipt #456"
  Publisher: "Now Driver Matching..."
  (sequential, coupled, slow)

Pub/Sub:
  Publisher: "ORDER PLACED: #123" --> [broker]
  (publisher is DONE, returns immediately)

  [broker] --> Kitchen:   "Processing order #123..."
  [broker] --> Billing:   "Charging payment..."
  [broker] --> Matching:  "Finding driver..."
  [broker] --> Analytics: "Recording order..."
  [broker] --> Notify:    "Sending confirmation..."
  (parallel, decoupled, fast)
```

### Topic Design

Topics form a hierarchy. You can subscribe to exact topics or use wildcards:

```
order.placed       -- a new order was created
order.confirmed    -- restaurant accepted the order
order.preparing    -- kitchen started cooking
order.ready        -- food is ready for pickup
order.cancelled    -- order was cancelled
order.*            -- ALL order events (wildcard)
```

Fine-grained topics (order.placed, order.confirmed) let subscribers filter precisely. The driver matching service only cares about `order.confirmed` (no point finding a driver if the restaurant has not accepted the order). The kitchen cares about `order.placed` (start preparing immediately).

Coarse topics with filtering (`order.*`) are simpler to manage but push filtering logic into the subscriber. The analytics service subscribes to `order.*` because it records every state transition.

The right granularity depends on your domain. Start fine-grained -- you can always add a wildcard subscriber later. Going the other direction (splitting a coarse topic) requires changing existing subscribers.

---

## Delivery Guarantees (the hard part)

The broker sits between publisher and subscriber. What happens when things go wrong? This is where pub/sub gets genuinely difficult.

### At-Most-Once

Fire and forget. The broker delivers the message once and does not track whether the subscriber received it. If the subscriber is down, the message is lost.

```
Publisher --> Broker --> Subscriber (received!)
Publisher --> Broker --> Subscriber (down) --> message LOST
```

This is like UDP. Fast, simple, no overhead. Acceptable for analytics events (missing one data point is fine) or heartbeats (the next one arrives in seconds). **Not acceptable for order processing** -- losing an order means a customer pays but never gets food.

### At-Least-Once

The broker retries until the subscriber acknowledges the message. If the subscriber crashes after processing but before acknowledging, the message is redelivered.

```
Publisher --> Broker --> Subscriber (processes, ACKs) --> done
Publisher --> Broker --> Subscriber (processes, crashes before ACK)
         --> Broker (no ACK, retries) --> Subscriber (processes AGAIN) --> ACKs
```

The message might be delivered twice (or more). The subscriber must be **idempotent** -- processing the same message multiple times must produce the same result as processing it once.

This is what most production systems use. It is the sweet spot: no lost messages, reasonable overhead. The cost is that subscribers must handle duplicates.

### Exactly-Once

The holy grail. Each message is delivered exactly once -- never lost, never duplicated. Sounds perfect, but achieving this requires **distributed transactions** between the broker and every subscriber. The broker and subscriber must atomically agree that the message was delivered and processed.

This is astronomically expensive. It requires two-phase commits, distributed consensus, and tight coupling between the broker and subscribers -- which defeats the purpose of pub/sub (decoupling). In practice, "exactly-once" systems either:
- Cheat by using at-least-once under the hood and deduplicating at the application level
- Have severe throughput limitations
- Only guarantee exactly-once within the broker itself (Kafka's exactly-once is between producers and the log, not between the log and consumers)

### FoodDash's Choice: At-Least-Once + Idempotent Handlers

FoodDash needs at-least-once delivery. It is okay to process an order twice (the kitchen sees a duplicate ticket and ignores it -- the order ID is the idempotency key). It is NOT okay to lose an order (the customer paid but nobody made the food).

```python
# Idempotent handler example
processed_orders = set()

async def handle_order_placed(event):
    if event["order_id"] in processed_orders:
        print(f"Duplicate: {event['order_id']} already processed, skipping")
        return
    processed_orders.add(event["order_id"])
    await prepare_food(event)
```

This is a fundamental principle: **at-least-once + idempotent handlers is strictly superior to exactly-once delivery**. It is cheaper, simpler, more resilient, and achieves the same business outcome. Design your handlers to be idempotent instead of demanding exactly-once from the broker.

---

## Systems Constraints Analysis

### CPU

**Broker does routing work** -- match events to subscriber patterns, manage queues, track acknowledgments.

For an **in-process broker** (like our educational implementation): negligible. Pattern matching on a few topics with a handful of subscribers is microseconds. The broker is just a dictionary lookup plus a loop over subscribers.

For a **distributed broker** (Redis, Kafka, RabbitMQ):
- **Serialization**: Each message must be serialized (JSON, Protobuf, Avro) before publishing and deserialized by each subscriber. At 1,000 events/second with 5 subscribers: 6,000 ser/deser operations per second.
- **Network overhead**: Each publish is a network round-trip to the broker. Each delivery is a network round-trip from the broker to the subscriber.
- **Pattern matching**: Wildcard topic matching (order.* against order.placed) on every publish. Simple string matching is fast; regex-based matching less so.

At FoodDash's scale (100 orders/minute = ~2 orders/second), CPU is not a concern for any broker. At 100K orders/second (stock exchange level), you need Kafka-level infrastructure.

### Memory

**Broker buffers undelivered messages.** If a subscriber is slow, messages queue up in the broker. Without backpressure: out-of-memory. With bounded queues: dropped messages.

Calculate for FoodDash:
```
100 orders/minute x 5 subscribers x 1KB/message = 500 KB/minute in transit
If a subscriber is 10 minutes behind: 5 MB buffered for that subscriber
If ALL subscribers are 10 minutes behind: 25 MB buffered total
```

This is fine for an in-process broker. For a distributed broker like Kafka, messages are written to disk, so memory pressure shifts to disk I/O.

The dangerous scenario: a subscriber goes down for an hour. At 500 KB/minute, that is 30 MB. For a day: 720 MB. This is why production brokers have message retention policies (Kafka defaults to 7 days, then deletes) and dead letter queues (messages that cannot be delivered after N retries go to a special queue for manual inspection).

### Network I/O

**Fan-out multiplies traffic.** One published message becomes N subscriber deliveries. For FoodDash with 5 subscribers: 5x network traffic.

```
Publisher sends 1 message (1 KB)
Broker delivers to 5 subscribers: 5 KB total
With headers/framing overhead: ~7-8 KB total network traffic

100 orders/minute = 100 KB published, 700-800 KB delivered
```

But here is the key insight: **this happens asynchronously, not in the request path.** The publisher's network cost is just one write to the broker. The fan-out happens on the broker's network budget, not the publisher's. The customer waiting for "Order Confirmed" only pays for the publisher-to-broker hop.

### Latency

The publisher's latency is just "write to broker":
- **In-process broker**: ~0.01ms (function call + queue put)
- **Local Redis**: ~1-5ms (network round-trip + serialization)
- **Remote Kafka**: ~5-20ms (network + batching + replication)

Subscriber processing happens entirely asynchronously. The customer gets "Order Confirmed" in 30-50ms (validate + save + publish). The kitchen gets the notification 200ms later. The driver gets matched 500ms later. But the customer does not wait for any of that.

**End-to-end latency** (time from publish to subscriber processing complete) depends on:
- Broker delivery time (queue depth, network)
- Subscriber processing time
- If a subscriber has a backlog of 1,000 messages, the new message waits behind all of them

### Bottleneck

**The broker is the single point of failure.** If the broker dies, no messages flow. Publishers cannot publish, subscribers cannot receive.

Solutions:
- **Replicated brokers**: Kafka uses leader-follower replication. If the leader dies, a follower takes over. Redis has Redis Sentinel for failover.
- **Multi-zone deployment**: Run broker instances in multiple availability zones.
- **Client-side buffering**: Publishers buffer messages locally if the broker is unreachable and retry when it comes back.

Also: **message ordering is only guaranteed within a partition** (Kafka) or per-channel (Redis). If you publish events A, B, C and they go to different partitions, subscribers might see B, A, C. If order matters (and for order status transitions it does), you must ensure all events for the same order go to the same partition (partition by order ID).

---

## Principal-Level Depth

### Dead Letter Queues

What happens when a message cannot be delivered? Maybe the subscriber is permanently down. Maybe the message is malformed and the subscriber throws an error every time. After N retry attempts, the broker moves the message to a **dead letter queue** (DLQ).

```
Broker --> Subscriber (fails)
Broker --> Subscriber (fails, retry 1)
Broker --> Subscriber (fails, retry 2)
Broker --> Subscriber (fails, retry 3)
Broker --> DLQ (for manual inspection)
```

The DLQ is a safety net. Without it, a single bad message blocks the subscriber's queue forever (the broker keeps retrying, never moves on). With a DLQ, bad messages are parked and the queue keeps flowing. An engineer can inspect the DLQ, fix the subscriber, and replay the messages.

### Poison Messages

A poison message is one that crashes the subscriber every time it is processed. Maybe it contains an unexpected null field. Maybe it triggers an unhandled edge case in the subscriber's code.

Without protection, the cycle is:
```
Broker delivers message --> Subscriber crashes
Broker retries --> Subscriber crashes
Broker retries --> Subscriber crashes
(infinite loop, subscriber is permanently down)
```

Defenses:
1. **Max retry count**: After N failures, move to DLQ (see above)
2. **Circuit breaker**: If a subscriber fails K times in a row, stop delivering to it temporarily. Let it recover.
3. **Defensive deserialization**: Validate the message schema before processing. Reject malformed messages immediately instead of crashing mid-processing.

### Ordering Guarantees

**Total ordering does not scale.** If you guarantee that every subscriber sees every message in exactly the order it was published, the broker becomes a bottleneck. It can only deliver one message at a time, globally, waiting for each subscriber to ACK before moving to the next.

**Partition-level ordering scales.** Kafka's approach: messages within a partition are strictly ordered. Messages across partitions have no ordering guarantee. You choose the partition key based on your domain:

```
Partition key: order_id
  --> All events for order #123 go to partition 7
  --> All events for order #456 go to partition 3
  --> Within partition 7: order.placed, order.confirmed, order.preparing (strict order)
  --> Across partitions: no ordering guarantee between #123 and #456
```

This is almost always what you want. You need events for the SAME order to be in order (you cannot confirm before placing). You do not need events for DIFFERENT orders to be in order (it does not matter if order #456 is confirmed before order #123).

### Fan-Out Patterns

Two distinct patterns, often confused:

**Fan-out (broadcast)**: Every subscriber gets every message. Five kitchen displays all show the same order. This is classic pub/sub -- the broker copies the message to each subscriber.

**Competing consumers**: Multiple instances of the SAME subscriber share the workload. Three kitchen workers subscribe, but each order goes to only ONE of them. This is a work queue, not pub/sub. Kafka achieves this with consumer groups -- within a group, each partition is assigned to one consumer.

```
Fan-out (pub/sub):
  [order.placed] --> Kitchen Display 1 (sees ALL orders)
                 --> Kitchen Display 2 (sees ALL orders)
                 --> Kitchen Display 3 (sees ALL orders)

Competing consumers (work queue):
  [order.placed] --> Kitchen Worker 1 (gets order #1, #4, #7...)
                 --> Kitchen Worker 2 (gets order #2, #5, #8...)
                 --> Kitchen Worker 3 (gets order #3, #6, #9...)
```

FoodDash uses both: fan-out to different services (kitchen, billing, driver matching all get the same event) and competing consumers within a service (three billing workers share the payment processing load).

### Backpressure Strategies

When a subscriber cannot keep up, messages pile up. Strategies:

1. **Bounded queues**: Set a maximum queue size. When full, either drop new messages (lossy) or block the publisher (apply backpressure upstream). Our educational broker uses bounded queues.

2. **Rate limiting**: The subscriber declares "I can handle 100 messages/second." The broker throttles delivery accordingly. Excess messages are buffered (up to a limit).

3. **Circuit breakers**: If a subscriber fails repeatedly, the broker stops delivering to it. After a cooldown period, it sends a test message. If that succeeds, normal delivery resumes.

4. **Adaptive batching**: Instead of delivering one message at a time, batch them. The subscriber processes 10 messages in one call. This reduces per-message overhead but increases latency for individual messages.

### In-Process vs External Broker

**In-process** (asyncio queues, like our implementation):
- Zero network overhead
- Messages lost if the process crashes
- Cannot fan-out across multiple server instances
- Perfect for: single-process applications, educational use, prototyping

**Redis Pub/Sub**:
- Cross-process, cross-machine fan-out
- No persistence -- if no subscriber is listening, the message vanishes
- Very fast (~1ms latency)
- Perfect for: real-time notifications where loss is acceptable

**Redis Streams**:
- Like Redis Pub/Sub but with persistence and consumer groups
- Messages survive restarts
- Supports competing consumers
- Perfect for: medium-scale work queues

**RabbitMQ**:
- Full-featured message broker with routing, exchanges, queues
- Supports complex routing patterns (topic exchange, fanout exchange, headers exchange)
- Push-based delivery to consumers
- Perfect for: enterprise integration, complex routing needs

**Apache Kafka**:
- Distributed commit log -- messages are written to disk and replicated
- Extremely high throughput (millions of messages/second)
- Messages retained for days/weeks (consumers can replay history)
- Partition-based ordering and consumer groups
- Perfect for: event sourcing, stream processing, high-scale systems

**Rule of thumb**: Start with in-process. When you need cross-process communication, use Redis Streams. When you need durability and replay, use Kafka. Do not start with Kafka -- its operational complexity is significant.

---

## Trade-offs at a Glance

| Dimension | Direct Calls | Pub/Sub | Message Queue | Event Sourcing |
|-----------|-------------|---------|---------------|----------------|
| **Coupling** | Tight -- caller knows all callees | Loose -- publisher knows only topics | Loose -- producer knows only queue | Loose -- producer writes events |
| **Latency (publisher)** | Sum of all calls (500-1000ms) | Write to broker (~1-5ms) | Write to queue (~1-5ms) | Write to log (~1-5ms) |
| **Latency (end-to-end)** | Same as publisher | Async, depends on queue depth | Async, depends on queue depth | Async, depends on consumers |
| **Failure isolation** | One failure blocks everything | Subscriber failures are independent | Consumer failures are independent | Consumer failures are independent |
| **Adding new consumers** | Modify publisher code | Just subscribe, no publisher change | Just consume, no producer change | Just read the log |
| **Message durability** | N/A (synchronous) | Depends on broker (Redis: no, Kafka: yes) | Yes (broker persists) | Yes (log is the source of truth) |
| **Ordering** | Guaranteed (sequential) | Per-partition only | Per-queue only | Total order in the log |
| **Replay** | Not possible | Depends on broker | Depends on broker | Always possible (that is the point) |
| **Complexity** | Low | Medium | Medium | High |
| **Best for** | Simple systems, <3 consumers | Event notification, fan-out | Work distribution, load leveling | Audit trails, temporal queries |

---

## Running the Code

### Run the publisher (demonstrates the full pub/sub cycle)

```bash
# From the repo root
uv run python -m chapters.ch07_pub_sub.publisher
```

This creates a broker, registers all subscribers, places an order, and shows:
- How fast the publisher returns (just the time to write to the broker)
- How all subscribers process in parallel
- The timing difference between sync calls and pub/sub
- What happens when a subscriber fails (error isolation)

---

## Bridge to Chapter 08

Pub/Sub decoupled our services beautifully. The order service publishes an event and returns in milliseconds. Five subscribers process independently, in parallel, with full error isolation. Adding a sixth subscriber requires zero changes to the publisher.

But notice something: several of our services are now **stateful**. The WebSocket chat server holds connection state. The SSE server holds subscriber queues. The pub/sub broker holds messages and subscription mappings. What happens when we need to scale horizontally -- when one server is not enough?

If we run two broker instances, which one holds the subscriptions? If a subscriber connects to broker A but the publisher publishes to broker B, the message never arrives. If we run three SSE servers behind a load balancer, a client's reconnection might hit a different server that has no record of their subscription.

This is where the stateful vs stateless tension becomes critical. Stateless services (REST APIs) scale trivially -- just add more instances behind a load balancer. Stateful services (brokers, WebSocket servers, SSE servers) require coordination: shared state, sticky sessions, or distributed consensus.

The next chapter tackles this head-on: [Chapter 08 -- Stateful vs Stateless](../ch08_stateful_vs_stateless/).

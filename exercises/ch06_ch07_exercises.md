# Exercises: Ch06 Push Notifications & Ch07 Pub/Sub

---

## Exercise 1 -- Push Notification Delivery Guarantees [Beginner]

**Question:** FoodDash sends a push notification: "Your order is ready for pickup!" via Apple Push Notification Service (APNs) and Firebase Cloud Messaging (FCM).

1. Are push notifications guaranteed to be delivered? List three reasons a notification might never reach the user.
2. The user's phone is in airplane mode for 2 hours. What happens to notifications sent during that time on iOS vs Android?
3. FoodDash accidentally sends 50 duplicate "order ready" notifications in 1 second (a bug). How do APNs and FCM handle this? What is a `collapse_id` / `collapse_key`?

<details>
<summary>Solution</summary>

**1. Push notifications are NOT guaranteed.** Reasons for non-delivery:
- **Device offline**: No network connection. The push service may store-and-forward, but with limits.
- **Token expired/invalid**: User uninstalled the app or revoked permissions. The push service returns an error, but you might not handle it.
- **User disabled notifications**: OS-level toggle. The push service may accept the message but the OS suppresses it.
- **Rate limiting**: APNs and FCM throttle apps that send too many notifications. Excess messages are silently dropped.
- **Payload too large**: APNs max 4KB, FCM max 4KB. Oversized messages are rejected.

**2. Airplane mode behavior:**
- **APNs (iOS)**: Stores the *most recent* notification per `collapse_id` (or per app if no collapse_id). When the device comes back online, it delivers only the latest one. 50 notifications during airplane mode = 1 delivered.
- **FCM (Android)**: Stores messages for up to 4 weeks (configurable via `time_to_live`). If multiple messages share the same `collapse_key`, only the last is delivered. Without `collapse_key`, all non-expired messages are delivered in a burst when the device reconnects.

**3. 50 duplicate notifications:**
- Without `collapse_id`/`collapse_key`: The user gets 50 notifications. Terrible UX.
- With `collapse_id`/`collapse_key`: Both APNs and FCM replace the previous notification with the same key. The user sees only 1 notification (the latest). Always set a collapse key for notifications that supersede each other.
- FoodDash should use `collapse_key: "order_ready_{order_id}"` so that per-order notifications collapse but different orders don't interfere.

</details>

---

## Exercise 2 -- Pub/Sub Topic Design [Beginner]

**Question:** FoodDash's order lifecycle generates these events:

```
order.created, order.paid, order.sent_to_kitchen, order.cooking,
order.ready, order.picked_up, order.delivered, order.cancelled, order.refunded
```

These services need to consume events:
- **Kitchen display**: needs `order.sent_to_kitchen`, `order.cancelled`
- **Driver matching**: needs `order.ready`
- **Billing**: needs `order.paid`, `order.cancelled`, `order.refunded`
- **Analytics**: needs ALL events
- **Customer notification**: needs `order.cooking`, `order.ready`, `order.picked_up`, `order.delivered`

1. Design the topic structure. Do you use one topic (`orders`) or multiple (`order.created`, `order.paid`, etc.)?
2. How do consumers filter for only the events they need in each approach?
3. What are the trade-offs of few-big-topics vs many-small-topics?

<details>
<summary>Solution</summary>

**Option A: Single topic `orders` with event type in the message:**
```
Topic: orders
Message: {"event": "order.created", "order_id": "abc", ...}
```
- Consumers subscribe to `orders` and filter client-side.
- Analytics is easy: consume everything.
- Kitchen display ignores 7/9 event types. Wasteful at scale.

**Option B: One topic per event type:**
```
Topics: order.created, order.paid, order.sent_to_kitchen, ...
```
- Kitchen subscribes to: `order.sent_to_kitchen`, `order.cancelled`
- No client-side filtering needed.
- Analytics must subscribe to 9 topics.
- Adding a new event type means creating a new topic and updating all relevant subscribers.

**Option C: Hierarchical topics with wildcard subscriptions (best for most brokers):**
```
Topic: orders.{event_type}
Examples: orders.created, orders.paid, orders.cooking
```
- Kitchen subscribes to: `orders.sent_to_kitchen`, `orders.cancelled`
- Analytics subscribes to: `orders.*` (wildcard)
- Customer notification subscribes to: `orders.cooking`, `orders.ready`, `orders.picked_up`, `orders.delivered`

**Trade-offs:**

| | Few Big Topics | Many Small Topics |
|---|---|---|
| **Filtering** | Client-side (wasteful bandwidth) | Broker-side (efficient) |
| **Topic management** | Simple (1 topic) | Complex (N topics to create/manage) |
| **Ordering** | Guaranteed within single topic | No ordering across topics |
| **Flexibility** | Easy to add new event types | Need new topic + subscriber updates |
| **Fan-out cost** | One delivery path | Broker manages N subscription trees |

**Recommendation for FoodDash:** Option C with a broker that supports wildcard subscriptions (RabbitMQ topic exchange, NATS subjects, Kafka with multiple consumer groups). Single-topic is fine under 10K events/sec. Many-topics becomes necessary at scale to reduce wasted bandwidth.

</details>

---

## Exercise 3 -- Guaranteed Delivery with Pub/Sub [Intermediate]

**Coding Challenge:** FoodDash publishes an `order.paid` event to the pub/sub broker. The billing service must process it to charge the customer's card. But:

1. The billing service is down for maintenance when the event is published. What happens to the event?
2. The billing service receives the event, starts processing, and crashes mid-way through charging the card. The card was charged, but the service didn't acknowledge the message. What happens?
3. Design and implement a solution using an **outbox pattern** + **idempotent consumer**. Show the database schema and the code.

<details>
<summary>Hint</summary>

The outbox pattern: instead of publishing directly to the broker, write the event to an "outbox" table in the same database transaction as the business operation. A separate process reads the outbox and publishes to the broker. The consumer uses a "processed_events" table to detect duplicates.

</details>

<details>
<summary>Solution</summary>

**1. Service down:** Depends on the broker. With a durable queue (RabbitMQ with persistent messages, Kafka with retention), the message waits until the consumer comes back. With an ephemeral broker (Redis Pub/Sub), the message is lost forever.

**2. Crash after partial processing:** The broker redelivers the unacknowledged message. The billing service processes it again. The customer is charged twice. This is the classic **at-least-once** delivery problem.

**3. Outbox + idempotent consumer:**

**Database schema:**

```sql
-- On the ORDER service side (publisher)
CREATE TABLE outbox (
    id          BIGSERIAL PRIMARY KEY,
    event_type  TEXT NOT NULL,
    payload     JSONB NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    published   BOOLEAN DEFAULT FALSE
);

-- On the BILLING service side (consumer)
CREATE TABLE processed_events (
    event_id    TEXT PRIMARY KEY,          -- idempotency key
    processed_at TIMESTAMPTZ DEFAULT NOW()
);
```

**Publisher (order service):**

```python
async def place_order(order: Order, db: AsyncSession):
    # Business logic + outbox write in ONE transaction
    async with db.begin():
        db.add(order)
        db.add(OutboxEvent(
            event_type="order.paid",
            payload={"order_id": order.id, "amount": order.total,
                     "idempotency_key": f"order_paid_{order.id}"}
        ))
    # Transaction committed atomically -- either both or neither

# Separate background task polls the outbox
async def outbox_publisher():
    while True:
        async with db.begin():
            events = await db.execute(
                select(OutboxEvent)
                .where(OutboxEvent.published == False)
                .order_by(OutboxEvent.id)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
            for event in events.scalars():
                await broker.publish(event.event_type, event.payload)
                event.published = True
        await asyncio.sleep(0.5)
```

**Consumer (billing service):**

```python
async def handle_order_paid(message: dict):
    idempotency_key = message["idempotency_key"]

    async with db.begin():
        # Check if already processed
        existing = await db.get(ProcessedEvent, idempotency_key)
        if existing:
            print(f"Duplicate event {idempotency_key}, skipping")
            return  # ACK the message, don't reprocess

        # Process: charge the card
        await payment_gateway.charge(
            order_id=message["order_id"],
            amount=message["amount"],
            idempotency_key=idempotency_key  # Gateway also deduplicates!
        )

        # Record as processed + ACK in same transaction
        db.add(ProcessedEvent(event_id=idempotency_key))

    await broker.ack(message)
```

**Why this works:**
- **Publisher side**: The outbox write is in the same transaction as the business operation. If the transaction fails, neither the order nor the event is persisted. No "event published but order not created" inconsistency.
- **Consumer side**: The `processed_events` table prevents double-processing. Even if the broker redelivers, the consumer detects the duplicate.
- **Payment gateway**: The `idempotency_key` is forwarded to Stripe/PayPal, which also deduplicates. Belt and suspenders.

</details>

---

## Exercise 4 -- Fan-Out Performance [Intermediate]

**Question:** FoodDash sends a push notification when a new restaurant joins the platform. There are 500,000 registered users. The notification service receives one pub/sub event (`restaurant.onboarded`) and must fan out to 500K push tokens.

1. If each APNs HTTP/2 request takes 50ms (including network round trip), how long does sequential fan-out take?
2. With 100 concurrent HTTP/2 connections to APNs, each multiplexing 100 streams, how long does it take?
3. At what scale does fan-out become the bottleneck? Design a system that can notify 10M users within 60 seconds.

<details>
<summary>Solution</summary>

**1. Sequential:** `500,000 * 50ms = 25,000 seconds = ~7 hours`. Obviously unacceptable.

**2. 100 connections * 100 streams = 10,000 concurrent requests:**
`500,000 / 10,000 = 50 batches * 50ms = 2.5 seconds`. This is reasonable for 500K users.

**3. Scaling to 10M users in 60 seconds:**

Target throughput: `10,000,000 / 60s = 166,667 notifications/second`.

At 50ms per request, you need `166,667 * 0.05 = 8,333` concurrent requests in flight.

**Architecture:**
```
Pub/Sub event: restaurant.onboarded
        |
        v
  Fan-out service (reads user segments from DB)
        |
        v
  SQS/Kafka queue (partitioned by user_id hash)
   /    |    |    \
  v     v    v     v
Worker Worker Worker Worker  (N workers, auto-scaling)
  |     |    |     |
  v     v    v     v
APNs / FCM  (HTTP/2 multiplexed)
```

- **Segmentation**: Don't send to all 10M. Segment by location, preferences. "New pizza place in Brooklyn" goes to Brooklyn users only.
- **Batching**: FCM supports multicast to 500 tokens per request. 10M / 500 = 20K requests.
- **Rate limiting**: APNs/FCM have per-app rate limits. Spread across multiple provider connections.
- **Token hygiene**: Prune invalid tokens (APNs returns 410 for unregistered). 10-20% of tokens are stale.
- **Priority queue**: Partition by user engagement. Active users first.

</details>

---

## Exercise 5 -- Design an Event-Driven Order Pipeline [Principal]

**Design Problem:** Redesign FoodDash's order flow using pub/sub, replacing the synchronous chain from Ch07's introduction:

Current (synchronous):
```
place_order -> validate -> create_in_db -> charge_payment ->
notify_kitchen -> match_driver -> send_confirmation
Total: 500-1000ms
```

Requirements:
- Customer must see "Order Confirmed" in < 200ms
- Payment failure after confirmation must trigger a cancellation flow
- Kitchen, driver, and notification services are maintained by different teams with different deploy schedules
- At peak, 1,000 orders/minute
- An event must never be lost (even if a service is down for hours)
- You must handle: out-of-order events, duplicate events, poison pill messages (events that always fail processing)

Design the following:
1. The topic/queue structure
2. The event schema (include what fields enable idempotency and ordering)
3. The happy path sequence diagram
4. The payment failure path
5. How you handle a "poison pill" message that crashes the billing service every time

<details>
<summary>Solution</summary>

**1. Topic/queue structure:**

```
Topics (durable, partitioned by order_id):
  order.events         -- all order lifecycle events (single topic for ordering)

Consumer groups (each reads from order.events):
  billing-service      -- reacts to order.created
  kitchen-service      -- reacts to order.payment_confirmed
  driver-service       -- reacts to order.kitchen_accepted
  notification-service -- reacts to order.*, sends appropriate notifications
  analytics-service    -- reacts to order.*, writes to data warehouse

Dead letter queues:
  order.events.dlq.billing
  order.events.dlq.kitchen
  order.events.dlq.driver
```

**2. Event schema:**

```json
{
  "event_id": "evt_a1b2c3",
  "event_type": "order.created",
  "order_id": "ord_xyz",
  "version": 3,
  "timestamp": "2024-01-15T10:30:00Z",
  "idempotency_key": "order_created_ord_xyz_v3",
  "correlation_id": "req_abc123",
  "payload": {
    "items": [{"id": "burger", "qty": 1}],
    "customer_id": "cust_42",
    "total": 1299
  }
}
```

- `event_id`: globally unique, for deduplication
- `order_id`: partition key, ensures all events for one order go to the same partition (preserving order)
- `version`: monotonically increasing per order, for ordering and optimistic concurrency
- `idempotency_key`: derived from event_type + order_id + version, for consumer-side dedup

**3. Happy path:**

```
Customer -> API Gateway -> Order Service
  |  (synchronous, <200ms)
  |  1. Validate (in-process, 5ms)
  |  2. Write to DB + Outbox (single transaction, 20ms)
  |  3. Return "Order Confirmed" with order_id
  |
  +-- Outbox relay publishes: order.created
        |
        +-> Billing Service
        |     Charge card (2s)
        |     Publish: order.payment_confirmed
        |       |
        |       +-> Kitchen Service
        |       |     Accept order (100ms)
        |       |     Publish: order.kitchen_accepted
        |       |       |
        |       |       +-> Driver Service
        |       |             Match driver (5s)
        |       |             Publish: order.driver_matched
        |       |
        |       +-> Notification: "Payment confirmed"
        |
        +-> Notification: "Order received"
        +-> Analytics: log event
```

Customer sees confirmation in ~25ms. Payment happens async.

**4. Payment failure path:**

```
Billing Service receives order.created
  -> Charge card fails (insufficient funds)
  -> Publish: order.payment_failed
      |
      +-> Order Service
      |     Update order status to "cancelled"
      |     Publish: order.cancelled
      |       |
      |       +-> Kitchen Service: remove from queue
      |       +-> Notification: "Payment failed, order cancelled"
      |       +-> Customer: refund if partial charge
```

**Key design decision**: The API confirmed the order *before* payment. This is a deliberate trade-off:
- Pro: < 200ms response time
- Con: Must handle post-confirmation cancellation
- This is standard in food delivery (Uber Eats, DoorDash confirm immediately)

**5. Poison pill handling:**

```python
MAX_RETRIES = 3

async def process_event(event: dict):
    retry_count = event.get("_retry_count", 0)

    try:
        await handle_event(event)
        await broker.ack(event)
    except Exception as e:
        if retry_count >= MAX_RETRIES:
            # Poison pill -- move to dead letter queue
            await dlq.publish({
                **event,
                "_error": str(e),
                "_failed_at": datetime.utcnow().isoformat(),
                "_retry_count": retry_count,
            })
            await broker.ack(event)  # ACK to remove from main queue
            await alert_ops(f"Poison pill: {event['event_id']}")
        else:
            # Retry with backoff
            event["_retry_count"] = retry_count + 1
            await broker.nack(event, delay=2 ** retry_count)  # 1s, 2s, 4s
```

The DLQ allows:
- Ops team to inspect failed events
- Manual replay after the bug is fixed
- Automatic replay via a DLQ consumer after a configurable delay
- The main queue is never blocked by a single bad event

</details>

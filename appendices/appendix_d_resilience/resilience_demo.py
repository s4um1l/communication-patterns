"""Appendix D — Resilience Patterns Demo (Retry, Circuit Breaker, Backoff).

Working implementations of resilience patterns applied to FoodDash.
No external dependencies — pure Python standard library + shared models.

Demonstrates:
1. Exponential backoff with jitter (full, equal, decorrelated)
2. Circuit breaker with state machine
3. Retry amplification and retry budgets
4. Idempotency keys preventing duplicate orders

Run with:
    uv run python -m appendices.appendix_d_resilience.resilience_demo
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import random
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# ── Ensure repo root is on sys.path so we can import shared models ──
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.models import (
    Customer,
    MenuItem,
    Order,
    OrderItem,
    OrderStatus,
)


# ═══════════════════════════════════════════════════════════════════════
# Part 1: Exponential Backoff with Jitter
# ═══════════════════════════════════════════════════════════════════════


class JitterStrategy(str, enum.Enum):
    """Jitter strategies for exponential backoff."""
    NONE = "none"          # Pure exponential: base * 2^attempt
    FULL = "full"          # random(0, base * 2^attempt)  — AWS recommended
    EQUAL = "equal"        # base * 2^attempt / 2 + random(0, base * 2^attempt / 2)
    DECORRELATED = "decorrelated"  # random(base, prev_delay * 3)


@dataclass
class ExponentialBackoff:
    """Configurable exponential backoff with jitter.

    This is a production-quality implementation. The math:
      base_delay = base * 2^attempt  (capped at max_delay)
      final_delay = jitter_fn(base_delay)

    Usage:
        backoff = ExponentialBackoff(base=1.0, max_delay=60.0, strategy=JitterStrategy.FULL)
        for attempt in range(max_retries):
            delay = backoff.delay(attempt)
            await asyncio.sleep(delay)
    """
    base: float = 1.0           # Base delay in seconds
    max_delay: float = 60.0     # Maximum delay cap
    strategy: JitterStrategy = JitterStrategy.FULL
    _prev_delay: float = 0.0    # For decorrelated jitter

    def delay(self, attempt: int) -> float:
        """Calculate the delay for a given attempt number (0-indexed)."""
        base_delay = min(self.base * (2 ** attempt), self.max_delay)

        if self.strategy == JitterStrategy.NONE:
            return base_delay

        if self.strategy == JitterStrategy.FULL:
            # random(0, base_delay) — maximum spread
            return random.uniform(0, base_delay)

        if self.strategy == JitterStrategy.EQUAL:
            # half + random(0, half) — guaranteed minimum wait
            half = base_delay / 2
            return half + random.uniform(0, half)

        if self.strategy == JitterStrategy.DECORRELATED:
            # random(base, prev_delay * 3) — adapts to history
            delay = random.uniform(self.base, max(self.base, self._prev_delay * 3))
            delay = min(delay, self.max_delay)
            self._prev_delay = delay
            return delay

        return base_delay

    def reset(self) -> None:
        """Reset state (for decorrelated jitter)."""
        self._prev_delay = 0.0


def demo_backoff_strategies() -> None:
    """Show all jitter strategies side by side for 6 retry attempts."""
    print("=" * 72)
    print("  PART 1: Exponential Backoff — Jitter Strategies Compared")
    print("=" * 72)
    print()

    max_attempts = 6
    random.seed(42)  # Reproducible for demo

    strategies = [
        JitterStrategy.NONE,
        JitterStrategy.FULL,
        JitterStrategy.EQUAL,
        JitterStrategy.DECORRELATED,
    ]

    # Show delay values for each strategy
    print(f"  {'Attempt':<10}", end="")
    for s in strategies:
        print(f"{s.value:>15}", end="")
    print()
    print(f"  {'-------':<10}", end="")
    for _ in strategies:
        print(f"{'--------':>15}", end="")
    print()

    for attempt in range(max_attempts):
        print(f"  {attempt:<10}", end="")
        for s in strategies:
            backoff = ExponentialBackoff(base=1.0, max_delay=60.0, strategy=s)
            # For decorrelated, simulate the chain
            if s == JitterStrategy.DECORRELATED:
                for a in range(attempt + 1):
                    d = backoff.delay(a)
                delay = d
            else:
                delay = backoff.delay(attempt)
            print(f"{delay:>14.2f}s", end="")
        print()

    print()
    print("  Full jitter has the widest spread → best load distribution")
    print("  Equal jitter guarantees minimum wait → predictable minimum")
    print("  Decorrelated adapts to history → self-adjusting")
    print()


def demo_thundering_herd() -> None:
    """Visualize 10 clients retrying with and without jitter."""
    print("=" * 72)
    print("  PART 2: Thundering Herd — 10 Clients Retrying")
    print("=" * 72)
    print()

    num_clients = 10
    max_attempts = 5
    timeline_width = 60

    random.seed(123)

    # WITHOUT jitter — all clients retry at the same times
    print("  WITHOUT jitter (synchronized retries):")
    print()
    backoff_no_jitter = ExponentialBackoff(base=1.0, strategy=JitterStrategy.NONE)
    retry_times_no_jitter: list[float] = [0.0]  # Initial failure at t=0
    for attempt in range(max_attempts):
        retry_times_no_jitter.append(
            retry_times_no_jitter[-1] + backoff_no_jitter.delay(attempt)
        )
    max_time = retry_times_no_jitter[-1]

    for client_id in range(num_clients):
        label = f"  Client {client_id:2d}: "
        bar = [" "] * timeline_width
        for t in retry_times_no_jitter:
            pos = int(t / max_time * (timeline_width - 1))
            pos = min(pos, timeline_width - 1)
            bar[pos] = "X"
        print(f"{label}|{''.join(bar)}|")

    # Show load per time slot
    print()
    load = [0] * timeline_width
    for t in retry_times_no_jitter:
        pos = int(t / max_time * (timeline_width - 1))
        pos = min(pos, timeline_width - 1)
        load[pos] = num_clients
    load_bar = ""
    for l in load:
        if l == 0:
            load_bar += " "
        elif l <= 3:
            load_bar += "."
        elif l <= 6:
            load_bar += "o"
        else:
            load_bar += "#"
    print(f"  Server:    |{load_bar}|")
    print(f"  {'':13s} # = {num_clients} simultaneous requests (SPIKE)")
    print()

    # WITH full jitter — clients spread out
    print("  WITH full jitter (randomized retries):")
    print()
    all_retry_times: list[list[float]] = []
    for client_id in range(num_clients):
        backoff_jitter = ExponentialBackoff(base=1.0, strategy=JitterStrategy.FULL)
        times = [0.0]
        for attempt in range(max_attempts):
            times.append(times[-1] + backoff_jitter.delay(attempt))
        all_retry_times.append(times)

    # Normalize to same max_time for comparison
    for client_id in range(num_clients):
        label = f"  Client {client_id:2d}: "
        bar = [" "] * timeline_width
        for t in all_retry_times[client_id]:
            pos = int(t / max_time * (timeline_width - 1))
            pos = max(0, min(pos, timeline_width - 1))
            bar[pos] = "x"
        print(f"{label}|{''.join(bar)}|")

    # Show load per time slot with jitter
    print()
    load_jitter = [0] * timeline_width
    for times in all_retry_times:
        for t in times:
            pos = int(t / max_time * (timeline_width - 1))
            pos = max(0, min(pos, timeline_width - 1))
            load_jitter[pos] += 1
    load_bar_jitter = ""
    for l in load_jitter:
        if l == 0:
            load_bar_jitter += " "
        elif l <= 2:
            load_bar_jitter += "."
        elif l <= 4:
            load_bar_jitter += "o"
        else:
            load_bar_jitter += "#"
    print(f"  Server:    |{load_bar_jitter}|")
    max_jitter_load = max(load_jitter)
    print(f"  {'':13s} Max simultaneous: {max_jitter_load} (vs {num_clients} without jitter)")
    print()


# ═══════════════════════════════════════════════════════════════════════
# Part 2: Circuit Breaker
# ═══════════════════════════════════════════════════════════════════════


class CircuitState(str, enum.Enum):
    CLOSED = "CLOSED"        # Normal — requests pass through
    OPEN = "OPEN"            # Failing — reject immediately
    HALF_OPEN = "HALF_OPEN"  # Testing — allow one request


@dataclass
class CircuitBreaker:
    """Circuit breaker with configurable thresholds.

    State machine:
      CLOSED → OPEN:     when failure_count >= failure_threshold within window
      OPEN → HALF_OPEN:  when recovery_timeout has elapsed
      HALF_OPEN → CLOSED: when a test request succeeds
      HALF_OPEN → OPEN:  when a test request fails

    Usage:
        cb = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)

        if cb.allow_request():
            try:
                result = call_downstream_service()
                cb.record_success()
            except Exception:
                cb.record_failure()
        else:
            # Circuit is OPEN — use fallback
            result = fallback_response()
    """
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    window_size: float = 60.0       # Sliding window for failure counting

    # Internal state
    state: CircuitState = field(default=CircuitState.CLOSED)
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: float = 0.0
    opened_at: float = 0.0
    failure_times: list[float] = field(default_factory=list)

    # Metrics
    total_requests: int = 0
    total_allowed: int = 0
    total_rejected: int = 0
    total_successes: int = 0
    total_failures: int = 0

    def allow_request(self) -> bool:
        """Check if a request should be allowed through."""
        self.total_requests += 1
        now = time.time()

        if self.state == CircuitState.CLOSED:
            self.total_allowed += 1
            return True

        if self.state == CircuitState.OPEN:
            # Check if recovery timeout has elapsed
            if now - self.opened_at >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                self.total_allowed += 1
                return True
            self.total_rejected += 1
            return False

        if self.state == CircuitState.HALF_OPEN:
            # Only allow one test request in half-open
            self.total_rejected += 1
            return False

        return False

    def record_success(self) -> None:
        """Record a successful request."""
        self.total_successes += 1

        if self.state == CircuitState.HALF_OPEN:
            # Recovery confirmed — close the circuit
            self.state = CircuitState.CLOSED
            self.failure_count = 0
            self.failure_times.clear()
            self.success_count = 0

        self.success_count += 1

    def record_failure(self) -> None:
        """Record a failed request."""
        now = time.time()
        self.total_failures += 1
        self.last_failure_time = now
        self.failure_times.append(now)

        if self.state == CircuitState.HALF_OPEN:
            # Recovery failed — reopen the circuit
            self.state = CircuitState.OPEN
            self.opened_at = now
            return

        # Prune failures outside the window
        cutoff = now - self.window_size
        self.failure_times = [t for t in self.failure_times if t > cutoff]
        self.failure_count = len(self.failure_times)

        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            self.opened_at = now

    def get_state_display(self) -> str:
        """Return current state with color-like indicators."""
        if self.state == CircuitState.CLOSED:
            return f"[CLOSED]  (normal — {self.failure_count}/{self.failure_threshold} failures)"
        if self.state == CircuitState.OPEN:
            elapsed = time.time() - self.opened_at
            remaining = max(0, self.recovery_timeout - elapsed)
            return f"[OPEN]    (rejecting — recovery in {remaining:.1f}s)"
        return f"[HALF-OPEN] (testing recovery)"


def demo_circuit_breaker() -> None:
    """Demonstrate circuit breaker protecting against cascading failure."""
    print("=" * 72)
    print("  PART 3: Circuit Breaker — Protecting Against Cascading Failure")
    print("=" * 72)
    print()

    # Simulate a downstream service that fails, then recovers
    class DownstreamService:
        def __init__(self):
            self.healthy = True
            self.call_count = 0

        def call(self) -> str:
            self.call_count += 1
            if not self.healthy:
                raise ConnectionError("Payment service unavailable")
            return "payment_confirmed"

    service = DownstreamService()
    cb = CircuitBreaker(
        failure_threshold=3,
        recovery_timeout=0.5,  # Short for demo
        window_size=10.0,
    )

    events: list[str] = []

    def make_request(request_num: int) -> None:
        if cb.allow_request():
            try:
                result = service.call()
                cb.record_success()
                events.append(f"  Request {request_num:2d}: ALLOWED  -> SUCCESS  {cb.get_state_display()}")
            except ConnectionError:
                cb.record_failure()
                events.append(f"  Request {request_num:2d}: ALLOWED  -> FAILURE  {cb.get_state_display()}")
        else:
            events.append(f"  Request {request_num:2d}: REJECTED (fast fail)  {cb.get_state_display()}")

    # Phase 1: Service is healthy
    print("  Phase 1: Payment service is HEALTHY")
    for i in range(1, 4):
        make_request(i)
    for e in events:
        print(e)
    events.clear()
    print()

    # Phase 2: Service goes down
    print("  Phase 2: Payment service goes DOWN")
    service.healthy = False
    for i in range(4, 12):
        make_request(i)
    for e in events:
        print(e)
    events.clear()
    print()

    # Phase 3: Wait for recovery timeout, then test
    print(f"  Phase 3: Waiting {cb.recovery_timeout}s for recovery timeout...")
    time.sleep(cb.recovery_timeout + 0.1)
    print()

    # Service is still down — half-open test will fail
    print("  Phase 3a: Service still down — half-open test fails")
    for i in range(12, 15):
        make_request(i)
    for e in events:
        print(e)
    events.clear()
    print()

    # Phase 4: Service recovers
    print(f"  Phase 4: Waiting {cb.recovery_timeout}s, then service RECOVERS")
    time.sleep(cb.recovery_timeout + 0.1)
    service.healthy = True
    for i in range(15, 20):
        make_request(i)
    for e in events:
        print(e)
    events.clear()
    print()

    # Summary
    print("  Summary:")
    print(f"    Total requests:  {cb.total_requests}")
    print(f"    Allowed through: {cb.total_allowed}")
    print(f"    Fast-rejected:   {cb.total_rejected} (saved {cb.total_rejected} slow timeouts)")
    print(f"    Successes:       {cb.total_successes}")
    print(f"    Failures:        {cb.total_failures}")
    print()
    print("  Key insight: requests 7-11 and 13-14 were rejected in ~0ms")
    print("  instead of waiting 30s for a timeout. Those threads stayed free")
    print("  to serve other requests.")
    print()


# ═══════════════════════════════════════════════════════════════════════
# Part 3: Retry Budget
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class RetryBudget:
    """Token-bucket retry budget limiting total retry percentage.

    Limits retries to a percentage of total traffic over a sliding window.
    This prevents retry amplification in service chains.

    Usage:
        budget = RetryBudget(budget_percent=10.0, window_seconds=10.0)

        # For each request:
        budget.record_request()

        # When a request fails and you want to retry:
        if budget.allow_retry():
            budget.record_retry()
            retry_the_request()
        else:
            # Budget exhausted — don't retry
            return_error_to_caller()
    """
    budget_percent: float = 10.0     # Max % of traffic that can be retries
    window_seconds: float = 10.0     # Sliding window
    min_retries: int = 3             # Always allow at least N concurrent retries

    # Tracking
    _request_times: list[float] = field(default_factory=list)
    _retry_times: list[float] = field(default_factory=list)

    # Metrics
    retries_allowed: int = 0
    retries_denied: int = 0

    def record_request(self) -> None:
        """Record an original (non-retry) request."""
        self._request_times.append(time.time())

    def record_retry(self) -> None:
        """Record that a retry was sent."""
        self._retry_times.append(time.time())
        self.retries_allowed += 1

    def allow_retry(self) -> bool:
        """Check if a retry is allowed under the budget."""
        now = time.time()
        cutoff = now - self.window_seconds

        # Count requests and retries in the window
        requests_in_window = sum(1 for t in self._request_times if t > cutoff)
        retries_in_window = sum(1 for t in self._retry_times if t > cutoff)

        # Always allow minimum retries
        if retries_in_window < self.min_retries:
            return True

        # Check budget percentage
        total_in_window = requests_in_window + retries_in_window
        if total_in_window == 0:
            return True

        retry_percent = (retries_in_window / total_in_window) * 100
        if retry_percent < self.budget_percent:
            return True

        self.retries_denied += 1
        return False

    def prune(self) -> None:
        """Remove expired entries to prevent memory growth."""
        cutoff = time.time() - self.window_seconds
        self._request_times = [t for t in self._request_times if t > cutoff]
        self._retry_times = [t for t in self._retry_times if t > cutoff]


def demo_retry_amplification() -> None:
    """Show retry amplification in a 5-service chain, then fix it with a budget."""
    print("=" * 72)
    print("  PART 4: Retry Amplification — 5-Service Chain")
    print("=" * 72)
    print()

    # ── Without retry budget ──
    print("  WITHOUT retry budget:")
    print("  Chain: A -> B -> C -> D -> E (E is down)")
    print()

    # Simulate the chain: each service retries 3 times
    retries_per_hop = 3
    chain_depth = 5

    def simulate_chain_no_budget(depth: int, counters: dict[str, int]) -> bool:
        """Simulate a service chain where every hop retries 3 times."""
        service_name = chr(ord('A') + (chain_depth - depth))
        counters[service_name] = counters.get(service_name, 0) + 1

        if depth == 1:
            # Service E — always fails
            return False

        # Try calling the next service, with retries
        for attempt in range(retries_per_hop):
            success = simulate_chain_no_budget(depth - 1, counters)
            if success:
                return True
        return False

    counters_no_budget: dict[str, int] = {}
    simulate_chain_no_budget(chain_depth, counters_no_budget)

    total_no_budget = sum(counters_no_budget.values())
    print(f"  Requests per service:")
    for svc in ['A', 'B', 'C', 'D', 'E']:
        count = counters_no_budget.get(svc, 0)
        bar = "#" * min(count, 60)
        if count > 60:
            bar += f"... ({count})"
        print(f"    Service {svc}: {count:>4d} requests  {bar}")

    print()
    print(f"  Total requests in system: {total_no_budget}")
    print(f"  Amplification factor: {total_no_budget}x from 1 original request")
    print(f"  Math: 3^0 + 3^1 + 3^2 + 3^3 + 3^4 = 1 + 3 + 9 + 27 + 81 = {1+3+9+27+81}")
    print()

    # ── With retry budget ──
    print("  WITH retry budget (max 20% retries per service):")
    print()

    # Per-service retry budgets
    budgets: dict[str, RetryBudget] = {
        chr(ord('A') + i): RetryBudget(budget_percent=20.0, window_seconds=60.0, min_retries=1)
        for i in range(chain_depth)
    }

    def simulate_chain_with_budget(depth: int, counters: dict[str, int]) -> bool:
        """Simulate a service chain where each hop has a retry budget."""
        service_name = chr(ord('A') + (chain_depth - depth))
        counters[service_name] = counters.get(service_name, 0) + 1
        budget = budgets[service_name]
        budget.record_request()

        if depth == 1:
            return False

        # First attempt (not a retry)
        success = simulate_chain_with_budget(depth - 1, counters)
        if success:
            return True

        # Retry attempts — limited by budget
        for attempt in range(retries_per_hop - 1):
            if not budget.allow_retry():
                break  # Budget exhausted
            budget.record_retry()
            success = simulate_chain_with_budget(depth - 1, counters)
            if success:
                return True
        return False

    counters_with_budget: dict[str, int] = {}
    simulate_chain_with_budget(chain_depth, counters_with_budget)

    total_with_budget = sum(counters_with_budget.values())
    print(f"  Requests per service:")
    for svc in ['A', 'B', 'C', 'D', 'E']:
        count = counters_with_budget.get(svc, 0)
        bar = "#" * min(count, 60)
        print(f"    Service {svc}: {count:>4d} requests  {bar}")

    print()
    print(f"  Total requests in system: {total_with_budget}")
    print(f"  Reduction: {total_no_budget}x -> {total_with_budget}x "
          f"({(1 - total_with_budget/total_no_budget)*100:.0f}% fewer requests)")
    print()
    print("  The retry budget prevents exponential amplification by limiting")
    print("  each service to a fixed retry percentage of its traffic.")
    print()


# ═══════════════════════════════════════════════════════════════════════
# Part 4: Idempotency Store
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class StoredResponse:
    """A cached response for an idempotency key."""
    status_code: int
    body: dict[str, Any]
    created_at: float
    request_hash: str


@dataclass
class IdempotencyStore:
    """Key-to-response cache with TTL for safe retries.

    Stores {idempotency_key → (request_hash, response)} so that
    retried requests return the same response without re-processing.

    Usage:
        store = IdempotencyStore(ttl_seconds=86400)  # 24-hour TTL

        key = request.headers["Idempotency-Key"]
        cached = store.get(key)
        if cached:
            if cached.request_hash != hash_request(request):
                return 422, "Key reused with different params"
            return cached.status_code, cached.body

        # Process the request
        response = process_order(request)
        store.put(key, hash_request(request), 201, response)
        return 201, response
    """
    ttl_seconds: float = 86400.0  # 24 hours (like Stripe)
    _store: dict[str, StoredResponse] = field(default_factory=dict)

    # Metrics
    hits: int = 0
    misses: int = 0
    conflicts: int = 0

    def get(self, key: str) -> StoredResponse | None:
        """Look up a stored response by idempotency key."""
        entry = self._store.get(key)
        if entry is None:
            self.misses += 1
            return None

        # Check TTL
        if time.time() - entry.created_at > self.ttl_seconds:
            del self._store[key]
            self.misses += 1
            return None

        self.hits += 1
        return entry

    def put(self, key: str, request_hash: str, status_code: int, body: dict) -> None:
        """Store a response for an idempotency key."""
        self._store[key] = StoredResponse(
            status_code=status_code,
            body=body,
            created_at=time.time(),
            request_hash=request_hash,
        )

    def size(self) -> int:
        """Return the number of stored responses."""
        return len(self._store)

    def prune_expired(self) -> int:
        """Remove expired entries and return the count removed."""
        now = time.time()
        expired = [
            k for k, v in self._store.items()
            if now - v.created_at > self.ttl_seconds
        ]
        for k in expired:
            del self._store[k]
        return len(expired)


def hash_request(params: dict) -> str:
    """Create a hash of request parameters for idempotency conflict detection."""
    return hashlib.sha256(str(sorted(params.items())).encode()).hexdigest()[:16]


def demo_idempotency_keys() -> None:
    """Show idempotency keys preventing duplicate order creation."""
    print("=" * 72)
    print("  PART 5: Idempotency Keys — Preventing Duplicate Orders")
    print("=" * 72)
    print()

    store = IdempotencyStore(ttl_seconds=3600.0)
    orders_created: list[str] = []

    def create_order(idempotency_key: str, params: dict) -> tuple[int, dict]:
        """Simulate an order creation endpoint with idempotency."""
        req_hash = hash_request(params)

        # Check for existing response
        cached = store.get(idempotency_key)
        if cached is not None:
            if cached.request_hash != req_hash:
                store.conflicts += 1
                return 422, {"error": "Idempotency key reused with different parameters"}
            return cached.status_code, cached.body

        # Process the order (this is the actual business logic)
        order = Order(
            id=f"ord_{uuid.uuid4().hex[:6]}",
            customer=Customer(id=params["customer_id"], name="Alice"),
            restaurant_id=params["restaurant_id"],
            items=[
                OrderItem(
                    menu_item=MenuItem(
                        id="item_01", name="Classic Burger", price_cents=899
                    ),
                    quantity=params.get("quantity", 1),
                )
            ],
        )
        orders_created.append(order.id)

        response = {
            "order_id": order.id,
            "status": order.status.value,
            "total_cents": order.total_cents,
        }

        # Store for future retries
        store.put(idempotency_key, req_hash, 201, response)
        return 201, response

    # Scenario 1: Normal order creation
    print("  Scenario 1: Normal order creation")
    key1 = str(uuid.uuid4())
    params1 = {"customer_id": "cust_01", "restaurant_id": "rest_01", "quantity": 2}
    status, body = create_order(key1, params1)
    print(f"    Request 1:  POST /orders (Key: {key1[:12]}...)")
    print(f"    Response:   {status} — {body}")
    print()

    # Scenario 2: Retry with same key (network timeout — client didn't get response)
    print("  Scenario 2: Client retries with SAME key (network timeout)")
    status, body = create_order(key1, params1)
    print(f"    Request 2:  POST /orders (Key: {key1[:12]}...) [RETRY]")
    print(f"    Response:   {status} — {body}")
    print(f"    Same order returned! No duplicate created.")
    print()

    # Scenario 3: Retry again
    print("  Scenario 3: Client retries AGAIN with same key")
    status, body = create_order(key1, params1)
    print(f"    Request 3:  POST /orders (Key: {key1[:12]}...) [RETRY]")
    print(f"    Response:   {status} — {body}")
    print(f"    Still the same order. Idempotent!")
    print()

    # Scenario 4: New order with new key
    print("  Scenario 4: New order with NEW key")
    key2 = str(uuid.uuid4())
    params2 = {"customer_id": "cust_01", "restaurant_id": "rest_02", "quantity": 1}
    status, body = create_order(key2, params2)
    print(f"    Request 4:  POST /orders (Key: {key2[:12]}...)")
    print(f"    Response:   {status} — {body}")
    print(f"    Different key → different order. Correct!")
    print()

    # Scenario 5: Key reuse with different params (conflict)
    print("  Scenario 5: Reusing key with DIFFERENT parameters (conflict!)")
    params_different = {"customer_id": "cust_02", "restaurant_id": "rest_03", "quantity": 5}
    status, body = create_order(key1, params_different)
    print(f"    Request 5:  POST /orders (Key: {key1[:12]}...) [DIFFERENT PARAMS]")
    print(f"    Response:   {status} — {body}")
    print()

    # Summary
    print(f"  Summary:")
    print(f"    Total requests processed: 5")
    print(f"    Actual orders created:    {len(orders_created)} ({', '.join(orders_created)})")
    print(f"    Cache hits (deduped):     {store.hits}")
    print(f"    Cache misses (new):       {store.misses}")
    print(f"    Conflicts detected:       {store.conflicts}")
    print()
    print("  Without idempotency keys, 3 retries of request 1 would have")
    print("  created 3 separate orders. The customer gets charged 3x.")
    print("  With idempotency keys, only 1 order was created.")
    print()


# ═══════════════════════════════════════════════════════════════════════
# Part 5: Combined Demo — All Patterns Working Together
# ═══════════════════════════════════════════════════════════════════════


async def demo_combined() -> None:
    """Show all resilience patterns working together in a FoodDash scenario."""
    print("=" * 72)
    print("  PART 6: Combined — FoodDash Order with Full Resilience")
    print("=" * 72)
    print()

    print("  Scenario: Customer places an order during a payment service outage.")
    print("  The order service uses all four resilience patterns:")
    print("    1. Exponential backoff with jitter for payment retries")
    print("    2. Circuit breaker to stop hammering the payment service")
    print("    3. Retry budget to limit amplification")
    print("    4. Idempotency key to prevent duplicate charges")
    print()

    # Setup
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout=1.0, window_size=10.0)
    backoff = ExponentialBackoff(base=0.1, max_delay=2.0, strategy=JitterStrategy.FULL)
    budget = RetryBudget(budget_percent=20.0, window_seconds=10.0, min_retries=2)
    store = IdempotencyStore(ttl_seconds=3600.0)

    payment_service_healthy = False
    payment_call_count = 0
    request_num = 0

    async def charge_payment(idempotency_key: str, amount_cents: int) -> tuple[bool, str]:
        """Simulate calling the payment service with all resilience patterns."""
        nonlocal payment_call_count, request_num
        request_num += 1
        req_num = request_num

        # Check idempotency first
        req_hash = hash_request({"key": idempotency_key, "amount": amount_cents})
        cached = store.get(idempotency_key)
        if cached:
            print(f"    [{req_num:2d}] Idempotency hit — returning cached response")
            return True, cached.body.get("charge_id", "unknown")

        # Check circuit breaker
        if not cb.allow_request():
            print(f"    [{req_num:2d}] Circuit OPEN — fast reject (0ms)")
            return False, "circuit_open"

        # Call the service
        payment_call_count += 1
        if not payment_service_healthy:
            cb.record_failure()
            print(f"    [{req_num:2d}] Payment FAILED — {cb.get_state_display()}")
            return False, "service_down"
        else:
            cb.record_success()
            charge_id = f"ch_{uuid.uuid4().hex[:8]}"
            store.put(idempotency_key, req_hash, 200, {"charge_id": charge_id})
            print(f"    [{req_num:2d}] Payment SUCCESS — charge_id={charge_id}")
            return True, charge_id

    async def place_order_with_resilience(order_id: str, amount_cents: int) -> bool:
        """Place an order with retry + backoff + circuit breaker + idempotency."""
        idempotency_key = f"order_{order_id}_payment"
        max_retries = 5

        budget.record_request()

        for attempt in range(max_retries):
            success, result = await charge_payment(idempotency_key, amount_cents)

            if success:
                print(f"    Order {order_id}: Payment confirmed! (attempt {attempt + 1})")
                return True

            if result == "circuit_open":
                print(f"    Order {order_id}: Circuit open, using fallback "
                      f"(queue charge for later)")
                return True  # Graceful degradation

            # Check retry budget before retrying
            if attempt < max_retries - 1:
                if not budget.allow_retry():
                    print(f"    Order {order_id}: Retry budget exhausted, failing")
                    return False
                budget.record_retry()

                delay = backoff.delay(attempt)
                print(f"    Backing off {delay:.2f}s before retry {attempt + 2}...")
                await asyncio.sleep(delay)

        return False

    # Simulate 3 orders during outage
    random.seed(99)
    print("  Payment service is DOWN. Three customers place orders simultaneously:")
    print()

    for i, (customer, order_id) in enumerate([
        ("Alice", "ord_001"),
        ("Bob", "ord_002"),
        ("Carol", "ord_003"),
    ]):
        print(f"  {customer}'s order ({order_id}):")
        success = await place_order_with_resilience(order_id, 1799)
        status = "QUEUED (graceful degradation)" if success else "FAILED"
        print(f"    Result: {status}")
        print()

    # Payment service recovers
    print("  --- Payment service RECOVERS ---")
    print()
    payment_service_healthy = True
    await asyncio.sleep(cb.recovery_timeout + 0.1)

    # Fourth order should work
    print("  Dave's order (ord_004) — after recovery:")
    request_num = 0
    success = await place_order_with_resilience("ord_004", 2199)
    status = "SUCCESS" if success else "FAILED"
    print(f"    Result: {status}")
    print()

    print(f"  Total payment service calls: {payment_call_count}")
    print(f"  Without resilience patterns: would have been ~15+ blocking calls")
    print(f"  with 30s timeouts each = 450s of blocked threads")
    print()


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════


def main() -> None:
    """Run all resilience pattern demonstrations."""
    print()
    print("+" * 72)
    print("+     Appendix D — Resilience Patterns Demo                        +")
    print("+     (Retry, Circuit Breaker, Backoff, Idempotency)               +")
    print("+                                                                  +")
    print("+     Working implementations applied to FoodDash.                 +")
    print("+     No external dependencies — pure Python.                      +")
    print("+" * 72)
    print()

    # Part 1: Backoff strategies
    demo_backoff_strategies()

    # Part 2: Thundering herd visualization
    demo_thundering_herd()

    # Part 3: Circuit breaker
    demo_circuit_breaker()

    # Part 4: Retry amplification
    demo_retry_amplification()

    # Part 5: Idempotency keys
    demo_idempotency_keys()

    # Part 6: Combined demo
    asyncio.run(demo_combined())

    print("=" * 72)
    print("  Demo complete. See README.md for full educational content.")
    print("  See visual.html for interactive visualizations.")
    print("=" * 72)
    print()


if __name__ == "__main__":
    main()

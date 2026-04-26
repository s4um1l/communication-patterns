"""
Polling vs SSE vs WebSocket -- Simulated Latency & Bandwidth Benchmark

Simulates 100 FoodDash orders going through status changes and compares
detection latency, bandwidth, and request count across four patterns.

This is a SIMULATION, not a live benchmark. It models the theoretical
behavior based on each pattern's characteristics.

Run: uv run python -m benchmarks.polling_vs_sse_vs_ws
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NUM_ORDERS = 100
STATUSES = ["received", "cooking", "ready_for_pickup", "driver_arriving", "delivered"]
# Seconds between status transitions (randomized within range)
TRANSITION_MIN_SECS = 30
TRANSITION_MAX_SECS = 120
# Observation window: we simulate until all orders reach "delivered"
SHORT_POLL_INTERVAL = 2.0  # seconds
LONG_POLL_TIMEOUT = 25.0  # seconds (server hold time)
LONG_POLL_RECONNECT_OVERHEAD = 0.1  # seconds for TCP re-setup

# Wire overhead estimates (bytes)
HTTP_REQUEST_OVERHEAD = 250   # request line + typical headers
HTTP_RESPONSE_OVERHEAD = 200  # status line + typical headers
JSON_STATUS_BODY = 80         # {"order_id": "abc123", "status": "cooking"}
SSE_FRAME_OVERHEAD = 30       # "id: 47\nevent: status\ndata: " + "\n\n"
WS_FRAME_OVERHEAD = 6         # 2-byte header + 4-byte mask (client->server)
WS_HANDSHAKE = 600            # HTTP upgrade request + response (one-time)
SSE_INITIAL_REQUEST = 300     # initial GET with Accept: text/event-stream


# ---------------------------------------------------------------------------
# Simulate order timelines
# ---------------------------------------------------------------------------

@dataclass
class StatusChange:
    order_id: int
    new_status: str
    timestamp: float  # seconds since simulation start


def generate_order_events() -> list[StatusChange]:
    """Generate randomized status change events for all orders."""
    events: list[StatusChange] = []
    for order_id in range(NUM_ORDERS):
        t = random.uniform(0, 10)  # staggered order placement
        for status in STATUSES:
            events.append(StatusChange(order_id, status, t))
            t += random.uniform(TRANSITION_MIN_SECS, TRANSITION_MAX_SECS)
    events.sort(key=lambda e: e.timestamp)
    return events


# ---------------------------------------------------------------------------
# Pattern simulators
# ---------------------------------------------------------------------------

@dataclass
class PatternResult:
    name: str
    detection_latencies: list[float] = field(default_factory=list)
    total_bytes: int = 0
    total_requests: int = 0

    @property
    def avg_latency(self) -> float:
        return statistics.mean(self.detection_latencies) if self.detection_latencies else 0

    @property
    def p50_latency(self) -> float:
        return statistics.median(self.detection_latencies) if self.detection_latencies else 0

    @property
    def p99_latency(self) -> float:
        if not self.detection_latencies:
            return 0
        sorted_lat = sorted(self.detection_latencies)
        idx = int(len(sorted_lat) * 0.99)
        return sorted_lat[min(idx, len(sorted_lat) - 1)]


def simulate_short_polling(events: list[StatusChange]) -> PatternResult:
    """
    Short polling: each order is polled every SHORT_POLL_INTERVAL seconds.
    Detection latency = time from status change to next poll that observes it.
    Every poll request costs HTTP overhead, regardless of whether data changed.
    """
    result = PatternResult(name="Short Poll (2s)")
    sim_end = max(e.timestamp for e in events) + SHORT_POLL_INTERVAL

    # Track latest status per order and what the client last saw
    actual_status: dict[int, tuple[str, float]] = {}
    client_seen: dict[int, str] = {}

    # Build timeline of events by order
    events_by_order: dict[int, list[StatusChange]] = {}
    for e in events:
        events_by_order.setdefault(e.order_id, []).append(e)

    # For each order, set initial state
    for order_id in range(NUM_ORDERS):
        actual_status[order_id] = (STATUSES[0], events_by_order[order_id][0].timestamp)
        client_seen[order_id] = ""

    # Simulate polling ticks
    # Each order is polled independently starting from its creation time
    for order_id in range(NUM_ORDERS):
        order_events = events_by_order[order_id]
        event_idx = 0
        poll_time = order_events[0].timestamp  # start polling at order creation

        while poll_time <= sim_end and event_idx < len(order_events):
            # Advance actual status to all changes <= poll_time
            while (event_idx < len(order_events) and
                   order_events[event_idx].timestamp <= poll_time):
                actual_status[order_id] = (
                    order_events[event_idx].new_status,
                    order_events[event_idx].timestamp,
                )
                event_idx += 1

            current_status, change_time = actual_status[order_id]

            # Every poll costs a request + response
            result.total_requests += 1
            result.total_bytes += HTTP_REQUEST_OVERHEAD + HTTP_RESPONSE_OVERHEAD + JSON_STATUS_BODY

            # Did we detect a new status?
            if current_status != client_seen.get(order_id):
                latency = poll_time - change_time
                result.detection_latencies.append(latency)
                client_seen[order_id] = current_status

                if current_status == "delivered":
                    break

            poll_time += SHORT_POLL_INTERVAL

    return result


def simulate_long_polling(events: list[StatusChange]) -> PatternResult:
    """
    Long polling: client sends request, server holds until status changes
    or timeout (25s). On change, responds immediately and client reconnects.
    Detection latency = reconnection overhead only (change happens while connected).
    """
    result = PatternResult(name="Long Poll (25s)")

    events_by_order: dict[int, list[StatusChange]] = {}
    for e in events:
        events_by_order.setdefault(e.order_id, []).append(e)

    for order_id in range(NUM_ORDERS):
        order_events = events_by_order[order_id]
        event_queue = list(order_events)
        current_event_idx = 0
        client_time = order_events[0].timestamp  # client starts polling at creation

        while current_event_idx < len(order_events):
            # Client sends long poll request at client_time
            result.total_requests += 1
            result.total_bytes += HTTP_REQUEST_OVERHEAD

            # Find next event after client_time
            next_event = None
            for i in range(current_event_idx, len(order_events)):
                if order_events[i].timestamp >= client_time:
                    next_event = order_events[i]
                    current_event_idx = i + 1
                    break

            if next_event is None:
                # No more events, timeout
                result.total_bytes += HTTP_RESPONSE_OVERHEAD + 20  # {"status":"no_change"}
                break

            wait_time = next_event.timestamp - client_time
            if wait_time > LONG_POLL_TIMEOUT:
                # Timeout before event -- empty response, reconnect
                result.total_bytes += HTTP_RESPONSE_OVERHEAD + 20
                client_time += LONG_POLL_TIMEOUT + LONG_POLL_RECONNECT_OVERHEAD
                current_event_idx -= 1  # re-check same event next round
            else:
                # Event happened while connected -- instant response
                result.total_bytes += HTTP_RESPONSE_OVERHEAD + JSON_STATUS_BODY
                latency = LONG_POLL_RECONNECT_OVERHEAD  # only reconnect overhead
                result.detection_latencies.append(latency)
                client_time = next_event.timestamp + LONG_POLL_RECONNECT_OVERHEAD

                if next_event.new_status == "delivered":
                    break

    return result


def simulate_sse(events: list[StatusChange]) -> PatternResult:
    """
    SSE: one persistent connection per order. Server pushes events as they happen.
    Detection latency ~= 0 (event pushed instantly over open connection).
    Cost: initial HTTP request + SSE frame per event.
    """
    result = PatternResult(name="SSE")

    events_by_order: dict[int, list[StatusChange]] = {}
    for e in events:
        events_by_order.setdefault(e.order_id, []).append(e)

    for order_id in range(NUM_ORDERS):
        # One HTTP request to establish the SSE connection
        result.total_requests += 1
        result.total_bytes += SSE_INITIAL_REQUEST + HTTP_RESPONSE_OVERHEAD

        for event in events_by_order[order_id]:
            # Each event pushed as an SSE frame
            result.total_bytes += SSE_FRAME_OVERHEAD + JSON_STATUS_BODY
            # Near-zero detection latency (network propagation only)
            result.detection_latencies.append(0.005)  # ~5ms network propagation

    return result


def simulate_websocket(events: list[StatusChange]) -> PatternResult:
    """
    WebSocket: one persistent connection per order. Bidirectional.
    Detection latency ~= 0 (pushed over open connection).
    Cost: one handshake + minimal frame overhead per event.
    """
    result = PatternResult(name="WebSocket")

    events_by_order: dict[int, list[StatusChange]] = {}
    for e in events:
        events_by_order.setdefault(e.order_id, []).append(e)

    for order_id in range(NUM_ORDERS):
        # One WebSocket handshake (HTTP upgrade)
        result.total_requests += 1
        result.total_bytes += WS_HANDSHAKE

        for event in events_by_order[order_id]:
            # Each event is a WebSocket frame (server -> client, no mask)
            result.total_bytes += WS_FRAME_OVERHEAD + JSON_STATUS_BODY
            result.detection_latencies.append(0.003)  # ~3ms (less overhead than SSE)

    return result


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_bytes(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    elif b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    else:
        return f"{b / (1024 * 1024):.2f} MB"


def bar_chart(value: float, max_value: float, width: int = 40) -> str:
    if max_value == 0:
        return ""
    filled = int((value / max_value) * width)
    return "#" * filled + "." * (width - filled)


def print_results(results: list[PatternResult]) -> None:
    print()
    print("=" * 80)
    print(f"  COMMUNICATION PATTERN BENCHMARK (simulated)")
    print(f"  {NUM_ORDERS} orders, {len(STATUSES)} status changes each")
    print("=" * 80)

    # -- Latency table --
    print()
    print("  DETECTION LATENCY (seconds from status change to client awareness)")
    print("  " + "-" * 76)
    print(f"  {'Pattern':<20} {'Avg':>8} {'P50':>8} {'P99':>8}  {'Distribution'}")
    print("  " + "-" * 76)

    max_p99 = max(r.p99_latency for r in results)
    for r in results:
        bar = bar_chart(r.avg_latency, max_p99, 30)
        print(f"  {r.name:<20} {r.avg_latency:>7.3f}s {r.p50_latency:>7.3f}s {r.p99_latency:>7.3f}s  {bar}")

    # -- Bandwidth table --
    print()
    print("  TOTAL BYTES TRANSFERRED")
    print("  " + "-" * 76)
    print(f"  {'Pattern':<20} {'Total':>12}  {'Chart'}")
    print("  " + "-" * 76)

    max_bytes = max(r.total_bytes for r in results)
    for r in results:
        bar = bar_chart(r.total_bytes, max_bytes, 40)
        print(f"  {r.name:<20} {format_bytes(r.total_bytes):>12}  {bar}")

    # -- Request count table --
    print()
    print("  TOTAL HTTP REQUESTS")
    print("  " + "-" * 76)
    print(f"  {'Pattern':<20} {'Requests':>10}  {'Chart'}")
    print("  " + "-" * 76)

    max_requests = max(r.total_requests for r in results)
    for r in results:
        bar = bar_chart(r.total_requests, max_requests, 40)
        print(f"  {r.name:<20} {r.total_requests:>10,}  {bar}")

    # -- Efficiency summary --
    print()
    print("  EFFICIENCY SUMMARY")
    print("  " + "-" * 76)
    baseline = results[0]  # short polling
    for r in results[1:]:
        latency_improvement = baseline.avg_latency / max(r.avg_latency, 0.001)
        bandwidth_savings = (1 - r.total_bytes / max(baseline.total_bytes, 1)) * 100
        request_savings = (1 - r.total_requests / max(baseline.total_requests, 1)) * 100
        print(f"  {r.name} vs Short Polling:")
        print(f"    Latency:    {latency_improvement:>6.0f}x faster")
        print(f"    Bandwidth:  {bandwidth_savings:>6.1f}% less")
        print(f"    Requests:   {request_savings:>6.1f}% fewer")
        print()

    print("=" * 80)
    print("  NOTE: This is a simulation, not a live benchmark.")
    print("  Real-world results depend on network conditions and implementation.")
    print("=" * 80)
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    random.seed(42)  # Reproducible results
    events = generate_order_events()

    results = [
        simulate_short_polling(events),
        simulate_long_polling(events),
        simulate_sse(events),
        simulate_websocket(events),
    ]

    print_results(results)


if __name__ == "__main__":
    main()

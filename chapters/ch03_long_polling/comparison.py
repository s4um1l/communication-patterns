"""Chapter 03 — Long Polling vs Short Polling: Side-by-Side Comparison

This script runs BOTH a short poller and a long poller against the same server,
tracking the same order. It advances the order status periodically and measures
each approach's efficiency.

At the end, it prints a comparison table showing:
    - Total requests made
    - Bytes transferred
    - Average detection latency
    - Efficiency (useful responses / total)

This demonstrates the concrete improvement long polling provides over short polling.

IMPORTANT: Requires the ch03 server running:
    uv run uvicorn chapters.ch03_long_polling.server:app --port 8003

Run with:
    uv run python -m chapters.ch03_long_polling.comparison
"""

from __future__ import annotations

import sys
import time
import threading
from dataclasses import dataclass, field

import httpx

BASE_URL = "http://localhost:8003"
SHORT_POLL_INTERVAL = 2.0  # Seconds between short poll requests
LONG_POLL_TIMEOUT = 8  # Server hold timeout for long polls
ADVANCE_INTERVAL = 5.0  # Seconds between status advances
NUM_ADVANCES = 4  # placed -> confirmed -> preparing -> ready -> picked_up


# ---------------------------------------------------------------------------
# Tracking
# ---------------------------------------------------------------------------


@dataclass
class PollerStats:
    """Tracks metrics for one polling strategy."""
    name: str
    total_requests: int = 0
    total_bytes_sent: int = 0
    total_bytes_received: int = 0
    status_changes_detected: int = 0
    detection_events: list = field(default_factory=list)  # (change_time, detect_time)
    wasted_requests: int = 0
    errors: int = 0

    @property
    def useful_requests(self) -> int:
        return self.status_changes_detected

    @property
    def efficiency_pct(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return (self.useful_requests / self.total_requests) * 100

    @property
    def avg_detection_latency_ms(self) -> float:
        if not self.detection_events:
            return 0.0
        latencies = [
            (detect - change) * 1000
            for change, detect in self.detection_events
        ]
        return sum(latencies) / len(latencies)

    @property
    def total_bytes(self) -> int:
        return self.total_bytes_sent + self.total_bytes_received


# Shared state: when did each status change actually happen?
_change_timestamps: dict[str, float] = {}
_change_lock = threading.Lock()
_stop_event = threading.Event()


def _estimate_request_bytes(method: str, path: str, body: str = "") -> int:
    """Rough estimate of HTTP request bytes (headers + body)."""
    # Approximate: method + path + HTTP version + standard headers
    header_size = len(f"{method} {path} HTTP/1.1\r\n") + 200  # ~200 bytes of headers
    return header_size + len(body)


def _estimate_response_bytes(resp: httpx.Response) -> int:
    """Rough estimate of HTTP response bytes."""
    header_size = sum(len(k) + len(v) + 4 for k, v in resp.headers.items()) + 20
    return header_size + len(resp.content)


# ---------------------------------------------------------------------------
# Short Poller
# ---------------------------------------------------------------------------


def short_poller(order_id: str, stats: PollerStats) -> None:
    """Classic short polling: GET /orders/{id} every N seconds."""
    last_known_status = "placed"

    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
        while not _stop_event.is_set():
            try:
                stats.total_requests += 1
                path = f"/orders/{order_id}"
                stats.total_bytes_sent += _estimate_request_bytes("GET", path)

                resp = client.get(path)
                detect_time = time.time()
                stats.total_bytes_received += _estimate_response_bytes(resp)

                if resp.status_code != 200:
                    stats.errors += 1
                    continue

                data = resp.json()
                current_status = data["status"]

                if current_status != last_known_status:
                    stats.status_changes_detected += 1
                    # Find when this change actually happened
                    with _change_lock:
                        change_time = _change_timestamps.get(current_status, detect_time)
                    stats.detection_events.append((change_time, detect_time))
                    last_known_status = current_status

                    if current_status in ("delivered", "cancelled", "picked_up"):
                        break
                else:
                    stats.wasted_requests += 1

            except Exception:
                stats.errors += 1

            # Wait before next poll
            _stop_event.wait(SHORT_POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Long Poller
# ---------------------------------------------------------------------------


def long_poller(order_id: str, stats: PollerStats) -> None:
    """Long polling: GET /orders/{id}/poll — server holds until change or timeout."""
    last_known_status = "placed"

    with httpx.Client(base_url=BASE_URL, timeout=60.0) as client:
        while not _stop_event.is_set():
            try:
                stats.total_requests += 1
                path = f"/orders/{order_id}/poll?timeout={LONG_POLL_TIMEOUT}&last_status={last_known_status}"
                stats.total_bytes_sent += _estimate_request_bytes("GET", path)

                resp = client.get(
                    f"/orders/{order_id}/poll",
                    params={"timeout": LONG_POLL_TIMEOUT, "last_status": last_known_status},
                )
                detect_time = time.time()
                stats.total_bytes_received += _estimate_response_bytes(resp)

                if resp.status_code != 200:
                    stats.errors += 1
                    continue

                data = resp.json()

                if data["changed"]:
                    stats.status_changes_detected += 1
                    new_status = data["status"]
                    with _change_lock:
                        change_time = _change_timestamps.get(new_status, detect_time)
                    stats.detection_events.append((change_time, detect_time))
                    last_known_status = new_status

                    if last_known_status in ("delivered", "cancelled", "picked_up"):
                        break
                else:
                    stats.wasted_requests += 1

            except httpx.ReadTimeout:
                stats.wasted_requests += 1
            except Exception:
                stats.errors += 1


# ---------------------------------------------------------------------------
# Status Advancer
# ---------------------------------------------------------------------------


def advance_order(order_id: str) -> None:
    """Advance the order status at fixed intervals."""
    time.sleep(ADVANCE_INTERVAL)  # Initial delay

    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
        for i in range(NUM_ADVANCES):
            if _stop_event.is_set():
                break

            try:
                resp = client.post(f"/orders/{order_id}/advance")
                if resp.status_code == 200:
                    data = resp.json()
                    change_time = time.time()
                    with _change_lock:
                        _change_timestamps[data["new_status"]] = change_time
                    print(
                        f"  [{time.time() - _start:.1f}s] "
                        f"STATUS CHANGE: {data['old_status']} -> {data['new_status']}"
                    )
            except Exception as e:
                print(f"  Advance error: {e}")

            if i < NUM_ADVANCES - 1:
                time.sleep(ADVANCE_INTERVAL)

    # Give pollers time to detect the last change
    time.sleep(2.0)
    _stop_event.set()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_start = time.time()


def print_header(title: str) -> None:
    width = 70
    print()
    print("=" * width)
    print(f"  {title}")
    print("=" * width)


def main() -> None:
    global _start
    _start = time.time()

    print_header("Chapter 03: Short Polling vs Long Polling Comparison")
    print()
    print(f"  Short poll interval:    {SHORT_POLL_INTERVAL}s")
    print(f"  Long poll timeout:      {LONG_POLL_TIMEOUT}s")
    print(f"  Status advances:        {NUM_ADVANCES} (every {ADVANCE_INTERVAL}s)")
    print()

    # Place an order
    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
        resp = client.post("/orders", json={
            "customer_name": "ComparisonTest",
            "restaurant_id": "rest_01",
            "item_ids": ["item_01", "item_02"],
        })
        if resp.status_code != 201:
            print(f"  ERROR: Failed to place order: {resp.status_code}")
            return
        order_id = resp.json()["order_id"]
        print(f"  Order placed: {order_id}")
        print()

    short_stats = PollerStats(name="Short Polling")
    long_stats = PollerStats(name="Long Polling")

    print("  Starting both pollers and status advancer...")
    print("  Watch the status changes and how each poller detects them:")
    print()

    # Start all threads
    threads = [
        threading.Thread(target=short_poller, args=(order_id, short_stats), daemon=True),
        threading.Thread(target=long_poller, args=(order_id, long_stats), daemon=True),
        threading.Thread(target=advance_order, args=(order_id,), daemon=True),
    ]
    for t in threads:
        t.start()

    # Wait for completion
    for t in threads:
        t.join(timeout=60)

    total_time = time.time() - _start

    # ----- Results -----
    print_header("Results")
    print()

    # Table header
    metric_width = 32
    val_width = 20
    print(f"  {'Metric':<{metric_width}} {'Short Polling':>{val_width}} {'Long Polling':>{val_width}}")
    print(f"  {'-' * metric_width} {'-' * val_width} {'-' * val_width}")

    rows = [
        ("Total requests", str(short_stats.total_requests), str(long_stats.total_requests)),
        ("Status changes detected", str(short_stats.status_changes_detected), str(long_stats.status_changes_detected)),
        ("Wasted requests", str(short_stats.wasted_requests), str(long_stats.wasted_requests)),
        ("Efficiency", f"{short_stats.efficiency_pct:.1f}%", f"{long_stats.efficiency_pct:.1f}%"),
        ("Avg detection latency", f"{short_stats.avg_detection_latency_ms:.0f} ms", f"{long_stats.avg_detection_latency_ms:.0f} ms"),
        ("Total bytes transferred", f"~{short_stats.total_bytes:,} B", f"~{long_stats.total_bytes:,} B"),
        ("Errors", str(short_stats.errors), str(long_stats.errors)),
    ]

    for label, short_val, long_val in rows:
        print(f"  {label:<{metric_width}} {short_val:>{val_width}} {long_val:>{val_width}}")

    print()
    print(f"  Total elapsed time: {total_time:.1f}s")

    # Analysis
    print()
    print_header("Analysis")
    print()

    if short_stats.total_requests > 0 and long_stats.total_requests > 0:
        request_ratio = short_stats.total_requests / max(long_stats.total_requests, 1)
        print(f"  Short polling made {request_ratio:.1f}x more requests than long polling")

    if short_stats.avg_detection_latency_ms > 0 and long_stats.avg_detection_latency_ms > 0:
        latency_ratio = short_stats.avg_detection_latency_ms / max(long_stats.avg_detection_latency_ms, 1)
        print(f"  Long polling detected changes {latency_ratio:.1f}x faster")
    elif long_stats.avg_detection_latency_ms > 0:
        print(f"  Long polling avg detection latency: {long_stats.avg_detection_latency_ms:.0f}ms")

    if short_stats.total_bytes > 0 and long_stats.total_bytes > 0:
        byte_ratio = short_stats.total_bytes / max(long_stats.total_bytes, 1)
        print(f"  Short polling transferred {byte_ratio:.1f}x more bytes")

    print()
    print("  Key insight: Long polling achieves the same outcome (detecting all status")
    print("  changes) with far fewer requests, lower bandwidth, and near-instant")
    print("  detection latency. The tradeoff is server memory (held connections).")
    print()
    print("  Next: Chapter 04 — SSE")
    print("  'What if we could stream MULTIPLE events over a single connection?'")
    print()


if __name__ == "__main__":
    try:
        main()
    except httpx.ConnectError:
        print(f"\n  ERROR: Cannot connect to server at {BASE_URL}")
        print("  Start the server first:")
        print("    uv run uvicorn chapters.ch03_long_polling.server:app --port 8003\n")
        sys.exit(1)

"""Chapter 02 — Short Polling Client

Demonstrates the short polling pattern by placing an order and then
repeatedly polling GET /orders/{id} every POLL_INTERVAL seconds.

Tracks and displays:
    - Total polls vs useful polls (where status actually changed)
    - Running efficiency percentage
    - Cumulative bytes transferred
    - Per-request timing
    - A final summary with the full waste breakdown

Run with:
    uv run python -m chapters.ch02_short_polling.client_poller

Requires the Ch02 server running on port 8002:
    uv run uvicorn chapters.ch02_short_polling.server:app --port 8002
"""

from __future__ import annotations

import sys
import time

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SERVER = "http://localhost:8002"
POLL_INTERVAL = 2.0  # seconds between polls
MAX_POLLS = 200  # safety limit so the demo doesn't run forever


def main() -> None:
    print("=" * 70)
    print("  FoodDash Ch02 — Short Polling Client")
    print("  Demonstrating the waste of repeated status checks")
    print("=" * 70)

    with httpx.Client(base_url=SERVER, timeout=10.0) as client:
        # ------------------------------------------------------------------
        # Step 1: Place an order
        # ------------------------------------------------------------------
        print("\n[1] Placing order...")
        resp = client.post(
            "/orders",
            json={
                "customer_name": "Polling Pat",
                "restaurant_id": "rest_01",
                "item_ids": ["item_01", "item_02", "item_03"],
            },
        )
        if resp.status_code != 201:
            print(f"    Failed to place order: {resp.status_code} {resp.text}")
            sys.exit(1)

        order = resp.json()
        order_id = order["order_id"]
        print(f"    Order {order_id} placed! Status: {order['status']}")
        print(f"    Total: ${order['total_cents'] / 100:.2f}")
        print(f"\n[2] Starting short polling every {POLL_INTERVAL}s...")
        print(f"    Advance the order in another terminal with:")
        print(f"    curl -X POST {SERVER}/orders/{order_id}/advance")
        print()

        # ------------------------------------------------------------------
        # Step 2: Poll repeatedly
        # ------------------------------------------------------------------
        last_status = order["status"]
        total_polls = 0
        useful_polls = 0
        wasted_polls = 0
        total_bytes = 0
        latencies: list[float] = []
        start_time = time.time()

        header = (
            f"{'#':>4}  {'Latency':>8}  {'Status':<12}  {'Changed?':<10}  "
            f"{'Useful':>6} / {'Total':<6}  {'Eff%':>6}  {'Bytes':>10}"
        )
        print(header)
        print("-" * len(header))

        try:
            for poll_num in range(1, MAX_POLLS + 1):
                time.sleep(POLL_INTERVAL)

                t0 = time.time()
                resp = client.get(f"/orders/{order_id}")
                t1 = time.time()

                if resp.status_code != 200:
                    print(f"    Error: {resp.status_code}")
                    continue

                latency_ms = (t1 - t0) * 1000
                latencies.append(latency_ms)

                # Count bytes: request line + headers + response headers + body
                request_overhead = 500  # approximate request headers
                response_bytes = len(resp.content) + 300  # body + response headers
                round_trip_bytes = request_overhead + response_bytes
                total_bytes += round_trip_bytes

                data = resp.json()
                current_status = data["status"]
                total_polls += 1

                changed = current_status != last_status
                if changed:
                    useful_polls += 1
                    last_status = current_status
                else:
                    wasted_polls += 1

                efficiency = (useful_polls / total_polls * 100) if total_polls > 0 else 0

                change_marker = ">> YES <<" if changed else "   no"
                print(
                    f"{poll_num:>4}  {latency_ms:>6.1f}ms  {current_status:<12}  "
                    f"{change_marker:<10}  {useful_polls:>6} / {total_polls:<6}  "
                    f"{efficiency:>5.1f}%  {_human_bytes(total_bytes):>10}"
                )

                # Stop if order is delivered or cancelled
                if current_status in ("delivered", "cancelled"):
                    print(f"\n    Order reached terminal status: {current_status}")
                    break

        except KeyboardInterrupt:
            print("\n    Polling interrupted by user.")

        # ------------------------------------------------------------------
        # Step 3: Print summary
        # ------------------------------------------------------------------
        elapsed = time.time() - start_time
        avg_latency = sum(latencies) / len(latencies) if latencies else 0

        print("\n" + "=" * 70)
        print("  SHORT POLLING SUMMARY")
        print("=" * 70)
        print(f"  Order ID:          {order_id}")
        print(f"  Poll interval:     {POLL_INTERVAL}s")
        print(f"  Duration:          {elapsed:.1f}s")
        print()
        print(f"  Total polls:       {total_polls}")
        print(f"  Useful polls:      {useful_polls}  (status actually changed)")
        print(f"  Wasted polls:      {wasted_polls}  (status was the same)")
        print(f"  Efficiency:        {(useful_polls / total_polls * 100) if total_polls else 0:.2f}%")
        print()
        print(f"  Total bytes:       {_human_bytes(total_bytes)}")
        print(f"  Avg latency:       {avg_latency:.1f}ms")
        print(f"  Min latency:       {min(latencies):.1f}ms" if latencies else "")
        print(f"  Max latency:       {max(latencies):.1f}ms" if latencies else "")
        print()

        # Extrapolate to 10K users
        if total_polls > 0 and elapsed > 0:
            rps = total_polls / elapsed
            print("  --- Extrapolation to 10,000 concurrent users ---")
            print(f"  Requests/sec (this client):  {rps:.1f}")
            print(f"  Requests/sec (10K clients):  {rps * 10_000:.0f}")
            print(f"  Bandwidth (10K clients):     {_human_bytes(int(total_bytes / elapsed * 10_000))}/s")
            print(f"  Wasted req/s (10K clients):  {(wasted_polls / elapsed) * 10_000:.0f}")
            cpu_per_req_ms = 1.0  # conservative estimate
            print(f"  CPU cores for polling:       {rps * 10_000 * cpu_per_req_ms / 1000:.1f}")

        print("=" * 70)
        print()
        print("  The lesson: every single one of those 'no change' lines was a")
        print("  complete HTTP round trip — TCP handshake, request headers, server")
        print("  processing, response serialization — all to learn 'nothing new.'")
        print()
        print("  Chapter 03 (Long Polling) eliminates this waste by holding the")
        print("  connection open until something actually changes.")
        print()


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"


if __name__ == "__main__":
    main()

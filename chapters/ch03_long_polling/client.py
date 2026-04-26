"""Chapter 03 — Long Polling: FoodDash Client

This client demonstrates long polling from the caller's side. After placing
an order, it enters a long poll loop: send a request, wait for the server
to hold it open, get notified when the status changes (or when the timeout
expires), then immediately reconnect.

The educational output shows:
    - When the client is waiting (connection held open by server)
    - When a notification arrives (near-instant detection)
    - When a timeout occurs (no change — reconnect)
    - Total requests made vs status changes detected (efficiency)

Run with:
    uv run python -m chapters.ch03_long_polling.client

Requires the server to be running:
    uv run uvicorn chapters.ch03_long_polling.server:app --port 8003
"""

from __future__ import annotations

import sys
import time
import threading

import httpx

BASE_URL = "http://localhost:8003"
POLL_TIMEOUT = 10  # Shorter timeout for the demo (normally 30s)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def print_header(title: str) -> None:
    width = 70
    print()
    print("=" * width)
    print(f"  {title}")
    print("=" * width)


def print_step(msg: str) -> None:
    elapsed = time.time() - _demo_start
    print(f"  [{elapsed:7.2f}s] {msg}")


_demo_start = time.time()


# ---------------------------------------------------------------------------
# Status advancer (simulates kitchen/driver updates in a background thread)
# ---------------------------------------------------------------------------


def advance_order_periodically(order_id: str, num_advances: int, delay: float) -> None:
    """Advance the order status in the background, simulating real-world updates.

    This runs in a separate thread so it happens independently of the client's
    long poll loop — just like a real kitchen updating order status.
    """
    time.sleep(delay)  # Initial delay before first advance

    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
        for i in range(num_advances):
            # Wait a random-ish interval between advances
            if i > 0:
                time.sleep(delay)

            try:
                resp = client.post(f"/orders/{order_id}/advance")
                if resp.status_code == 200:
                    data = resp.json()
                    print_step(
                        f"[KITCHEN] Advanced: {data['old_status']} -> {data['new_status']}"
                    )
                else:
                    print_step(f"[KITCHEN] Failed to advance: {resp.status_code}")
                    break
            except Exception as e:
                print_step(f"[KITCHEN] Error advancing: {e}")
                break


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------


def main() -> None:
    global _demo_start
    _demo_start = time.time()

    print_header("Chapter 03: Long Polling Pattern")
    print()
    print("  FoodDash — The server holds your request open until something changes.")
    print("  No more wasted requests. No more polling every 3 seconds.")
    print("  The server only responds when it has NEWS.")
    print()

    with httpx.Client(base_url=BASE_URL, timeout=60.0) as client:

        # ----- Step 1: Place an order -----
        print_header("Step 1: Place an Order")

        resp = client.post("/orders", json={
            "customer_name": "Alice",
            "restaurant_id": "rest_01",
            "item_ids": ["item_01", "item_02", "item_03"],
        })

        if resp.status_code != 201:
            print(f"  ERROR: Failed to place order: {resp.status_code} {resp.text}")
            return

        order = resp.json()
        order_id = order["order_id"]
        print_step(f"Order placed! ID: {order_id}")
        print_step(f"Status: {order['status']}")
        print_step(f"Total: ${order['total_cents'] / 100:.2f}")
        print()

        # ----- Step 2: Start background status advances -----
        print_header("Step 2: Long Poll Loop")
        print()
        print("  Now entering the long poll loop. In a separate thread, the 'kitchen'")
        print("  will advance the order status every ~4 seconds. Watch how the client")
        print("  gets INSTANT notification — no wasted requests!")
        print()
        print("  Compare to short polling: instead of asking every 3 seconds and getting")
        print("  'no change' 99% of the time, we ask ONCE and the server tells us WHEN")
        print("  something changes.")
        print()

        # Start the background advancer
        # 5 advances: placed -> confirmed -> preparing -> ready -> picked_up -> en_route
        advancer = threading.Thread(
            target=advance_order_periodically,
            args=(order_id, 5, 4.0),
            daemon=True,
        )
        advancer.start()

        # ----- Step 3: Long poll loop -----
        last_status = order["status"]
        total_requests = 0
        status_changes_detected = 0
        timeouts = 0
        detection_latencies: list[float] = []

        while True:
            total_requests += 1
            request_start = time.time()

            print_step(
                f"[POLL #{total_requests}] Sending long poll "
                f"(last_status={last_status}, timeout={POLL_TIMEOUT}s)..."
            )
            print_step(f"[POLL #{total_requests}] Waiting... server is holding my request open...")

            try:
                # This call will BLOCK until the server responds.
                # The server holds it open for up to POLL_TIMEOUT seconds.
                # If the status changes, the server responds immediately.
                resp = client.get(
                    f"/orders/{order_id}/poll",
                    params={"timeout": POLL_TIMEOUT, "last_status": last_status},
                )
                response_time = time.time()
                held_seconds = response_time - request_start

                if resp.status_code != 200:
                    print_step(f"  ERROR: {resp.status_code} {resp.text}")
                    break

                data = resp.json()

                if data["changed"]:
                    # Status changed — near-instant notification!
                    old_status = last_status
                    last_status = data["status"]
                    status_changes_detected += 1

                    # The detection latency is approximately how long the server
                    # held the request. If the change happened mid-hold, the
                    # server_held_seconds tells us the total hold time.
                    detection_latencies.append(held_seconds)

                    print_step(
                        f"[POLL #{total_requests}] CHANGE DETECTED! "
                        f"{old_status} -> {last_status} "
                        f"(server held for {data['server_held_seconds']:.2f}s)"
                    )

                    # Check for terminal statuses
                    if last_status in ("delivered", "cancelled"):
                        print_step(f"Order reached terminal status: {last_status}")
                        break

                else:
                    # Timeout — no change. Immediately reconnect.
                    timeouts += 1
                    print_step(
                        f"[POLL #{total_requests}] Timeout after {held_seconds:.1f}s — "
                        f"no change. Reconnecting immediately..."
                    )

            except httpx.ReadTimeout:
                # Client-side timeout (shouldn't happen if server timeout < client timeout)
                timeouts += 1
                print_step(
                    f"[POLL #{total_requests}] Client-side timeout. Reconnecting..."
                )

            except httpx.ConnectError:
                print_step("  ERROR: Lost connection to server")
                break

        # ----- Summary -----
        print_header("Summary: Long Polling Results")
        print()
        print(f"  Total long poll requests:     {total_requests}")
        print(f"  Status changes detected:      {status_changes_detected}")
        print(f"  Timeout responses:            {timeouts}")
        print(f"  Efficiency:                   {status_changes_detected}/{total_requests} "
              f"= {status_changes_detected / max(total_requests, 1) * 100:.0f}% useful responses")
        print()

        if detection_latencies:
            avg_latency = sum(detection_latencies) / len(detection_latencies)
            print(f"  Avg server hold time:         {avg_latency:.2f}s")
            print(f"  Detection latency:            Near-instant (server responded as soon as status changed)")
        print()

        # Compare to what short polling would have done
        total_time = time.time() - _demo_start
        short_poll_requests = int(total_time / 3)  # 3-second interval
        print(f"  --- Comparison ---")
        print(f"  Time elapsed:                 {total_time:.1f}s")
        print(f"  Long poll requests:           {total_requests}")
        print(f"  Short poll would have made:   ~{short_poll_requests} requests (at 3s interval)")
        print(f"  Requests saved:               ~{short_poll_requests - total_requests}")
        print()
        print("  Key takeaway: Long polling achieved the same result (detected all")
        print("  status changes) with a fraction of the requests, and with near-instant")
        print("  detection latency instead of averaging 1.5 seconds.")
        print()
        print("  Next: Chapter 04 — Server-Sent Events (SSE)")
        print("  'What if the server could push MULTIPLE events over a single connection?'")
        print()


if __name__ == "__main__":
    try:
        main()
    except httpx.ConnectError:
        print(f"\n  ERROR: Cannot connect to server at {BASE_URL}")
        print("  Start the server first:")
        print("    uv run uvicorn chapters.ch03_long_polling.server:app --port 8003\n")
        sys.exit(1)

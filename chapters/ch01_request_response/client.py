"""Chapter 01 — Request-Response: FoodDash Client

This client demonstrates the request-response pattern from the caller's side.
Each operation follows the same rhythm:

    1. Serialize data (if any) to JSON
    2. Send HTTP request
    3. BLOCK — wait for the response (nothing else happens)
    4. Deserialize the response
    5. Use the data

We measure each step to show where time is actually spent.

Run with:
    uv run python -m chapters.ch01_request_response.client

Requires the server to be running:
    uv run uvicorn chapters.ch01_request_response.server:app --port 8001
"""

from __future__ import annotations

import json
import sys
import time

import httpx

BASE_URL = "http://localhost:8001"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def print_header(title: str) -> None:
    width = 70
    print()
    print("=" * width)
    print(f"  {title}")
    print("=" * width)


def print_timing(label: str, start: float, end: float) -> None:
    elapsed_ms = (end - start) * 1000
    print(f"  [{elapsed_ms:7.2f} ms] {label}")


def print_http_details(response: httpx.Response) -> None:
    """Print the HTTP-level details that are usually invisible."""
    req = response.request

    print(f"\n  --- HTTP Request ---")
    print(f"  {req.method} {req.url}")
    for name, value in req.headers.items():
        print(f"  {name}: {value}")
    if req.content:
        body = req.content.decode()
        print(f"  Body ({len(req.content)} bytes): {body[:200]}")

    print(f"\n  --- HTTP Response ---")
    print(f"  {response.status_code} {response.reason_phrase}")
    for name, value in response.headers.items():
        print(f"  {name}: {value}")
    body_bytes = len(response.content)
    print(f"  Body ({body_bytes} bytes)")

    # Calculate overhead: headers vs body
    header_size = sum(
        len(k) + len(v) + 4  # ": " + "\r\n"
        for k, v in response.headers.items()
    )
    print(f"\n  Overhead: ~{header_size} bytes of headers for {body_bytes} bytes of body")
    if header_size > body_bytes:
        print(f"  --> Headers are LARGER than the body! This is typical for small JSON payloads.")


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------


def main() -> None:
    print_header("Chapter 01: Request-Response Pattern")
    print("\n  FoodDash Day 1 — Place an order using pure request-response.")
    print("  Every operation: client asks, server answers, client waits.\n")

    # We use httpx.Client() as a context manager to get connection pooling.
    # This means the TCP+TLS handshake happens once, and all subsequent
    # requests reuse the same connection (HTTP keep-alive).
    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:

        # ----- Step 1: Browse the menu (GET) -----
        print_header("Step 1: Browse the Menu (GET /restaurants/rest_01/menu)")
        print("\n  The client sends a GET request. GET is safe (no side effects)")
        print("  and idempotent (same result every time). The client BLOCKS")
        print("  until the full response arrives.\n")

        t_start = time.perf_counter()
        t_serialize = time.perf_counter()  # No body to serialize for GET

        response = client.get("/restaurants/rest_01/menu")

        t_response = time.perf_counter()
        menu_data = response.json()
        t_deserialize = time.perf_counter()

        print_http_details(response)

        print(f"\n  --- Timing Breakdown ---")
        print_timing("Total round trip (network + processing)", t_start, t_response)
        print_timing("Response deserialization (JSON parse)", t_response, t_deserialize)
        print_timing("Total wall clock time", t_start, t_deserialize)

        print(f"\n  --- Menu ---")
        for item in menu_data["items"]:
            price = item["price_cents"] / 100
            print(f"  {item['id']:>8}  ${price:.2f}  {item['name']} — {item['description']}")

        # ----- Step 2: Place an order (POST) -----
        print_header("Step 2: Place an Order (POST /orders)")
        print("\n  POST is NOT idempotent — sending this twice creates two orders.")
        print("  In production, you'd send an Idempotency-Key header to prevent")
        print("  duplicates from double-clicks or retries.\n")

        order_payload = {
            "customer_name": "Alice",
            "restaurant_id": "rest_01",
            "item_ids": ["item_01", "item_02"],  # Classic Burger + Fries
        }

        t_start = time.perf_counter()
        body_bytes = json.dumps(order_payload).encode()
        t_serialize = time.perf_counter()

        response = client.post("/orders", json=order_payload)

        t_response = time.perf_counter()
        order_data = response.json()
        t_deserialize = time.perf_counter()

        print_http_details(response)

        print(f"\n  --- Timing Breakdown ---")
        print_timing("Request serialization (JSON encode)", t_start, t_serialize)
        print_timing("Round trip (network + server processing)", t_serialize, t_response)
        print_timing("Response deserialization (JSON parse)", t_response, t_deserialize)
        print_timing("Total wall clock time", t_start, t_deserialize)

        print(f"\n  --- Order Confirmed ---")
        print(f"  Order ID: {order_data['order_id']}")
        print(f"  Status:   {order_data['status']}")
        print(f"  Total:    ${order_data['total_cents'] / 100:.2f}")
        for item in order_data["items"]:
            print(f"    - {item['name']} x{item['quantity']} (${item['subtotal_cents'] / 100:.2f})")

        order_id = order_data["order_id"]

        # ----- Step 3: Check order status (GET) -----
        print_header("Step 3: Check Order Status (GET /orders/{order_id})")
        print(f"\n  We check the status of order {order_id}.")
        print("  This returns a POINT-IN-TIME snapshot. If the status hasn't changed,")
        print("  we've wasted a full HTTP round trip to learn nothing new.")
        print("  At scale, this is the problem that drives Ch02 (Short Polling).\n")

        t_start = time.perf_counter()

        response = client.get(f"/orders/{order_id}")

        t_response = time.perf_counter()
        status_data = response.json()
        t_deserialize = time.perf_counter()

        print_http_details(response)

        print(f"\n  --- Timing Breakdown ---")
        print_timing("Total round trip", t_start, t_response)
        print_timing("Response deserialization", t_response, t_deserialize)
        print_timing("Total wall clock time", t_start, t_deserialize)

        print(f"\n  --- Order Status ---")
        print(f"  Order ID: {status_data['order_id']}")
        print(f"  Customer: {status_data['customer_name']}")
        print(f"  Status:   {status_data['status']}")
        print(f"  Total:    ${status_data['total_cents'] / 100:.2f}")

        # ----- Summary -----
        print_header("Summary: What We Observed")
        print("""
  1. Every interaction followed the same pattern:
     Client sends request --> blocks --> receives response

  2. The client was IDLE during network transit. The thread did nothing
     while bytes traveled over the wire. This is wasted CPU capacity.

  3. GET requests were safe to retry — they don't change state.
     The POST created a resource — sending it twice would create two orders.

  4. HTTP headers added significant overhead relative to our tiny JSON payloads.
     For real-world APIs with small responses, header bytes often exceed body bytes.

  5. To check if the order status changed, we had to make ANOTHER request.
     The server cannot notify us. This is the limitation that Ch02 addresses.

  Next: Chapter 02 — Short Polling
  "What if we just... keep asking every few seconds?"
""")


if __name__ == "__main__":
    try:
        main()
    except httpx.ConnectError:
        print("\n  ERROR: Cannot connect to server at", BASE_URL)
        print("  Start the server first:")
        print("    uv run uvicorn chapters.ch01_request_response.server:app --port 8001\n")
        sys.exit(1)

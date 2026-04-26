"""Chapter 04 — Server-Sent Events: FoodDash Python Client

This client demonstrates consuming an SSE stream from Python. After connecting
to the SSE endpoint, it receives a continuous stream of events — new orders,
status changes — all over a single HTTP connection.

The educational output shows:
    - The single persistent connection (no reconnections between events)
    - Each event with its type, ID, and data
    - Timing information showing near-instant delivery
    - Reconnection with Last-Event-ID when the connection drops
    - A background thread that places orders and advances statuses

Compare to Ch03's client: that client had to reconnect after EVERY event.
This client connects ONCE and receives ALL events over the same connection.

Run with:
    uv run python -m chapters.ch04_server_sent_events.client

Requires the server to be running:
    uv run uvicorn chapters.ch04_server_sent_events.server:app --port 8004
"""

from __future__ import annotations

import json
import sys
import time
import threading

import httpx

BASE_URL = "http://localhost:8004"


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
# SSE line parser
#
# The SSE protocol is text-based. Each event is a block of field lines
# separated by a blank line. Fields are:
#   data: <payload>
#   event: <type>
#   id: <event-id>
#   retry: <ms>
#   : <comment>
# ---------------------------------------------------------------------------


def parse_sse_events(lines_iter):
    """Parse SSE events from an iterator of text lines.

    Yields dicts with keys: event, data, id, retry.
    Each dict represents one complete SSE event (terminated by a blank line).
    """
    event = {}
    data_lines = []

    for raw_line in lines_iter:
        line = raw_line.rstrip("\n").rstrip("\r")

        if line == "":
            # Blank line = end of event
            if data_lines:
                event["data"] = "\n".join(data_lines)
                yield event
            elif event:
                yield event
            event = {}
            data_lines = []
            continue

        if line.startswith(":"):
            # Comment line — used as heartbeat
            # We can surface these for educational purposes
            comment = line[1:].strip()
            yield {"comment": comment}
            continue

        # Parse field: value
        if ":" in line:
            field, _, value = line.partition(":")
            value = value.lstrip(" ")  # Single leading space is stripped per spec
        else:
            field = line
            value = ""

        if field == "data":
            data_lines.append(value)
        elif field == "event":
            event["event"] = value
        elif field == "id":
            event["id"] = value
        elif field == "retry":
            event["retry"] = value


# ---------------------------------------------------------------------------
# Background activity: place orders and advance them
# ---------------------------------------------------------------------------


def simulate_activity(delay: float = 3.0) -> None:
    """Place orders and advance them in the background.

    This simulates real-world activity: customers placing orders and
    the kitchen advancing their statuses. The SSE client should see
    ALL of these events arrive over its single persistent connection.
    """
    time.sleep(delay)  # Let the SSE connection establish first

    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
        # Place a few orders
        customers = [
            ("Alice", ["item_01", "item_02"]),
            ("Bob", ["item_03"]),
            ("Carol", ["item_01", "item_02", "item_03"]),
        ]

        order_ids = []

        for name, items in customers:
            time.sleep(1.5)
            try:
                resp = client.post("/orders", json={
                    "customer_name": name,
                    "restaurant_id": "rest_01",
                    "item_ids": items,
                })
                if resp.status_code == 201:
                    data = resp.json()
                    order_ids.append(data["order_id"])
                    print_step(
                        f"[ACTIVITY] Placed order for {name}: "
                        f"id={data['order_id']} total=${data['total_cents'] / 100:.2f}"
                    )
                else:
                    print_step(f"[ACTIVITY] Failed to place order: {resp.status_code}")
            except Exception as e:
                print_step(f"[ACTIVITY] Error placing order: {e}")
                return

        # Advance statuses
        time.sleep(2.0)
        for order_id in order_ids:
            for _ in range(3):  # Advance each order 3 times
                time.sleep(1.0)
                try:
                    resp = client.post(f"/orders/{order_id}/advance")
                    if resp.status_code == 200:
                        data = resp.json()
                        print_step(
                            f"[ACTIVITY] Advanced {order_id[:8]}: "
                            f"{data['old_status']} -> {data['new_status']}"
                        )
                    else:
                        print_step(f"[ACTIVITY] Failed to advance: {resp.status_code}")
                        break
                except Exception as e:
                    print_step(f"[ACTIVITY] Error advancing: {e}")
                    return


# ---------------------------------------------------------------------------
# SSE client with reconnection
# ---------------------------------------------------------------------------


def connect_sse(last_event_id: str | None = None) -> None:
    """Connect to the SSE stream and consume events.

    If last_event_id is provided, sends it as the Last-Event-ID header
    so the server can replay any missed events.
    """
    headers = {}
    if last_event_id:
        headers["Last-Event-ID"] = last_event_id
        print_step(f"[SSE] Reconnecting with Last-Event-ID: {last_event_id}")
    else:
        print_step("[SSE] Connecting to event stream...")

    events_received = 0
    current_last_id = last_event_id

    # Use httpx streaming to consume the SSE response
    # The response never completes — we read it line by line as events arrive
    with httpx.Client(base_url=BASE_URL, timeout=None) as client:
        with client.stream("GET", "/orders/stream", headers=headers) as response:
            print_step(
                f"[SSE] Connected! Status: {response.status_code} "
                f"Content-Type: {response.headers.get('content-type', 'unknown')}"
            )
            print_step("[SSE] Listening for events on persistent connection...")
            print()

            # Iterate over lines as they arrive from the server.
            # This is the key difference from regular HTTP: the response
            # body is infinite — it keeps producing lines as events occur.
            for event in parse_sse_events(response.iter_lines()):

                # Handle comment (heartbeat)
                if "comment" in event:
                    print_step(f"[SSE] Heartbeat: :{event['comment']}")
                    continue

                events_received += 1
                event_type = event.get("event", "message")
                event_id = event.get("id", "")
                data_str = event.get("data", "")

                if event_id:
                    current_last_id = event_id

                # Parse the JSON data
                try:
                    data = json.loads(data_str) if data_str else {}
                except json.JSONDecodeError:
                    data = {"raw": data_str}

                # Display the event
                print_step(f"[SSE] Event #{events_received}:")
                print(f"           type:  {event_type}")
                print(f"           id:    {event_id}")

                if event_type == "connected":
                    print(f"           msg:   {data.get('message', '')}")
                elif event_type == "order_placed":
                    print(
                        f"           order: {data.get('order_id', '?')} "
                        f"from {data.get('customer_name', '?')}"
                    )
                    items = data.get("items", [])
                    for item in items:
                        print(
                            f"                  - {item['name']} x{item['quantity']} "
                            f"(${item['subtotal_cents'] / 100:.2f})"
                        )
                    print(f"           total: ${data.get('total_cents', 0) / 100:.2f}")
                elif event_type == "status_changed":
                    print(
                        f"           order: {data.get('order_id', '?')} "
                        f"{data.get('old_status', '?')} -> {data.get('new_status', '?')}"
                    )
                else:
                    print(f"           data:  {data_str[:100]}")

                print()

    return current_last_id


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    global _demo_start
    _demo_start = time.time()

    print_header("Chapter 04: Server-Sent Events (SSE)")
    print()
    print("  FoodDash — The server keeps ONE connection open and streams")
    print("  MULTIPLE events over it. No reconnection between events.")
    print("  Compare to Ch03 where each event required a full reconnect cycle.")
    print()

    # Start background activity (placing orders, advancing statuses)
    print_header("Starting SSE stream + background activity")
    print()
    print("  A background thread will place orders and advance their statuses.")
    print("  The SSE stream will receive ALL events as they happen — watch how")
    print("  multiple events flow over the same connection without reconnection.")
    print()

    activity_thread = threading.Thread(
        target=simulate_activity,
        args=(3.0,),
        daemon=True,
    )
    activity_thread.start()

    # Connect to the SSE stream with reconnection support
    max_retries = 3
    retry_delay = 2.0
    last_event_id = None

    for attempt in range(max_retries + 1):
        try:
            last_event_id = connect_sse(last_event_id)
            break  # Clean disconnect
        except httpx.ConnectError:
            if attempt == 0:
                raise  # First connection failed — server probably isn't running
            print_step(
                f"[SSE] Connection lost. Retrying in {retry_delay}s "
                f"(attempt {attempt + 1}/{max_retries})..."
            )
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30.0)  # Exponential backoff
        except httpx.ReadError:
            print_step("[SSE] Connection closed by server.")
            if attempt < max_retries:
                print_step(
                    f"[SSE] Reconnecting with Last-Event-ID: {last_event_id}"
                )
                time.sleep(retry_delay)
            else:
                print_step("[SSE] Max retries reached.")
        except KeyboardInterrupt:
            print()
            print_step("[SSE] Interrupted by user.")
            break

    # Summary
    print_header("Summary: Server-Sent Events")
    print()
    print("  Key observations:")
    print("  - ONE connection was used for ALL events (no reconnection between events)")
    print("  - Events arrived near-instantly when they occurred on the server")
    print("  - The server pushed events TO us — we didn't have to ask for each one")
    print("  - Heartbeats kept the connection alive through idle periods")
    print("  - Last-Event-ID enables automatic resumption after disconnection")
    print()
    print("  Compare to Ch03 (Long Polling):")
    print("  - Long polling: 1 event per request, reconnect after each event")
    print("  - SSE: Many events per connection, zero reconnection overhead")
    print("  - Both have near-instant latency for individual events")
    print("  - SSE is dramatically more efficient for high-frequency event streams")
    print()
    print("  Next: Chapter 05 — WebSockets")
    print("  'What if BOTH sides need to send messages freely?'")
    print()


if __name__ == "__main__":
    try:
        main()
    except httpx.ConnectError:
        print(f"\n  ERROR: Cannot connect to server at {BASE_URL}")
        print("  Start the server first:")
        print("    uv run uvicorn chapters.ch04_server_sent_events.server:app --port 8004\n")
        sys.exit(1)

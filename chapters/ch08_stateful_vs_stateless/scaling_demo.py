"""Scaling demo — shows what works and what breaks with horizontal scaling.

This is the key educational script for Chapter 08. It starts two server
instances and demonstrates:
  1. Stateless requests (JWT auth) work on BOTH servers
  2. Stateful sessions BREAK when you hit the wrong server
  3. WebSocket connections can't cross server boundaries

Run with:
    uv run python -m chapters.ch08_stateful_vs_stateless.scaling_demo
"""

from __future__ import annotations

import asyncio
import json
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

STATELESS_PORT_A = 8008
STATELESS_PORT_B = 8009
STATEFUL_PORT_A = 8018
STATEFUL_PORT_B = 8019

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
STARTUP_TIMEOUT = 10  # seconds to wait for servers to start


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def header(text: str) -> None:
    width = 70
    print()
    print("=" * width)
    print(f"  {text}")
    print("=" * width)


def subheader(text: str) -> None:
    print(f"\n--- {text} ---\n")


def success(text: str) -> None:
    print(f"  [OK]   {text}")


def failure(text: str) -> None:
    print(f"  [FAIL] {text}")


def info(text: str) -> None:
    print(f"  [INFO] {text}")


def is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_server(module: str, port: int) -> subprocess.Popen:
    """Start a uvicorn server as a subprocess."""
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            module,
            "--port", str(port),
            "--log-level", "warning",
        ],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


def wait_for_server(port: int, timeout: float = STARTUP_TIMEOUT) -> bool:
    """Wait until a server is accepting connections."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_port_open(port):
            return True
        time.sleep(0.2)
    return False


# ---------------------------------------------------------------------------
# Demo sections
# ---------------------------------------------------------------------------

async def demo_stateless(client: httpx.AsyncClient) -> None:
    """Demonstrate that stateless JWT auth works across servers."""
    header("PART 1: STATELESS API (JWT Auth)")
    info("Starting two stateless API servers...")
    info(f"Server A on port {STATELESS_PORT_A}")
    info(f"Server B on port {STATELESS_PORT_B}")

    subheader("Step 1: Login on Server A to get a JWT")
    resp = await client.post(
        f"http://127.0.0.1:{STATELESS_PORT_A}/login",
        json={"name": "Alice", "address": "742 Evergreen Terrace"},
    )
    data = resp.json()
    token = data["token"]
    customer_id = data["customer_id"]
    success(f"Got JWT from Server A (customer: {customer_id})")
    info(f"Token (first 50 chars): {token[:50]}...")

    subheader("Step 2: Use the JWT on Server A")
    resp = await client.get(
        f"http://127.0.0.1:{STATELESS_PORT_A}/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    data = resp.json()
    success(f"Server A recognized us: {data['user']['name']} (pid: {data['served_by_pid']})")

    subheader("Step 3: Use the SAME JWT on Server B")
    resp = await client.get(
        f"http://127.0.0.1:{STATELESS_PORT_B}/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    data = resp.json()
    success(f"Server B recognized us: {data['user']['name']} (pid: {data['served_by_pid']})")

    subheader("Step 4: Place an order on Server A, read it on Server B")
    resp = await client.post(
        f"http://127.0.0.1:{STATELESS_PORT_A}/orders",
        json={"restaurant_id": "rest_01", "item_ids": ["item_01", "item_02"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    order = resp.json()
    order_id = order["order_id"]
    success(f"Order {order_id} placed on Server A (pid: {order['served_by_pid']})")

    # Note: In a real system with a shared DB, this would work. With our
    # in-memory DB, each process has its own store, so we demonstrate the
    # concept with the JWT verification instead.
    info("In production with a shared database, you could read this order from Server B.")
    info("The key insight: JWT verification needs NO server-side state.")

    subheader("Stateless Verdict")
    success("JWT works on ANY server. No sticky sessions needed.")
    success("Load balancer can use round-robin. Scaling is trivial.")
    success("If Server A dies, Server B handles all requests seamlessly.")


async def demo_stateful(client: httpx.AsyncClient) -> None:
    """Demonstrate that server-side sessions break across servers."""
    header("PART 2: STATEFUL SESSIONS (In-Memory Session Store)")
    info("Starting two stateful session servers...")
    info(f"Server A on port {STATEFUL_PORT_A}")
    info(f"Server B on port {STATEFUL_PORT_B}")

    subheader("Step 1: Login on Server A to create a session")
    resp = await client.post(
        f"http://127.0.0.1:{STATEFUL_PORT_A}/login",
        json={"name": "Bob", "address": "123 Fake Street"},
    )
    data = resp.json()
    session_id = data["session_id"]
    server_a_id = data["server_id"]
    success(f"Session created on {server_a_id}: {session_id}")

    subheader("Step 2: Use the session on Server A (same server)")
    resp = await client.get(
        f"http://127.0.0.1:{STATEFUL_PORT_A}/me",
        cookies={"session_id": session_id},
    )
    if resp.status_code == 200:
        data = resp.json()
        success(f"Server A found our session: user={data['user']['name']}")
    else:
        failure(f"Unexpected error on Server A: {resp.status_code}")

    subheader("Step 3: Use the SAME session on Server B (DIFFERENT server)")
    info("This is the moment of truth. The session exists in Server A's memory.")
    info("Server B has never seen this session ID.")
    print()

    resp = await client.get(
        f"http://127.0.0.1:{STATEFUL_PORT_B}/me",
        cookies={"session_id": session_id},
    )
    if resp.status_code == 401:
        error_detail = resp.json().get("detail", {})
        failure(f"Server B rejected us: {error_detail.get('error', 'Session not found')}")
        info(f"Server B says: \"{error_detail.get('explanation', 'N/A')}\"")
    else:
        info(f"Unexpected response: {resp.status_code} {resp.json()}")

    subheader("Step 4: Check session state on both servers")
    resp_a = await client.get(f"http://127.0.0.1:{STATEFUL_PORT_A}/debug/sessions")
    resp_b = await client.get(f"http://127.0.0.1:{STATEFUL_PORT_B}/debug/sessions")
    sessions_a = resp_a.json()
    sessions_b = resp_b.json()
    info(f"Server A ({sessions_a['server_id']}): {sessions_a['total_sessions']} session(s)")
    for sid, s in sessions_a.get("sessions", {}).items():
        info(f"  - {sid}: user={s['name']}, age={s['age_seconds']}s")
    info(f"Server B ({sessions_b['server_id']}): {sessions_b['total_sessions']} session(s)")
    if sessions_b["total_sessions"] == 0:
        info("  - (empty — no sessions)")

    subheader("Stateful Verdict")
    failure("Session only works on the server that created it.")
    failure("Round-robin load balancing BREAKS session-based auth.")
    failure("If Server A dies, ALL its sessions are permanently lost.")
    info("Solutions: sticky sessions, external session store (Redis), or switch to JWT.")


async def demo_websocket_problem() -> None:
    """Demonstrate the WebSocket cross-server problem conceptually."""
    header("PART 3: THE WEBSOCKET PROBLEM (Conceptual)")
    info("This section explains the WebSocket scaling problem from Ch05.")
    print()

    print("  Scenario: FoodDash chat between customer Alice and driver Dave")
    print()
    print("  With ONE server:")
    print("    Alice (customer) ---WebSocket---> Server 1 ---WebSocket---> Dave (driver)")
    print("    Alice sends 'Where are you?'")
    print("    Server 1 has BOTH connections in memory.")
    print("    Server 1 forwards the message to Dave.")
    success("Message delivered.")
    print()
    print("  With TWO servers (round-robin load balancer):")
    print("    Alice (customer) ---WebSocket---> Server 1")
    print("    Dave  (driver)   ---WebSocket---> Server 2")
    print("    Alice sends 'Where are you?'")
    print("    Server 1 iterates over its local connections...")
    print("    Dave is NOT on Server 1. He's on Server 2.")
    failure("Message LOST. Dave never receives it.")
    print()
    print("  Why? WebSocket connections are IN-PROCESS STATE.")
    print("  Server 1's connection list: [Alice]")
    print("  Server 2's connection list: [Dave]")
    print("  Neither server knows about the other's connections.")
    print()

    subheader("The Same Problem Hits Other Stateful Patterns")
    print("  SSE (Ch04):  Customer's event stream is on Server 1.")
    print("               Kitchen update hits Server 2.")
    print("               Server 2 has no subscriber to notify.")
    failure("Customer sees stale data.")
    print()
    print("  Long Polling (Ch03): Client's parked connection is on Server 1.")
    print("                       Event triggers on Server 2.")
    print("                       Server 2 can't wake Server 1's connection.")
    failure("Client times out, polls again, maybe hits the right server.")
    print()
    print("  Pub/Sub (Ch07): Server 1's broker has subscribers [A, C].")
    print("                  Server 2's broker has subscribers [B, D].")
    print("                  Event published on Server 1 only reaches A and C.")
    failure("Split brain: B and D never see the event.")


async def demo_retrospective() -> None:
    """Map all Ch01-07 patterns to their scaling characteristics."""
    header("PART 4: RETROSPECTIVE — All Patterns at a Glance")
    print()
    print("  Pattern             │ State Model  │ Scales Horizontally?  │ Fix")
    print("  ────────────────────┼──────────────┼───────────────────────┼──────────────────────")
    print("  Ch01 Request-Resp   │ Stateless    │ Yes (trivially)       │ N/A — already works")
    print("  Ch02 Short Polling  │ Stateless    │ Yes (trivially)       │ N/A — already works")
    print("  Ch03 Long Polling   │ Stateful     │ No (parked conns)     │ Sticky sessions")
    print("  Ch04 SSE            │ Stateful     │ No (open streams)     │ Redis pub/sub bridge")
    print("  Ch05 WebSockets     │ Stateful     │ No (connection state) │ Redis pub/sub bridge")
    print("  Ch06 Push Notif     │ Stateless*   │ Yes (state in FCM)    │ N/A — outsourced")
    print("  Ch07 Pub/Sub        │ Stateful     │ No (split brain)      │ External broker")
    print()
    info("* Push Notifications are stateless from YOUR server's perspective.")
    info("  The state lives in Google/Apple's infrastructure.")
    print()

    subheader("The Fundamental Trade-off")
    print("  STATEFUL:")
    print("    + Lower latency (state is local, no network hop)")
    print("    + Simpler code (just use a dict)")
    print("    + No external dependencies")
    print("    - Can't scale horizontally without engineering effort")
    print("    - Server death = lost state")
    print("    - Uneven load distribution")
    print()
    print("  STATELESS:")
    print("    + Horizontal scaling is trivial")
    print("    + Server death is invisible to clients")
    print("    + Even load distribution")
    print("    - Higher latency (external store hop)")
    print("    - More infrastructure (Redis, etc.)")
    print("    - Serialization overhead")
    print()

    subheader("Memory Impact")
    print("  Stateful WebSocket server with 100K connections:")
    print("    ~30 KB per connection x 100,000 = 3 GB in RAM")
    print("    Plus message buffers, subscriber lists, pending events")
    print("    Server maxes out its RAM; you CANNOT add connections without a bigger box.")
    print()
    print("  Stateless approach (connections + Redis pub/sub):")
    print("    Each server holds only its local connections (~1 GB with 33K each)")
    print("    Redis holds shared state (channel subscriptions, message routing)")
    print("    Add a 4th server to handle the next 33K connections.")
    print("    Redis cluster can scale independently if needed.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    processes: list[subprocess.Popen] = []

    header("Chapter 08: Stateful vs Stateless — Scaling Demo")
    info("This demo starts 4 server instances and shows what works vs what breaks.")
    info("Press Ctrl+C to stop all servers and exit.\n")

    try:
        # Start all four servers
        info("Starting servers...")
        servers = [
            ("chapters.ch08_stateful_vs_stateless.stateless_api:app", STATELESS_PORT_A),
            ("chapters.ch08_stateful_vs_stateless.stateless_api:app", STATELESS_PORT_B),
            ("chapters.ch08_stateful_vs_stateless.stateful_session:app", STATEFUL_PORT_A),
            ("chapters.ch08_stateful_vs_stateless.stateful_session:app", STATEFUL_PORT_B),
        ]

        for module, port in servers:
            if is_port_open(port):
                info(f"Port {port} already in use — skipping server start")
                continue
            proc = start_server(module, port)
            processes.append(proc)

        # Wait for all servers
        for module, port in servers:
            if wait_for_server(port):
                success(f"Server on port {port} is ready")
            else:
                failure(f"Server on port {port} failed to start within {STARTUP_TIMEOUT}s")
                info("Try running the servers manually — see README.md for commands.")
                return

        print()
        info("All 4 servers are running. Starting demonstrations...\n")
        await asyncio.sleep(0.5)

        async with httpx.AsyncClient(timeout=10) as client:
            # Part 1: Stateless works
            await demo_stateless(client)
            await asyncio.sleep(0.5)

            # Part 2: Stateful breaks
            await demo_stateful(client)
            await asyncio.sleep(0.5)

        # Part 3: WebSocket problem (conceptual)
        await demo_websocket_problem()
        await asyncio.sleep(0.5)

        # Part 4: Retrospective
        await demo_retrospective()

        header("DEMO COMPLETE")
        info("Key takeaway: if your communication pattern holds state in the server")
        info("process, horizontal scaling requires additional engineering.")
        info("")
        info("Stateless patterns (Ch01, Ch02, Ch06): scale freely.")
        info("Stateful patterns (Ch03, Ch04, Ch05, Ch07): need sticky sessions,")
        info("external state stores, or architectural redesign.")
        print()

    except KeyboardInterrupt:
        print("\n\nShutting down...")

    finally:
        for proc in processes:
            proc.terminate()
        for proc in processes:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        if processes:
            info(f"Stopped {len(processes)} server process(es).")


if __name__ == "__main__":
    asyncio.run(main())

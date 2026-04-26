"""
Connection Cost Benchmark -- Memory per Connection by Pattern

Calculates theoretical memory cost per connection for each communication
pattern and extrapolates to 10K, 100K, and 1M concurrent connections.

These are estimates based on typical implementations (Python asyncio / uvicorn).
Actual memory depends on your runtime, OS, and application state.

Run: uv run python -m benchmarks.connection_cost
"""

from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Memory cost models (bytes per connection)
# ---------------------------------------------------------------------------

@dataclass
class ConnectionProfile:
    name: str
    description: str
    # Memory breakdown (bytes)
    tcp_socket_buffer: int       # OS-level send/recv buffers
    tls_state: int               # TLS session state (if applicable)
    http_overhead: int           # HTTP parsing state, headers buffer
    application_state: int       # App-level per-connection state
    coroutine_stack: int         # async coroutine / greenlet stack
    notes: str = ""

    @property
    def total_bytes(self) -> int:
        return (
            self.tcp_socket_buffer
            + self.tls_state
            + self.http_overhead
            + self.application_state
            + self.coroutine_stack
        )

    @property
    def total_kb(self) -> float:
        return self.total_bytes / 1024


PROFILES = [
    ConnectionProfile(
        name="Short Polling",
        description="No persistent connection. Cost is per-request, not per-user.",
        tcp_socket_buffer=0,         # Connection closed between polls
        tls_state=0,                 # Re-negotiated each request (or session ticket ~0.3KB)
        http_overhead=0,             # Freed after response
        application_state=0,         # Stateless
        coroutine_stack=0,           # Request handler exits
        notes=(
            "Short polling has near-zero IDLE memory cost because connections "
            "are not held open. The cost is per-REQUEST (~2-4 KB for the duration "
            "of each request). With 10K users polling every 2s, you have ~5K "
            "concurrent requests, not 10K persistent connections."
        ),
    ),
    ConnectionProfile(
        name="Long Polling",
        description="Held HTTP connection + suspended coroutine waiting for event.",
        tcp_socket_buffer=4096 + 4096,  # 4KB send + 4KB recv (reduced from default 128KB)
        tls_state=320,                  # TLS 1.3 session state (~0.3KB)
        http_overhead=1024,             # Parsed request headers kept in memory
        application_state=256,          # Future/Event object + order_id mapping
        coroutine_stack=4096,           # Python coroutine suspended at await
        notes=(
            "Each long-poll client holds a TCP connection open for up to 25 seconds. "
            "The coroutine is suspended at `await future`, consuming stack space. "
            "TCP buffers dominate. Linux `SO_RCVBUF`/`SO_SNDBUF` can be tuned down "
            "since the server only sends a small response."
        ),
    ),
    ConnectionProfile(
        name="SSE",
        description="Persistent HTTP connection streaming events. Server -> client only.",
        tcp_socket_buffer=4096 + 8192,  # 4KB recv (idle) + 8KB send (event buffer)
        tls_state=320,                  # TLS 1.3 session
        http_overhead=1024,             # HTTP response state (chunked encoding ctx)
        application_state=1024,         # Event buffer (last N events for replay)
        coroutine_stack=4096,           # Generator/async generator suspended
        notes=(
            "SSE connections are long-lived HTTP responses. The send buffer is "
            "slightly larger than long polling because the server actively streams "
            "data. The event buffer stores recent events for `Last-Event-ID` replay. "
            "No client-to-server data after initial request, so recv buffer is minimal."
        ),
    ),
    ConnectionProfile(
        name="WebSocket",
        description="Persistent bidirectional connection with frame parsing state.",
        tcp_socket_buffer=8192 + 8192,  # 8KB send + 8KB recv (bidirectional traffic)
        tls_state=320,                  # TLS 1.3 session
        http_overhead=512,              # Post-upgrade, HTTP state is minimal
        application_state=2048,         # Chat history buffer, user state, room membership
        coroutine_stack=4096,           # Two coroutines: reader + writer
        notes=(
            "WebSocket connections carry bidirectional traffic, so both send and "
            "recv buffers are active. Application state is larger because WebSocket "
            "connections typically maintain richer state (chat rooms, subscriptions, "
            "user presence). Frame parsing adds ~200 bytes for mask/opcode tracking."
        ),
    ),
]

# Concurrent connection scale points
SCALE_POINTS = [10_000, 100_000, 1_000_000]


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_bytes(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    elif b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    elif b < 1024 * 1024 * 1024:
        return f"{b / (1024 * 1024):.1f} MB"
    else:
        return f"{b / (1024 * 1024 * 1024):.2f} GB"


def bar_chart(value: float, max_value: float, width: int = 30) -> str:
    if max_value == 0:
        return ""
    filled = int((value / max_value) * width)
    return "#" * filled + "." * (width - filled)


def main() -> None:
    print()
    print("=" * 88)
    print("  CONNECTION MEMORY COST BY PATTERN")
    print("  Theoretical estimates for Python asyncio / uvicorn")
    print("=" * 88)

    # -- Per-connection breakdown --
    print()
    print("  PER-CONNECTION MEMORY BREAKDOWN")
    print("  " + "-" * 84)
    print(
        f"  {'Pattern':<16} {'TCP Buf':>8} {'TLS':>8} {'HTTP':>8} "
        f"{'App State':>10} {'Coroutine':>10} {'TOTAL':>10}"
    )
    print("  " + "-" * 84)

    max_total = max(p.total_bytes for p in PROFILES)
    for p in PROFILES:
        bar = bar_chart(p.total_bytes, max_total, 20)
        print(
            f"  {p.name:<16} "
            f"{format_bytes(p.tcp_socket_buffer):>8} "
            f"{format_bytes(p.tls_state):>8} "
            f"{format_bytes(p.http_overhead):>8} "
            f"{format_bytes(p.application_state):>10} "
            f"{format_bytes(p.coroutine_stack):>10} "
            f"{format_bytes(p.total_bytes):>10}  {bar}"
        )

    # -- Scale projection table --
    print()
    print("  MEMORY AT SCALE (total RAM for connections only)")
    print("  " + "-" * 84)
    header = f"  {'Pattern':<16}"
    for scale in SCALE_POINTS:
        header += f"  {scale:>10,} conn"
    print(header)
    print("  " + "-" * 84)

    for p in PROFILES:
        row = f"  {p.name:<16}"
        for scale in SCALE_POINTS:
            if p.name == "Short Polling":
                # Short polling: estimate concurrent requests, not connections
                # With 2s interval, ~50% of users have an active request at any moment
                # (request takes ~50ms, so actually only 2.5% are concurrent)
                concurrent_fraction = 0.025
                mem = int(p.total_bytes + 3072) * int(scale * concurrent_fraction)
                row += f"  {format_bytes(mem):>14}"
            else:
                mem = p.total_bytes * scale
                row += f"  {format_bytes(mem):>14}"
        print(row)

    # -- Context: what fits on common server sizes --
    print()
    print("  SERVER SIZING REFERENCE")
    print("  " + "-" * 84)
    server_sizes = [
        ("Small (4 GB)", 4 * 1024**3),
        ("Medium (16 GB)", 16 * 1024**3),
        ("Large (64 GB)", 64 * 1024**3),
        ("XL (256 GB)", 256 * 1024**3),
    ]

    # Reserve 30% of RAM for OS + app code + other overhead
    usable_fraction = 0.70

    print(f"  {'Server':<20}", end="")
    for p in PROFILES:
        print(f"  {p.name:>16}", end="")
    print()
    print("  " + "-" * 84)

    for server_name, total_ram in server_sizes:
        usable = int(total_ram * usable_fraction)
        row = f"  {server_name:<20}"
        for p in PROFILES:
            if p.total_bytes == 0:
                # Short polling: use concurrent request cost
                cost = 3072  # ~3KB per active request
                max_conn = usable // max(cost, 1)
            else:
                max_conn = usable // max(p.total_bytes, 1)
            if max_conn >= 1_000_000:
                row += f"  {max_conn / 1_000_000:>13.1f}M"
            elif max_conn >= 1_000:
                row += f"  {max_conn / 1_000:>13.0f}K"
            else:
                row += f"  {max_conn:>14,}"
        print(row)

    print()
    print(f"  (Assumes {usable_fraction:.0%} of RAM available for connections; "
          f"rest for OS + app code)")

    # -- Notes --
    print()
    print("  NOTES")
    print("  " + "-" * 84)
    for p in PROFILES:
        if p.notes:
            print(f"  {p.name}:")
            # Word wrap notes at ~75 chars
            words = p.notes.split()
            line = "    "
            for word in words:
                if len(line) + len(word) + 1 > 80:
                    print(line)
                    line = "    " + word
                else:
                    line += " " + word if line.strip() else "    " + word
            if line.strip():
                print(line)
            print()

    # -- Key insight --
    print("  " + "-" * 84)
    print("  KEY INSIGHT:")
    print("  Short polling trades memory for bandwidth. It uses almost no server")
    print("  memory (connections are transient) but generates enormous request volume.")
    print("  Persistent patterns (long poll, SSE, WebSocket) trade bandwidth for memory")
    print("  by holding connections open. At scale, memory is usually cheaper than")
    print("  bandwidth + CPU for handling millions of redundant HTTP requests.")
    print("  " + "-" * 84)
    print()
    print("=" * 88)
    print("  NOTE: These are theoretical estimates. Actual memory depends on your")
    print("  runtime (Python, Go, Rust), OS TCP tuning, and application logic.")
    print("=" * 88)
    print()


if __name__ == "__main__":
    main()

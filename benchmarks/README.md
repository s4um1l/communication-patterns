# Benchmarks

Simulated benchmarks that model the theoretical behavior of different communication patterns. These are **not** live server benchmarks -- they calculate expected performance based on each pattern's characteristics to build intuition about trade-offs.

## Available Benchmarks

### 1. Polling vs SSE vs WebSocket (`polling_vs_sse_vs_ws.py`)

Simulates 100 orders going through status changes and compares four patterns:
- **Short Polling (2s interval)**: Periodic HTTP requests
- **Long Polling**: Held connections with instant response on change
- **Server-Sent Events**: Persistent HTTP stream
- **WebSocket**: Persistent bidirectional connection

Measures: detection latency, total bytes transferred, total requests made.

```bash
uv run python -m benchmarks.polling_vs_sse_vs_ws
```

### 2. Connection Cost (`connection_cost.py`)

Calculates theoretical memory cost per connection for each pattern and extrapolates to 10K, 100K, and 1M concurrent connections.

```bash
uv run python -m benchmarks.connection_cost
```

## Interpreting Results

These simulations model **ideal conditions** (no packet loss, no server processing delay beyond what's modeled). Real-world performance depends on network conditions, server implementation, and load. The goal is to build intuition about the **relative** costs of each pattern, not to predict exact production numbers.

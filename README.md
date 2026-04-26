# Communication Patterns — From First Principles to Principal-Level Mastery

> **How do systems talk to each other?** This repo answers that question by building a food delivery platform ("FoodDash") from scratch, evolving from the simplest pattern (request-response) to sophisticated architectures (pub/sub, multiplexing, sidecars). Each chapter introduces a new pattern *because the previous one breaks* — you'll feel the pain before learning the cure.

## Why This Exists

Most resources list communication patterns as isolated concepts. But in practice, you don't pick a pattern from a menu — you evolve toward one because your current approach hit a wall. This repo teaches the **intuition** behind each pattern through a single case study that grows chapter by chapter.

Every chapter includes:
- **The Scene** — a narrative that makes you feel the problem before the solution
- **Systems Constraints Analysis** — CPU, memory, network I/O, latency breakdowns
- **Working Code** — runnable servers and clients, not pseudocode
- **Interactive Visuals** — open `visual.html` in a browser to *see* the pattern in action
- **Principal-Level Depth** — edge cases, failure modes, production gotchas

## The Learning Path

```
Ch 00: Foundations ──────────────────── TCP, HTTP, sockets from scratch
  │
  ▼
Ch 01: Request-Response ────────────── Customer places an order
  │  "But how do I check if my food is ready?"
  ▼
Ch 02: Short Polling ───────────────── Refresh every 5 seconds
  │  "10K users × 2s = 5K wasted requests/sec"
  ▼
Ch 03: Long Polling ────────────────── Hold the connection open
  │  "Better, but the server is holding 10K threads"
  ▼
Ch 04: Server-Sent Events ─────────── Stream updates to the browser
  │  "Great for one-way, but the driver needs to TALK to the customer"
  ▼
Ch 05: WebSockets ──────────────────── Full-duplex chat
  │  "Works perfectly... until the customer closes the app"
  ▼
Ch 06: Push Notifications ─────────── Reach users outside the app
  │  "Now 5 services all need to know about 'order placed'"
  ▼
Ch 07: Pub/Sub ─────────────────────── Decoupled event-driven architecture
  │
  ├── Ch 08: Stateful vs Stateless ── What breaks when you add a second server?
  ├── Ch 09: Multiplexing ─────────── One connection, many streams
  └── Ch 10: Sidecar Pattern ──────── Extract auth/logging from every service
  │
  ▼
Ch 11: Synthesis ───────────────────── Decision framework: which pattern for which problem?
```

## Quick Start

```bash
# Clone and install
git clone <this-repo>
cd communication_patterns
uv sync

# Run any chapter's server
uv run python -m chapters.ch01_request_response.server

# In another terminal, run the client
uv run python -m chapters.ch01_request_response.client

# Open interactive visuals
open chapters/ch01_request_response/visual.html
```

### Optional dependencies

Some chapters need extra packages. Install what you need:

```bash
uv sync --extra websockets   # Ch 05: WebSockets
uv sync --extra sse          # Ch 04: Server-Sent Events
uv sync --extra push         # Ch 06: Push Notifications
uv sync --all-extras         # Everything
```

## Prerequisites

- Python 3.12+
- Basic understanding of HTTP (we'll deepen this in Ch 00)
- Curiosity about why systems are built the way they are

## Project Structure

```
shared/              FoodDash domain models — same business, evolving patterns
chapters/
  ch00_foundations/   TCP and HTTP from raw sockets
  ch01_..ch11_../    One pattern per chapter, each building on the last
exercises/           Per-chapter exercises with solutions
benchmarks/          Comparative benchmarks across patterns
```

## The Constraints Lens

Every chapter analyzes the pattern through five system constraints:

| Constraint | What We Measure |
|---|---|
| **CPU** | Encode/decode cost, context switching, idle time |
| **Memory** | Per-connection state, buffers, session storage |
| **Network I/O** | Bandwidth, connection count, header overhead |
| **Latency** | Where time goes: propagation, serialization, queueing |
| **Bottleneck Shift** | Which constraint relaxes — and which tightens |

This builds the muscle to reason about trade-offs like a principal engineer.

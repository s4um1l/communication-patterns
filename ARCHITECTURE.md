# Architecture — How FoodDash Communication Patterns Evolve

## The Big Picture

```
┌──────────────────────────────────────────────────────────────────────┐
│                         FoodDash Platform                            │
│                                                                      │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐      │
│  │ Customer  │    │Restaurant│    │  Driver   │    │  Admin   │      │
│  │   App     │    │Dashboard │    │   App     │    │  Panel   │      │
│  └────┬─────┘    └────┬─────┘    └────┬─────┘    └────┬─────┘      │
│       │               │               │               │              │
│  ─────┼───────────────┼───────────────┼───────────────┼──────────── │
│       │               │               │               │              │
│       ▼               ▼               ▼               ▼              │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │                    API Gateway (Stateless)                   │    │
│  │              Ch01: Request-Response + Ch08: Stateless        │    │
│  └────────────────────────┬────────────────────────────────────┘    │
│                           │                                          │
│  ┌────────────────────────┼────────────────────────────────────┐    │
│  │                    Event Bus (Pub/Sub)                       │    │
│  │                      Ch07: Pub/Sub                           │    │
│  └──┬──────────┬──────────┬──────────┬──────────┬─────────────┘    │
│     │          │          │          │          │                    │
│     ▼          ▼          ▼          ▼          ▼                    │
│  ┌──────┐ ┌───────┐ ┌────────┐ ┌────────┐ ┌────────┐              │
│  │Order │ │Kitchen│ │Billing │ │Driver  │ │Notif.  │              │
│  │Svc   │ │Svc    │ │Svc     │ │Match   │ │Svc     │              │
│  └──┬───┘ └───────┘ └────────┘ └───┬────┘ └───┬────┘              │
│     │                               │          │                    │
│     │  Ch02-03: Polling             │          │ Ch06: Push         │
│     │  Ch04: SSE (dashboard)        │          │                    │
│     │  Ch05: WebSocket (chat)       │          │                    │
│     │  Ch09: Multiplexed            │          │                    │
│     │                               │          │                    │
│  ┌──┴──────────────────────────────┴──────────┴──────────────┐     │
│  │              Sidecar Proxies (per service)                 │     │
│  │         Ch10: Auth, Logging, Rate Limiting                 │     │
│  └────────────────────────────────────────────────────────────┘     │
└──────────────────────────────────────────────────────────────────────┘
```

## Pattern Relationships

```
                    Unidirectional ◄──────────────► Bidirectional
                         │                               │
     Request-Response ◄──┤                               ├──► WebSockets
          (Ch01)         │                               │      (Ch05)
                         │                               │
      Short Polling ◄────┤                               │
          (Ch02)         │                               │
                         │                               │
       Long Polling ◄────┤                               │
          (Ch03)         │                               │
                         │                               │
            SSE ◄────────┘                               │
          (Ch04)                                         │
                                                         │
                    Point-to-Point ◄─────────────► Fan-out
                         │                            │
     Request-Response ◄──┤                            ├──► Pub/Sub (Ch07)
     Push (Ch06)    ◄────┘                            └──► SSE broadcast (Ch04)
```

## Constraint Pressure Map

How each pattern shifts the burden across system resources:

```
Pattern              CPU     Memory    Network    Latency
─────────────────────────────────────────────────────────
Request-Response     ●○○○    ●○○○      ●○○○       ●●●○
Short Polling        ●●○○    ●○○○      ●●●●       ●●●○
Long Polling         ●○○○    ●●●○      ●○○○       ●●○○
SSE                  ●○○○    ●●○○      ●○○○       ●○○○
WebSockets           ●●○○    ●●●○      ●○○○       ●○○○
Push                 ●○○○    ●○○○      ●○○○       ●●○○
Pub/Sub (broker)     ●●○○    ●●○○      ●●○○       ●○○○

●○○○ = low pressure    ●●●● = high pressure
```

## How to Read This Repo

**Sequential (recommended):** Start at Ch 00 and follow the story. Each chapter's problem naturally motivates the next chapter's solution.

**By need:** Jump to the pattern you're evaluating. Each chapter is self-contained with its own README, code, and visuals. The "Bridge to Next Chapter" section links forward, and "Why the Previous Approach Fails" links backward.

**By constraint:** If you're debugging a specific bottleneck (e.g., "we're running out of connections"), use the constraint pressure map above to find the relevant patterns.

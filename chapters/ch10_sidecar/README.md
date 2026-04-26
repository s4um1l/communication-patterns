# Chapter 10 -- Sidecar Pattern

## The Scene

You have five microservices in FoodDash: order, kitchen, billing, driver, and notification. Each one has the same three pieces of middleware copy-pasted into it:

```python
# This code exists in ALL FIVE services. Copy-pasted, slightly different in each.

# 1. Auth verification
@app.middleware("http")
async def verify_auth(request, call_next):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not verify_jwt(token):                  # <-- copied into every service
        return JSONResponse(status_code=401)
    request.state.user = decode_jwt(token)
    return await call_next(request)

# 2. Request logging
@app.middleware("http")
async def log_request(request, call_next):
    start = time.time()
    response = await call_next(request)
    elapsed = time.time() - start
    logger.info(f"{request.method} {request.url} {response.status_code} {elapsed:.3f}s")
    return response

# 3. Rate limiting
@app.middleware("http")
async def rate_limit(request, call_next):
    client_ip = request.client.host
    if not token_bucket.consume(client_ip):    # <-- copied into every service
        return JSONResponse(status_code=429)
    return await call_next(request)
```

When you change the auth logic -- say migrating from HS256 to RS256 JWT verification -- you deploy five services. When you add distributed tracing, you touch five codebases. When the rate limiter has a bug that lets traffic spikes through, you patch five services. When you add a sixth service, you copy-paste the middleware again.

This is the **cross-cutting concern** problem. Auth, logging, rate limiting, metrics, tracing, retries, circuit breaking -- they are concerns that cut across every service. They are not business logic. They should not live in the business logic.

There must be a better way.

---

## The Pattern -- Sidecar

A **sidecar** is a helper process that runs alongside your service on the same host (or in the same Kubernetes pod). All network traffic flows through the sidecar before reaching your service. The sidecar handles cross-cutting concerns transparently -- your service code contains only business logic.

```
WITHOUT sidecar:

  Client --> [Order Service: auth + logging + rate-limit + business logic]
  Client --> [Kitchen Service: auth + logging + rate-limit + business logic]
  Client --> [Billing Service: auth + logging + rate-limit + business logic]
  Client --> [Driver Service: auth + logging + rate-limit + business logic]
  Client --> [Notification Service: auth + logging + rate-limit + business logic]

  5 services x 3 middleware = 15 copies of cross-cutting code


WITH sidecar:

  Client --> [Sidecar: auth + logging + rate-limit] --> [Order Service: business logic]
  Client --> [Sidecar: auth + logging + rate-limit] --> [Kitchen Service: business logic]
  Client --> [Sidecar: auth + logging + rate-limit] --> [Billing Service: business logic]
  Client --> [Sidecar: auth + logging + rate-limit] --> [Driver Service: business logic]
  Client --> [Sidecar: auth + logging + rate-limit] --> [Notification Service: business logic]

  5 identical sidecars (same binary, same config) + 5 clean services
```

The sidecar is deployed as a **separate process** but on the **same host** as the service. They share a network namespace (in Kubernetes: same pod). The service listens on `localhost:8010`, the sidecar listens on `localhost:8011` (the external-facing port). Traffic flows:

```
Inbound request:
  Internet --> Load Balancer --> Sidecar (port 8011)
                                   |
                                   +--> Auth check (reject 401 if invalid)
                                   +--> Rate limit check (reject 429 if throttled)
                                   +--> Log: "POST /orders 200 45ms"
                                   |
                                   +--> Forward to Service (localhost:8010)
                                   |
                                   +--> Service processes request (pure business logic)
                                   |
                                   +--> Response flows back through sidecar
                                   +--> Log: response timing, status
                                   |
  Internet <-- Load Balancer <-- Sidecar <-- Service
```

The service never sees unauthenticated requests. It never implements rate limiting. It never logs request metadata. It just does its job.

### Why Not a Shared Library?

You could put auth, logging, and rate limiting in a shared library that all services import. This works for monolingual systems (all Python, all Go). But:

1. **Language lock-in**: If one team writes in Python and another in Go, the shared library must be maintained in both languages. Or you force everyone onto one language.

2. **Version coupling**: Upgrading the library requires redeploying every service that uses it. If service A needs the new auth logic and service B has not upgraded yet, you have version skew.

3. **Process-level isolation**: A bug in the logging library that causes a memory leak crashes the business service. With a sidecar, the sidecar crashes but the service continues (or vice versa -- the failure domains are separate processes).

4. **Operational independence**: The platform team owns the sidecar. The service teams own their services. Neither needs to coordinate releases. The sidecar can be upgraded independently across the fleet.

The sidecar is a shared library extracted into a separate process. The cost is an extra network hop (localhost). The benefit is operational independence, language agnosticism, and process isolation.

---

## Service Mesh -- Generalizing the Sidecar

When every service in your fleet has a sidecar proxy, you have a **service mesh**. The sidecars form a network of proxies that handle all inter-service communication. The mesh provides:

```
Service A --[sidecar A]--> network -->[sidecar B]--> Service B

  Sidecar A (outbound):            Sidecar B (inbound):
    - mTLS encryption               - mTLS termination
    - Retry with backoff             - Auth verification
    - Circuit breaking               - Rate limiting
    - Load balancing                 - Request logging
    - Distributed tracing (inject)   - Distributed tracing (propagate)
```

### Istio

Istio is the most widely deployed service mesh. Architecture:

- **Data plane**: Envoy sidecar proxies (one per service pod). All traffic flows through Envoy.
- **Control plane**: Istiod -- configures all Envoy proxies, manages certificates, collects telemetry.

```
                          +-------------+
                          |   Istiod    |  <-- Control plane
                          | (config,    |      Pushes config to all Envoys
                          |  certs,     |      Manages mTLS certificates
                          |  telemetry) |      Collects metrics
                          +------+------+
                                 |
                    +------------+------------+
                    |                         |
              +-----+------+          +------+-----+
              |   Envoy    |          |   Envoy    |
              |  (sidecar) |--------->|  (sidecar) |
              +-----+------+  mTLS   +------+-----+
                    |                         |
              +-----+------+          +------+-----+
              |  Order     |          |  Kitchen   |
              |  Service   |          |  Service   |
              +------------+          +------------+
```

Key capabilities:
- **mTLS everywhere**: Every service-to-service call is encrypted. Certificates are automatically rotated. Zero application code changes.
- **Traffic management**: Canary deployments (route 5% of traffic to v2), traffic mirroring (copy production traffic to staging), fault injection (test resilience).
- **Observability**: Distributed traces, metrics (latency, error rate, throughput), access logs -- all without instrumenting application code.

### Linkerd

Linkerd takes a simpler approach: smaller footprint, less configuration, fewer features. Written in Rust (linkerd2-proxy) for performance. Installs in under 60 seconds. The trade-off: less flexibility than Istio, but dramatically less operational complexity.

---

## Variants

The sidecar is one of three related patterns for extending service behavior without modifying the service itself:

### Sidecar

Runs alongside the service. Handles cross-cutting concerns for inbound AND outbound traffic.

```
Client --> [Sidecar] --> [Service]
                 ^
                 |
         Auth, logging, rate limiting,
         mTLS, retries, circuit breaking
```

Use when: you need to intercept all traffic to/from a service and apply uniform policies.

### Ambassador

A specialized sidecar for **outbound** traffic only. The service talks to `localhost:ambassador_port`, and the ambassador handles service discovery, load balancing, and retries for the outbound call.

```
[Service] --> [Ambassador] --> [Remote Service]
                   ^
                   |
           Service discovery,
           load balancing,
           retries, circuit breaking
```

Use when: you want to simplify how your service connects to external dependencies. The service just talks to localhost -- it does not need to know about service discovery, DNS, or retry policies.

### Adapter

A specialized sidecar that **transforms protocols or data formats**. The service outputs in one format, and the adapter converts it to whatever the consumer expects.

```
[Service] --> [Adapter] --> [Monitoring System]
  (custom       ^           (expects Prometheus
   metrics)     |            format)
         Protocol translation,
         format conversion
```

Use when: you need to integrate a service with a system that expects a different protocol or data format. Classic example: a legacy service that outputs custom metrics, adapted into Prometheus format.

---

## Systems Constraints Analysis

### CPU

**Proxy overhead**: Every request is parsed twice -- once by the sidecar, once by the service. The sidecar must parse HTTP headers (for auth, routing), apply rate limiting logic (token bucket check), and construct the forwarded request.

Measured overhead per request:
```
Auth check (JWT verification):    ~0.5-2ms  (depends on algorithm; RS256 is slower)
Rate limit check (token bucket):  ~0.01ms   (in-memory counter check)
Logging (structured log write):   ~0.05ms   (async write to buffer)
Request forwarding (proxy hop):   ~0.1-0.5ms (localhost HTTP round trip)

Total sidecar overhead: ~1-3ms per request
```

For FoodDash at 100 orders/minute: 100 x 3ms = 300ms of CPU time per minute. Negligible. At 10K requests/second: the sidecar needs its own CPU core. Envoy is designed for this -- it uses an event-driven architecture with worker threads and can handle 10K+ requests/second per core.

### Memory

**Sidecar process footprint**: The sidecar is a separate process with its own memory space.

```
Envoy proxy:           ~50-100 MB RSS (baseline)
Our Python sidecar:    ~30-50 MB RSS (Python + httpx + FastAPI)
Linkerd2-proxy (Rust): ~10-20 MB RSS (minimal footprint)
```

Multiply by the number of services: 5 services x 50 MB sidecar = 250 MB additional memory. This is the cost of process isolation. A shared library would use ~0 additional memory (loaded once per process). But you gain failure isolation and operational independence.

At scale (1000 services): 1000 x 50 MB = 50 GB of memory just for sidecar proxies. This is why Linkerd's ~15 MB footprint matters -- 1000 x 15 MB = 15 GB, a significant reduction.

### Network

**Extra localhost hop**: Every request makes an additional HTTP call from the sidecar to the service (or vice versa). This is a localhost call -- no physical network traversal, no serialization beyond HTTP framing.

```
Without sidecar:
  Client --[network]--> Service
  Latency: network RTT only

With sidecar:
  Client --[network]--> Sidecar --[localhost]--> Service
  Latency: network RTT + localhost RTT (~0.1-0.5ms)
```

The localhost hop adds 0.1-0.5ms. On a 50ms total request, that is 0.2-1% overhead. On a 2ms internal RPC, it is 5-25% overhead. This matters for latency-critical paths.

**mTLS overhead**: When sidecars encrypt inter-service traffic (mTLS), there is additional latency for TLS handshake on first connection and encryption/decryption on every message. Connection pooling and session resumption mitigate this to ~0.2ms per request after warmup.

### Latency

The sidecar adds latency. This is the fundamental cost of the pattern:

```
Request path WITHOUT sidecar:
  Client --> Service --> Client
  Added latency: 0ms

Request path WITH sidecar (inbound only):
  Client --> Sidecar --> Service --> Sidecar --> Client
  Added latency: ~1-3ms (auth + rate limit + 2 localhost hops)

Request path WITH service mesh (both sides):
  Client --> Sidecar A --> [network] --> Sidecar B --> Service
  Added latency: ~2-6ms (2 sidecars in the path)
```

For most web applications (50-500ms response times), 1-3ms is acceptable. For latency-critical paths (sub-millisecond internal RPCs, high-frequency trading), the sidecar overhead is too much.

---

## Principal-Level Depth

### Envoy Proxy Internals

Envoy (the proxy used by Istio, and the most widely deployed sidecar proxy) has a sophisticated architecture:

**Threading model**: One main thread for configuration management, N worker threads for request processing. Each worker thread runs its own event loop. Connections are assigned to workers and never migrate -- this eliminates lock contention.

```
Main Thread:
  - Configuration updates (xDS from control plane)
  - Stats aggregation
  - Admin interface

Worker Thread 0:          Worker Thread 1:
  - Event loop              - Event loop
  - Connections: A, C, E    - Connections: B, D, F
  - Filters: auth, rate     - Filters: auth, rate
  - No locks needed         - No locks needed
```

**Filter chains**: Envoy processes requests through a chain of filters. Each filter can inspect, modify, or reject the request. Common filters:
- `envoy.filters.http.jwt_authn` -- JWT verification
- `envoy.filters.http.ratelimit` -- Rate limiting (calls external rate limit service)
- `envoy.filters.http.router` -- Routes to upstream cluster
- `envoy.filters.http.cors` -- CORS handling

**xDS APIs**: Envoy does not read static config files (well, it can, but in production it does not). It receives configuration dynamically via xDS (discovery service) APIs:
- **LDS** (Listener Discovery): What ports to listen on
- **RDS** (Route Discovery): How to route requests
- **CDS** (Cluster Discovery): What upstream services exist
- **EDS** (Endpoint Discovery): Which instances are healthy

This means you can change routing rules, add rate limits, or deploy canary weights without restarting any proxy. The control plane pushes new config and Envoy hot-reloads.

### mTLS Between Sidecars

In a service mesh, every service-to-service call is encrypted with mutual TLS. Both sides present certificates. The control plane acts as a Certificate Authority:

```
1. Service A's sidecar needs to call Service B
2. Sidecar A presents its certificate (issued by Istiod CA)
3. Sidecar B presents its certificate (issued by Istiod CA)
4. Both verify the other's certificate against the CA
5. TLS session established -- traffic is encrypted
6. Sidecar B checks the identity in A's certificate against RBAC policy
7. If authorized: forward to Service B
   If not: reject with 403
```

The services themselves have no TLS code. They talk plain HTTP to localhost. The sidecar handles all encryption. This is "zero-trust networking" -- even inside your private network, every call is authenticated and encrypted.

Certificate rotation happens automatically. Istiod issues short-lived certificates (default: 24 hours) and rotates them before expiry. No human intervention needed.

### Observability for Free

With sidecars intercepting all traffic, you get observability without instrumenting application code:

**Distributed tracing**: The sidecar injects trace headers (B3, W3C Trace-Context) into outbound requests and propagates them on inbound requests. A complete trace shows every service hop, with timing:

```
[Order Service: 45ms]
  |
  +--> [Sidecar A -> Sidecar B: 2ms]
  |      |
  |      +--> [Kitchen Service: 15ms]
  |
  +--> [Sidecar A -> Sidecar C: 2ms]
         |
         +--> [Billing Service: 120ms]  <-- bottleneck identified
```

**Metrics (RED method)**: Every sidecar automatically records:
- **Rate**: Requests per second
- **Errors**: Error rate (4xx, 5xx)
- **Duration**: Latency percentiles (p50, p95, p99)

Exported as Prometheus metrics. No application code changes.

**Access logs**: Every request is logged with method, path, status, latency, upstream/downstream addresses. Structured JSON for log aggregation.

### Traffic Management

Sidecars enable sophisticated traffic control:

**Canary deployments**: Route 5% of traffic to v2 of your service, 95% to v1. If error rate on v2 spikes, roll back to 0%. If healthy, gradually increase to 100%.

```yaml
# Istio VirtualService
apiVersion: networking.istio.io/v1alpha3
kind: VirtualService
spec:
  http:
  - route:
    - destination:
        host: order-service
        subset: v1
      weight: 95
    - destination:
        host: order-service
        subset: v2
      weight: 5
```

**Circuit breaking**: If an upstream service is failing, stop sending traffic to it. After a cooldown, send a probe request. If it succeeds, resume traffic.

**Fault injection**: In staging, inject 500ms latency or 10% errors to test resilience:

```yaml
apiVersion: networking.istio.io/v1alpha3
kind: VirtualService
spec:
  http:
  - fault:
      delay:
        percentage:
          value: 10
        fixedDelay: 500ms
    route:
    - destination:
        host: billing-service
```

### When NOT to Use Sidecars

1. **Latency-critical paths**: If your service-to-service calls need sub-millisecond latency (real-time bidding, HFT), the 1-3ms sidecar overhead is too much. Use in-process libraries instead.

2. **Simple deployments**: If you have 2-3 services and one team, the operational overhead of managing sidecar proxies outweighs the benefits. Copy-paste the middleware -- it is fine at small scale.

3. **Resource-constrained environments**: Each sidecar consumes 10-100 MB of memory. On edge devices or IoT gateways, this overhead is prohibitive.

4. **Monoliths**: If you have one service, there is nothing to mesh. The sidecar pattern only pays off when you have N services that share cross-cutting concerns.

5. **Debugging difficulty**: Adding a proxy layer makes debugging harder. Request failures could be in the sidecar, in the network between sidecar and service, or in the service itself. You need good observability tooling to diagnose issues.

**Rule of thumb**: If you have fewer than 5 services and one team, use shared libraries. Between 5-20 services, consider sidecars for the most critical cross-cutting concerns (auth, mTLS). Above 20 services with multiple teams, a full service mesh pays for itself.

---

## Trade-offs at a Glance

| Dimension | Shared Library | Sidecar Proxy | Service Mesh |
|-----------|---------------|---------------|--------------|
| **Added latency** | 0ms (in-process) | 1-3ms (localhost hop) | 2-6ms (two sidecars) |
| **Memory overhead** | ~0 (shared process) | 10-100 MB per service | 10-100 MB per service |
| **Language coupling** | Must match service language | Language-agnostic | Language-agnostic |
| **Deploy independence** | Requires service redeploy | Independent sidecar deploy | Independent, fleet-wide |
| **Failure isolation** | Shares process (crash together) | Separate processes | Separate processes |
| **Config changes** | Redeploy all services | Hot reload sidecar | Hot reload via control plane |
| **Observability** | Must instrument each service | Automatic (intercepts traffic) | Automatic + centralized |
| **mTLS** | Each service implements | Transparent | Transparent + CA managed |
| **Complexity** | Low | Medium | High |
| **Best for** | Small teams, <5 services | Cross-cutting concerns, mixed languages | Large fleets, multiple teams |

---

## Running the Code

### Start the business service (pure logic, no middleware)

```bash
# From the repo root
uv run python -m chapters.ch10_sidecar.app_service
```

This starts the order service on port 8010. It has NO auth, NO logging, NO rate limiting. Pure business logic. Try calling it directly -- no token needed.

### Start the sidecar proxy

```bash
# In another terminal
uv run python -m chapters.ch10_sidecar.sidecar_proxy
```

The sidecar starts on port 8011. It forwards valid requests to the service on port 8010, handling:
- JWT auth verification
- Request/response logging
- Token-bucket rate limiting

### Test the flow

```bash
# Direct to service (no auth required -- this is the "naked" service)
curl http://localhost:8010/orders

# Through sidecar WITHOUT token (rejected 401)
curl http://localhost:8011/orders

# Through sidecar WITH valid token (accepted)
curl -H "Authorization: Bearer fooddash-demo-token" http://localhost:8011/orders

# Rapid-fire to trigger rate limiting (429 after threshold)
for i in $(seq 1 20); do
  curl -s -o /dev/null -w "%{http_code}\n" \
    -H "Authorization: Bearer fooddash-demo-token" \
    http://localhost:8011/orders
done
```

### Docker Compose (conceptual)

```bash
# See docker-compose.yml for how service + sidecar deploy together
cat chapters/ch10_sidecar/docker-compose.yml
```

### Open the visualization

Open `chapters/ch10_sidecar/visual.html` in a browser to see:
- Request flow through the sidecar
- Each cross-cutting concern applied step-by-step
- Comparison with and without sidecar
- Rate limiting in action

---

## Bridge to Chapter 11

We have now covered every communication pattern in the FoodDash journey:

- **Request-Response** (Ch01): The foundation. Client sends, server replies.
- **Short Polling** (Ch02): Client asks repeatedly. Simple but wasteful.
- **Long Polling** (Ch03): Client asks, server holds until there is data.
- **Server-Sent Events** (Ch04): Server pushes over HTTP. One-directional.
- **WebSockets** (Ch05): Full-duplex, bidirectional, persistent.
- **Push Notifications** (Ch06): Reaching users when the app is closed.
- **Pub/Sub** (Ch07): Decoupling publishers from subscribers.
- **Stateful vs Stateless** (Ch08): The scaling tension.
- **Multiplexing** (Ch09): Many streams, one connection.
- **Sidecar** (Ch10): Cross-cutting concerns extracted from services.

Each pattern solves a specific problem and creates new ones. Request-response is simple but synchronous. WebSockets are powerful but stateful. Pub/sub decouples but requires a broker. Multiplexing saves connections but introduces framing complexity. Sidecars simplify services but add latency.

The final chapter synthesizes everything: how do you choose the right pattern for the right problem? How do these patterns compose? What does the complete FoodDash architecture look like when all ten patterns work together?

Next: [Chapter 11 -- Synthesis](../ch11_synthesis/).

"""Sidecar proxy -- handles auth, logging, and rate limiting.

This proxy sits in front of the app service (port 8010) and intercepts all
inbound traffic on port 8011. It applies three cross-cutting concerns:

  1. JWT Auth:      Validates the Authorization header before forwarding.
  2. Logging:       Logs every request with method, path, status, and timing.
  3. Rate Limiting: Token-bucket rate limiter per client IP.

The app service sees only authenticated, rate-limited, logged requests.
It contains zero middleware code.

Run:
    # First start the app service:
    uv run python -m chapters.ch10_sidecar.app_service

    # Then start this sidecar:
    uv run python -m chapters.ch10_sidecar.sidecar_proxy
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from starlette.responses import JSONResponse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

UPSTREAM_URL = "http://localhost:8010"  # The app service
SIDECAR_PORT = 8011                     # External-facing port

# JWT settings (simplified for education -- production would use RS256 + JWKS)
JWT_SECRET = "fooddash-secret-key-for-demo"
DEMO_TOKEN = "fooddash-demo-token"  # Pre-shared demo token for easy testing

# Rate limiting settings
RATE_LIMIT_TOKENS = 10       # Max tokens per bucket
RATE_LIMIT_REFILL_RATE = 2.0 # Tokens added per second
RATE_LIMIT_WINDOW = 1.0      # Refill check interval in seconds


# ---------------------------------------------------------------------------
# JWT Auth (simplified)
# ---------------------------------------------------------------------------

def verify_token(token: str) -> dict | None:
    """Verify a JWT-like token.

    In production this would:
      - Decode the JWT (header.payload.signature)
      - Verify the signature against a public key (RS256) or secret (HS256)
      - Check expiration (exp claim)
      - Check issuer (iss claim)
      - Check audience (aud claim)

    For this educational demo, we accept:
      - The literal demo token (for easy curl testing)
      - A simple HMAC-signed token format: payload.signature
    """
    # Accept the demo token for easy testing
    if token == DEMO_TOKEN:
        return {"sub": "demo-user", "role": "customer"}

    # Try HMAC verification (simplified JWT)
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        payload_b64, signature = parts
        expected_sig = hmac.new(
            JWT_SECRET.encode(), payload_b64.encode(), hashlib.sha256
        ).hexdigest()[:16]
        if not hmac.compare_digest(signature, expected_sig):
            return None
        payload = json.loads(
            __import__("base64").b64decode(payload_b64 + "==").decode()
        )
        return payload
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Token Bucket Rate Limiter
# ---------------------------------------------------------------------------

@dataclass
class TokenBucket:
    """Token bucket rate limiter.

    Each client IP gets a bucket. Tokens are consumed on each request.
    Tokens refill at a fixed rate. If the bucket is empty, the request
    is rejected with 429 Too Many Requests.

    This is the same algorithm used by Envoy, Nginx, and most API gateways.
    """

    max_tokens: float = RATE_LIMIT_TOKENS
    refill_rate: float = RATE_LIMIT_REFILL_RATE
    _buckets: dict[str, float] = field(default_factory=dict, init=False)
    _last_refill: dict[str, float] = field(default_factory=dict, init=False)

    def consume(self, key: str) -> bool:
        """Try to consume one token. Returns True if allowed, False if rate limited."""
        now = time.time()

        if key not in self._buckets:
            self._buckets[key] = self.max_tokens
            self._last_refill[key] = now

        # Refill tokens based on elapsed time
        elapsed = now - self._last_refill[key]
        self._buckets[key] = min(
            self.max_tokens,
            self._buckets[key] + elapsed * self.refill_rate,
        )
        self._last_refill[key] = now

        # Try to consume
        if self._buckets[key] >= 1.0:
            self._buckets[key] -= 1.0
            return True
        return False

    def remaining(self, key: str) -> int:
        """How many tokens remain for this key."""
        return int(self._buckets.get(key, self.max_tokens))


# ---------------------------------------------------------------------------
# Sidecar application
# ---------------------------------------------------------------------------

app = FastAPI(title="Ch10 -- Sidecar Proxy")
rate_limiter = TokenBucket()
http_client: httpx.AsyncClient | None = None

# Request counter for logging
request_count = 0


@app.on_event("startup")
async def startup():
    global http_client
    http_client = httpx.AsyncClient(base_url=UPSTREAM_URL, timeout=10.0)


@app.on_event("shutdown")
async def shutdown():
    if http_client:
        await http_client.aclose()


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(request: Request, path: str):
    """Main proxy endpoint -- all requests flow through here.

    This single handler implements the sidecar pattern:
      1. Auth check
      2. Rate limit check
      3. Log the request
      4. Forward to upstream service
      5. Log the response
      6. Return the response
    """
    global request_count
    request_count += 1
    req_id = request_count
    start_time = time.time()
    client_ip = request.client.host if request.client else "unknown"

    # ----- STEP 1: Auth Check -----
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.replace("Bearer ", "").strip()

    # Skip auth for health checks (sidecar should always expose health)
    if path == "health":
        pass  # No auth required for health
    elif not token:
        elapsed = time.time() - start_time
        print(
            f"  [{req_id:04d}] {request.method} /{path} --> 401 UNAUTHORIZED "
            f"(no token) [{elapsed*1000:.1f}ms] [{client_ip}]"
        )
        return JSONResponse(
            status_code=401,
            content={"error": "Missing Authorization header", "sidecar": True},
        )
    elif verify_token(token) is None:
        elapsed = time.time() - start_time
        print(
            f"  [{req_id:04d}] {request.method} /{path} --> 401 UNAUTHORIZED "
            f"(invalid token) [{elapsed*1000:.1f}ms] [{client_ip}]"
        )
        return JSONResponse(
            status_code=401,
            content={"error": "Invalid token", "sidecar": True},
        )

    # ----- STEP 2: Rate Limit Check -----
    if not rate_limiter.consume(client_ip):
        elapsed = time.time() - start_time
        print(
            f"  [{req_id:04d}] {request.method} /{path} --> 429 RATE LIMITED "
            f"[{elapsed*1000:.1f}ms] [{client_ip}]"
        )
        return JSONResponse(
            status_code=429,
            content={
                "error": "Rate limit exceeded",
                "retry_after_seconds": 1.0 / RATE_LIMIT_REFILL_RATE,
                "sidecar": True,
            },
            headers={"Retry-After": str(int(1.0 / RATE_LIMIT_REFILL_RATE))},
        )

    # ----- STEP 3: Forward to Upstream Service -----
    try:
        body = await request.body()
        headers = dict(request.headers)
        # Remove hop-by-hop headers
        headers.pop("host", None)
        headers.pop("connection", None)
        # Add sidecar metadata (the service can see who validated the request)
        user_info = verify_token(token) if token else None
        if user_info:
            headers["X-Sidecar-User"] = json.dumps(user_info)
        headers["X-Sidecar-Request-Id"] = str(req_id)
        headers["X-Forwarded-For"] = client_ip

        upstream_response = await http_client.request(
            method=request.method,
            url=f"/{path}",
            headers=headers,
            content=body,
            params=dict(request.query_params),
        )
    except httpx.ConnectError:
        elapsed = time.time() - start_time
        print(
            f"  [{req_id:04d}] {request.method} /{path} --> 502 BAD GATEWAY "
            f"(upstream unreachable) [{elapsed*1000:.1f}ms] [{client_ip}]"
        )
        return JSONResponse(
            status_code=502,
            content={"error": "Upstream service unreachable", "sidecar": True},
        )
    except Exception as exc:
        elapsed = time.time() - start_time
        print(
            f"  [{req_id:04d}] {request.method} /{path} --> 502 BAD GATEWAY "
            f"({exc}) [{elapsed*1000:.1f}ms] [{client_ip}]"
        )
        return JSONResponse(
            status_code=502,
            content={"error": f"Proxy error: {exc}", "sidecar": True},
        )

    # ----- STEP 4: Log and Return Response -----
    elapsed = time.time() - start_time
    status = upstream_response.status_code
    remaining = rate_limiter.remaining(client_ip)

    print(
        f"  [{req_id:04d}] {request.method} /{path} --> {status} "
        f"[{elapsed*1000:.1f}ms] [{client_ip}] "
        f"[rate-limit remaining: {remaining}/{RATE_LIMIT_TOKENS}]"
    )

    # Build response with sidecar headers
    response_headers = dict(upstream_response.headers)
    response_headers["X-Sidecar"] = "true"
    response_headers["X-Sidecar-Latency-Ms"] = f"{elapsed*1000:.1f}"
    response_headers["X-RateLimit-Remaining"] = str(remaining)
    response_headers["X-RateLimit-Limit"] = str(RATE_LIMIT_TOKENS)

    return Response(
        content=upstream_response.content,
        status_code=status,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("Chapter 10 -- Sidecar Proxy")
    print("=" * 60)
    print("Cross-cutting concerns handled by this proxy:")
    print("  1. JWT Auth verification")
    print("  2. Request/response logging")
    print("  3. Token-bucket rate limiting")
    print(f"\nSidecar listening on http://localhost:{SIDECAR_PORT}")
    print(f"Forwarding to upstream at {UPSTREAM_URL}")
    print(f"\nDemo token: {DEMO_TOKEN}")
    print(f"Rate limit: {RATE_LIMIT_TOKENS} requests, refill {RATE_LIMIT_REFILL_RATE}/sec")
    print("=" * 60 + "\n")

    uvicorn.run(app, host="0.0.0.0", port=SIDECAR_PORT)

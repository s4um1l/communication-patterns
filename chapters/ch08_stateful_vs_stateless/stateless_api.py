"""Stateless API server — any server can handle any request.

This demonstrates the purest form of stateless architecture: JWT-based auth
with no server-side sessions. The server holds ZERO per-client state. Every
request carries its own identity proof (JWT) and references shared external
state (the database).

Run two instances on different ports to prove it:
    uv run uvicorn chapters.ch08_stateful_vs_stateless.stateless_api:app --port 8008
    uv run uvicorn chapters.ch08_stateful_vs_stateless.stateless_api:app --port 8009

Then hit EITHER server with the same JWT — both will work identically.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import base64
from typing import Any

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from shared.db import DB
from shared.models import Customer, Order, OrderItem

# ---------------------------------------------------------------------------
# JWT implementation (simplified for educational purposes)
# In production, use PyJWT or python-jose. This is intentionally transparent
# so you can see exactly what "stateless auth" means.
# ---------------------------------------------------------------------------

# In real life this comes from an environment variable, rotated periodically.
# The SAME secret is deployed to ALL servers — that's what makes it stateless.
JWT_SECRET = "fooddash-demo-secret-shared-across-all-servers"
JWT_EXPIRY_SECONDS = 3600  # 1 hour


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * padding)


def create_jwt(payload: dict[str, Any]) -> str:
    """Create a signed JWT. The signature proves the payload hasn't been tampered with."""
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {**payload, "iat": int(time.time()), "exp": int(time.time()) + JWT_EXPIRY_SECONDS}

    header_b64 = _b64url_encode(json.dumps(header).encode())
    payload_b64 = _b64url_encode(json.dumps(payload).encode())

    signing_input = f"{header_b64}.{payload_b64}"
    signature = hmac.new(JWT_SECRET.encode(), signing_input.encode(), hashlib.sha256).digest()
    signature_b64 = _b64url_encode(signature)

    return f"{header_b64}.{payload_b64}.{signature_b64}"


def verify_jwt(token: str) -> dict[str, Any]:
    """Verify a JWT and return its payload. ANY server with the secret can do this."""
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
    except ValueError:
        raise HTTPException(status_code=401, detail="Malformed JWT")

    # Verify signature — this proves the payload was created by someone with the secret
    signing_input = f"{header_b64}.{payload_b64}"
    expected_sig = hmac.new(JWT_SECRET.encode(), signing_input.encode(), hashlib.sha256).digest()
    actual_sig = _b64url_decode(signature_b64)

    if not hmac.compare_digest(expected_sig, actual_sig):
        raise HTTPException(status_code=401, detail="Invalid JWT signature")

    payload = json.loads(_b64url_decode(payload_b64))

    # Check expiration
    if payload.get("exp", 0) < time.time():
        raise HTTPException(status_code=401, detail="JWT expired")

    return payload


def get_current_user(authorization: str | None = Header(None)) -> dict[str, Any]:
    """Extract and verify the user from the Authorization header.

    This is the KEY insight: the server doesn't look up a session. It verifies
    a cryptographic signature. No database call. No session store. No state.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization.removeprefix("Bearer ")
    return verify_jwt(token)


# ---------------------------------------------------------------------------
# Application — a stateless FoodDash API
# ---------------------------------------------------------------------------

app = FastAPI(title="FoodDash Stateless API (Ch08)")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Shared database — in production this would be PostgreSQL/MySQL accessible
# by ALL servers. For the demo, each process has its own in-memory DB seeded
# with the same data, which is good enough to prove the stateless concept.
db = DB()


class LoginRequest(BaseModel):
    name: str
    address: str = "123 Main St"


class PlaceOrderRequest(BaseModel):
    restaurant_id: str
    item_ids: list[str]


@app.get("/")
async def root(request: Request):
    """Identify which server instance is responding."""
    import os
    return {
        "service": "FoodDash Stateless API",
        "chapter": "08 — Stateful vs Stateless",
        "port": request.url.port,
        "pid": os.getpid(),
        "note": "This server holds NO per-client state. Hit any server with your JWT.",
    }


@app.post("/login")
async def login(req: LoginRequest):
    """Create a JWT for the user.

    NOTE: This is NOT a real login — there's no password check. The point is
    to demonstrate that the JWT carries all identity information and any server
    can verify it without a session store.
    """
    customer = Customer(name=req.name, address=req.address)
    db.customers[customer.id] = customer

    token = create_jwt({"user_id": customer.id, "name": customer.name})

    return {
        "token": token,
        "customer_id": customer.id,
        "message": (
            "This JWT is self-contained. It carries your identity. "
            "ANY server can verify it without looking up a session. "
            "That's what makes this stateless."
        ),
    }


@app.get("/me")
async def get_me(authorization: str | None = Header(None)):
    """Return the current user's info — decoded from the JWT, not from a session.

    This endpoint proves statelessness: the server doesn't look up who you are
    in a session store. It reads your identity directly from the JWT you sent.
    """
    user = get_current_user(authorization)
    import os
    return {
        "user": user,
        "served_by_pid": os.getpid(),
        "note": "Your identity came from the JWT, not from server memory.",
    }


@app.get("/menu/{restaurant_id}")
async def get_menu(restaurant_id: str):
    """Get a restaurant's menu — no auth needed, completely stateless."""
    restaurant = db.get_restaurant(restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found")
    return {"restaurant": restaurant.name, "menu": [item.model_dump() for item in restaurant.menu]}


@app.post("/orders")
async def place_order(req: PlaceOrderRequest, authorization: str | None = Header(None)):
    """Place an order — authenticated via JWT, stored in shared database."""
    user = get_current_user(authorization)

    restaurant = db.get_restaurant(req.restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found")

    menu_map = {item.id: item for item in restaurant.menu}
    items = []
    for item_id in req.item_ids:
        if item_id not in menu_map:
            raise HTTPException(status_code=422, detail=f"Unknown menu item: {item_id}")
        items.append(OrderItem(menu_item=menu_map[item_id]))

    customer = db.customers.get(user["user_id"])
    if not customer:
        customer = Customer(id=user["user_id"], name=user["name"])

    order = Order(customer=customer, restaurant_id=req.restaurant_id, items=items)
    await db.place_order(order)

    import os
    return {
        "order_id": order.id,
        "total_cents": order.total_cents,
        "status": order.status.value,
        "served_by_pid": os.getpid(),
        "note": "This order was created by one server but can be read by ANY server.",
    }


@app.get("/orders/{order_id}")
async def get_order(order_id: str, authorization: str | None = Header(None)):
    """Get an order — ANY server can serve this, no sticky sessions needed."""
    _user = get_current_user(authorization)

    order = await db.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    import os
    return {
        "order": order.model_dump(),
        "served_by_pid": os.getpid(),
        "note": "Any server with access to the shared database can return this order.",
    }

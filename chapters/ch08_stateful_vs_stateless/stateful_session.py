"""Stateful session server — sessions only work on the server that created them.

This demonstrates the fundamental problem with server-side sessions: the
session state lives IN the server process. If a load balancer routes your
next request to a different server, your session doesn't exist there.

Run two instances to see the failure:
    uv run uvicorn chapters.ch08_stateful_vs_stateless.stateful_session:app --port 8018
    uv run uvicorn chapters.ch08_stateful_vs_stateless.stateful_session:app --port 8019

Login on port 8018, then try to use that session on port 8019 — it fails.
This is exactly what happens behind a round-robin load balancer.
"""

from __future__ import annotations

import os
import time
import uuid

from fastapi import FastAPI, HTTPException, Cookie, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from shared.db import DB
from shared.models import Customer, Order, OrderItem

# ---------------------------------------------------------------------------
# In-memory session store — THIS IS THE PROBLEM
#
# This dict exists only in THIS server process. Another server process has
# its own empty dict. When a load balancer routes a request to the wrong
# server, the session isn't found and the user gets a 401.
# ---------------------------------------------------------------------------

sessions: dict[str, dict] = {}

# Track this server's identity for educational output
SERVER_ID = f"server_{os.getpid()}"

app = FastAPI(title="FoodDash Stateful Session Server (Ch08)")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

db = DB()


class LoginRequest(BaseModel):
    name: str
    address: str = "123 Main St"


class PlaceOrderRequest(BaseModel):
    restaurant_id: str
    item_ids: list[str]


def get_session(session_id: str | None) -> dict:
    """Look up a session — this ONLY works on the server that created it.

    This is the crux of the stateful problem. The session dict is local to
    this process. If you hit a different server, this lookup returns nothing.
    """
    if not session_id:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "No session cookie",
                "server": SERVER_ID,
                "explanation": "You haven't logged in to THIS server.",
            },
        )

    session = sessions.get(session_id)
    if not session:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "Session not found",
                "session_id": session_id,
                "server": SERVER_ID,
                "sessions_on_this_server": len(sessions),
                "explanation": (
                    f"This session was created on a DIFFERENT server. "
                    f"This server ({SERVER_ID}) has {len(sessions)} sessions, "
                    f"but yours isn't one of them. This is the stateful session "
                    f"problem — your session is trapped on the server that created it."
                ),
            },
        )

    # Check expiry
    if session.get("expires_at", 0) < time.time():
        del sessions[session_id]
        raise HTTPException(status_code=401, detail="Session expired")

    return session


@app.get("/")
async def root():
    return {
        "service": "FoodDash Stateful Session Server",
        "chapter": "08 — Stateful vs Stateless",
        "server_id": SERVER_ID,
        "pid": os.getpid(),
        "active_sessions": len(sessions),
        "session_ids": list(sessions.keys()),
        "warning": (
            "Sessions are stored IN THIS PROCESS. They do NOT exist on other servers. "
            "This is the fundamental problem with stateful session management."
        ),
    }


@app.post("/login")
async def login(req: LoginRequest, response: Response):
    """Create a session — stored in THIS server's memory only.

    If the next request goes to a different server, this session won't be found.
    """
    customer = Customer(name=req.name, address=req.address)
    db.customers[customer.id] = customer

    session_id = f"sess_{uuid.uuid4().hex[:16]}"
    sessions[session_id] = {
        "user_id": customer.id,
        "name": customer.name,
        "created_at": time.time(),
        "expires_at": time.time() + 3600,
        "created_on_server": SERVER_ID,
    }

    # Set the session cookie
    response.set_cookie(key="session_id", value=session_id, httponly=True)

    return {
        "session_id": session_id,
        "customer_id": customer.id,
        "server_id": SERVER_ID,
        "message": (
            f"Session created on {SERVER_ID}. This session ONLY exists in this "
            f"server's memory. If your next request hits a different server, "
            f"you'll get a 401. That's the stateful session problem."
        ),
    }


@app.get("/me")
async def get_me(session_id: str | None = Cookie(None)):
    """Return the current user — looked up from the in-memory session store.

    Unlike the JWT approach in stateless_api.py, this requires hitting the
    SAME server that created the session.
    """
    session = get_session(session_id)
    return {
        "user": {
            "user_id": session["user_id"],
            "name": session["name"],
        },
        "session_created_on": session["created_on_server"],
        "served_by": SERVER_ID,
        "note": (
            "Your identity came from an in-memory session lookup. "
            "This only works because you hit the same server that created your session."
        ),
    }


@app.get("/menu/{restaurant_id}")
async def get_menu(restaurant_id: str):
    """Get a restaurant's menu — no auth needed."""
    restaurant = db.get_restaurant(restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found")
    return {"restaurant": restaurant.name, "menu": [item.model_dump() for item in restaurant.menu]}


@app.post("/orders")
async def place_order(req: PlaceOrderRequest, session_id: str | None = Cookie(None)):
    """Place an order — requires a valid session on THIS server."""
    session = get_session(session_id)

    restaurant = db.get_restaurant(req.restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found")

    menu_map = {item.id: item for item in restaurant.menu}
    items = []
    for item_id in req.item_ids:
        if item_id not in menu_map:
            raise HTTPException(status_code=422, detail=f"Unknown menu item: {item_id}")
        items.append(OrderItem(menu_item=menu_map[item_id]))

    customer = db.customers.get(session["user_id"])
    if not customer:
        customer = Customer(id=session["user_id"], name=session["name"])

    order = Order(customer=customer, restaurant_id=req.restaurant_id, items=items)
    await db.place_order(order)

    return {
        "order_id": order.id,
        "total_cents": order.total_cents,
        "status": order.status.value,
        "served_by": SERVER_ID,
    }


@app.get("/orders/{order_id}")
async def get_order(order_id: str, session_id: str | None = Cookie(None)):
    """Get an order — requires a valid session on THIS server."""
    _session = get_session(session_id)

    order = await db.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    return {
        "order": order.model_dump(),
        "served_by": SERVER_ID,
    }


@app.get("/debug/sessions")
async def debug_sessions():
    """Debug endpoint — shows all sessions on this server.

    This makes the stateful problem visible: each server has its own
    independent set of sessions.
    """
    return {
        "server_id": SERVER_ID,
        "pid": os.getpid(),
        "total_sessions": len(sessions),
        "sessions": {
            sid: {
                "user_id": s["user_id"],
                "name": s["name"],
                "created_on_server": s["created_on_server"],
                "age_seconds": round(time.time() - s["created_at"], 1),
            }
            for sid, s in sessions.items()
        },
    }

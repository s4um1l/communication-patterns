"""Chapter 06 — Push Notifications: FoodDash Push Server

This server demonstrates the push notification pattern. The key insight:
we're NOT maintaining a connection to the client. Instead, we store a
*subscription* and send messages through a third-party push service
when events occur.

Endpoints:
    POST /subscribe           — Store a push subscription (endpoint + keys)
    POST /notify/{order_id}   — Send a push notification for an order event
    POST /orders              — Place an order (triggers push to customer)
    POST /orders/{id}/advance — Advance order status (triggers push)
    GET  /subscriptions       — List all stored subscriptions
    GET  /push-log            — View the log of sent/attempted pushes
    GET  /health              — Health check

Key things to notice:
    - No persistent connections. The server stores subscriptions (~200 bytes)
      and fires HTTPS POSTs to the push service when events occur.
    - The push service (FCM/Mozilla/etc.) maintains the device connection.
    - Payloads are encrypted end-to-end (ECDH + AES-128-GCM). The push
      service routes opaque blobs — it cannot read "Your food is arriving."
    - TTL, Urgency, and Topic headers control delivery behavior.
    - If pywebpush is available, we send real Web Push messages.
      Otherwise, we log exactly what WOULD be sent with full detail.

Run with:
    uv run uvicorn chapters.ch06_push_notifications.server:app --port 8006
"""

from __future__ import annotations

import json
import time
import hashlib
import base64
import traceback
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from shared.db import DB
from shared.models import Customer, MenuItem, Order, OrderItem, OrderStatus

# ---------------------------------------------------------------------------
# Try to import pywebpush for real Web Push delivery
# ---------------------------------------------------------------------------

try:
    from pywebpush import webpush, WebPushException
    WEBPUSH_AVAILABLE = True
except ImportError:
    WEBPUSH_AVAILABLE = False

try:
    from py_vapid import Vapid
    VAPID_AVAILABLE = True
except ImportError:
    VAPID_AVAILABLE = False


# ---------------------------------------------------------------------------
# App and database
# ---------------------------------------------------------------------------

app = FastAPI(
    title="FoodDash — Ch06 Push Notifications",
    description=(
        "Push notifications for order events. Demonstrates reaching users "
        "who have NO active connection to our server. Messages travel through "
        "the push service (APNs/FCM/Web Push) and arrive even when the app "
        "is closed."
    ),
    version="0.1.0",
)

db = DB()


# ---------------------------------------------------------------------------
# VAPID key management
# ---------------------------------------------------------------------------

# In production, these would be generated once and stored securely.
# The public key is shared with clients during subscription.
# The private key signs JWT tokens for VAPID authentication.

VAPID_PRIVATE_KEY: str | None = None
VAPID_PUBLIC_KEY: str | None = None
VAPID_CLAIMS = {"sub": "mailto:dev@fooddash.example.com"}

if VAPID_AVAILABLE:
    try:
        _vapid = Vapid()
        _vapid.generate_keys()
        VAPID_PRIVATE_KEY = _vapid.private_pem()
        VAPID_PUBLIC_KEY = _vapid.public_key_urlsafe_base64()
    except Exception:
        # Fall back to demo keys if generation fails
        VAPID_PRIVATE_KEY = None
        VAPID_PUBLIC_KEY = None

# Demo VAPID public key for when real key generation isn't available
DEMO_VAPID_PUBLIC_KEY = (
    "BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFg"
    "DzkEs7U8PY7iHR0hfjHG4WOJkGxlnHoA-RA1b8JMoGI"
)

def get_vapid_public_key() -> str:
    """Return the server's VAPID public key (for client subscription)."""
    return VAPID_PUBLIC_KEY or DEMO_VAPID_PUBLIC_KEY


# ---------------------------------------------------------------------------
# In-memory push subscription store
# ---------------------------------------------------------------------------

# Maps customer_id -> list of PushSubscription objects
# A user can have multiple subscriptions (multiple browsers/devices)
subscriptions: dict[str, list[dict[str, Any]]] = {}

# Log of all push attempts (for educational inspection)
push_log: list[dict[str, Any]] = []


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class SubscribeRequest(BaseModel):
    """A Web Push subscription from the browser.

    The browser generates this when the user grants notification permission
    and calls PushManager.subscribe(). It contains:
    - endpoint: unique URL on the push service for this browser
    - keys.p256dh: browser's ECDH public key (for payload encryption)
    - keys.auth: shared secret (for HKDF derivation)
    """
    customer_id: str = Field(..., examples=["cust_01"])
    endpoint: str = Field(..., examples=["https://fcm.googleapis.com/fcm/send/abc123"])
    keys: dict[str, str] = Field(
        ...,
        examples=[{
            "p256dh": "BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkEs7U8PY7iHR0hfjHG4WOJkGxlnHoA-RA1b8JMoGI",
            "auth": "aGVsbG93b3JsZA",
        }],
    )


class SubscribeResponse(BaseModel):
    customer_id: str
    subscription_count: int
    message: str
    subscription_size_bytes: int


class PlaceOrderRequest(BaseModel):
    customer_name: str = Field(..., min_length=1, examples=["Alice"])
    restaurant_id: str = Field(..., examples=["rest_01"])
    item_ids: list[str] = Field(..., min_length=1, examples=[["item_01", "item_02"]])


class PlaceOrderResponse(BaseModel):
    order_id: str
    status: OrderStatus
    items: list[dict]
    total_cents: int
    created_at: float
    push_sent: bool
    push_details: dict | None = None


class NotifyRequest(BaseModel):
    """Manual push notification trigger for testing."""
    title: str = Field(..., examples=["Your food is arriving!"])
    body: str = Field(..., examples=["Driver Bob is at your door with your Burger Palace order."])
    urgency: str = Field(default="normal", pattern="^(very-low|low|normal|high)$")
    ttl: int = Field(default=3600, ge=0, le=2419200, description="Time to live in seconds")
    topic: str | None = Field(default=None, examples=["order-status-abc123"])


class NotifyResponse(BaseModel):
    order_id: str
    customer_id: str
    subscriptions_targeted: int
    results: list[dict]


# ---------------------------------------------------------------------------
# Push notification sending logic
# ---------------------------------------------------------------------------

def _build_push_payload(
    title: str,
    body: str,
    order_id: str | None = None,
    status: str | None = None,
) -> str:
    """Build the JSON payload for a push notification.

    This payload will be encrypted before sending. The push service
    cannot read it — only the browser (Service Worker) can decrypt it.

    Payload must be under 4096 bytes (the Web Push limit).
    """
    payload = {
        "title": title,
        "body": body,
        "timestamp": time.time(),
        "data": {},
    }
    if order_id:
        payload["data"]["order_id"] = order_id
        payload["data"]["url"] = f"/orders/{order_id}"
    if status:
        payload["data"]["status"] = status

    encoded = json.dumps(payload)

    # Web Push payloads must be <= 4096 bytes
    if len(encoded.encode("utf-8")) > 4096:
        # Truncate body to fit
        excess = len(encoded.encode("utf-8")) - 4096 + 20
        payload["body"] = body[:len(body) - excess] + "..."
        encoded = json.dumps(payload)

    return encoded


def _send_push(
    subscription_info: dict[str, Any],
    payload: str,
    urgency: str = "normal",
    ttl: int = 3600,
    topic: str | None = None,
) -> dict[str, Any]:
    """Send a push notification to a single subscription.

    If pywebpush is available, sends a real Web Push message:
    1. Encrypts the payload using the subscription's p256dh and auth keys
    2. Signs the request with our VAPID private key
    3. POSTs the encrypted blob to the subscription endpoint

    If pywebpush is not available, logs what WOULD be sent with
    full educational detail about the encryption process.
    """
    result: dict[str, Any] = {
        "endpoint": subscription_info["endpoint"],
        "timestamp": time.time(),
        "payload_size_bytes": len(payload.encode("utf-8")),
        "urgency": urgency,
        "ttl": ttl,
        "topic": topic,
    }

    if WEBPUSH_AVAILABLE and VAPID_PRIVATE_KEY:
        # --- Real Web Push delivery ---
        try:
            headers: dict[str, str] = {}
            if topic:
                headers["Topic"] = topic

            response = webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=VAPID_CLAIMS.copy(),
                ttl=ttl,
                headers=headers,
            )

            result["status"] = "sent"
            result["response_status"] = getattr(response, "status_code", 201)
            result["method"] = "webpush (real)"
            result["encryption"] = {
                "algorithm": "AES-128-GCM",
                "key_exchange": "ECDH (P-256)",
                "key_derivation": "HKDF-SHA-256",
                "note": "Payload encrypted end-to-end. Push service cannot read it.",
            }

        except Exception as e:
            error_msg = str(e)
            result["status"] = "error"
            result["error"] = error_msg
            result["method"] = "webpush (real, failed)"

            # Educational: explain common errors
            if "401" in error_msg or "403" in error_msg:
                result["explanation"] = (
                    "VAPID authentication failed. The push service rejected our "
                    "server identity. This means our JWT signature didn't match "
                    "the public key the subscription was created with."
                )
            elif "410" in error_msg:
                result["explanation"] = (
                    "Subscription expired (410 Gone). The user unsubscribed or "
                    "the browser invalidated this subscription. Remove it from "
                    "your database — pushing to it again is pointless."
                )
            elif "429" in error_msg:
                result["explanation"] = (
                    "Rate limited (429 Too Many Requests). The push service is "
                    "throttling us. Back off and retry with exponential delay."
                )
            else:
                result["explanation"] = (
                    f"Push delivery failed: {error_msg}. "
                    "This could be a network issue, an invalid subscription, "
                    "or a push service outage."
                )
    else:
        # --- Simulated push (educational) ---
        result["status"] = "simulated"
        result["method"] = "simulated (pywebpush not configured)"

        # Show exactly what WOULD happen
        payload_bytes = payload.encode("utf-8")
        payload_hash = hashlib.sha256(payload_bytes).hexdigest()[:16]

        result["would_send"] = {
            "http_method": "POST",
            "url": subscription_info["endpoint"],
            "headers": {
                "Content-Type": "application/octet-stream",
                "Content-Encoding": "aes128gcm",
                "TTL": str(ttl),
                "Urgency": urgency,
                "Authorization": "vapid t=<JWT signed with ES256>, k=<VAPID public key>",
                **({"Topic": topic} if topic else {}),
            },
            "body": f"<{len(payload_bytes)} bytes, encrypted with AES-128-GCM>",
        }

        result["encryption_steps"] = [
            "1. Generate ephemeral ECDH key pair (P-256 curve)",
            f"2. ECDH(ephemeral_private, subscription.p256dh) -> shared_secret",
            f"3. HKDF(shared_secret, subscription.auth) -> content_encryption_key + nonce",
            f"4. AES-128-GCM(key, nonce, '{payload[:60]}...') -> encrypted_payload",
            f"5. Prepend ephemeral public key to encrypted_payload",
            f"6. POST encrypted_payload to {subscription_info['endpoint'][:50]}...",
        ]

        result["vapid_jwt_claims"] = {
            "aud": _extract_origin(subscription_info["endpoint"]),
            "exp": int(time.time()) + 86400,
            "sub": VAPID_CLAIMS["sub"],
        }

        result["payload_hash_sha256_prefix"] = payload_hash

    return result


def _extract_origin(url: str) -> str:
    """Extract the origin from a URL (for VAPID 'aud' claim)."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


async def _send_push_to_customer(
    customer_id: str,
    payload: str,
    urgency: str = "normal",
    ttl: int = 3600,
    topic: str | None = None,
) -> list[dict[str, Any]]:
    """Send a push notification to all of a customer's subscriptions.

    A customer may have multiple subscriptions (phone browser, desktop
    browser, etc.). We send to ALL of them — the user sees the notification
    on whichever device is available.
    """
    customer_subs = subscriptions.get(customer_id, [])
    if not customer_subs:
        return [{
            "status": "no_subscription",
            "customer_id": customer_id,
            "explanation": (
                "No push subscription found for this customer. "
                "They either never granted permission, or their "
                "subscription expired and wasn't renewed."
            ),
        }]

    results = []
    for sub in customer_subs:
        result = _send_push(
            subscription_info=sub,
            payload=payload,
            urgency=urgency,
            ttl=ttl,
            topic=topic,
        )
        results.append(result)

        # Log for educational inspection
        push_log.append({
            "customer_id": customer_id,
            "payload_preview": payload[:100],
            **result,
        })

    return results


# ---------------------------------------------------------------------------
# Status-to-notification mapping
# ---------------------------------------------------------------------------

STATUS_NOTIFICATIONS: dict[OrderStatus, dict[str, Any]] = {
    OrderStatus.PLACED: {
        "title": "Order Confirmed!",
        "body": "Your order from {restaurant} has been placed. We're finding a driver.",
        "urgency": "normal",
        "ttl": 86400,
    },
    OrderStatus.CONFIRMED: {
        "title": "Restaurant Accepted",
        "body": "{restaurant} has confirmed your order and is starting to prepare it.",
        "urgency": "normal",
        "ttl": 3600,
    },
    OrderStatus.PREPARING: {
        "title": "Cooking in Progress",
        "body": "The kitchen at {restaurant} is preparing your food.",
        "urgency": "low",
        "ttl": 3600,
    },
    OrderStatus.READY: {
        "title": "Food Ready!",
        "body": "Your order from {restaurant} is ready for pickup. Driver is on the way.",
        "urgency": "normal",
        "ttl": 1800,
    },
    OrderStatus.PICKED_UP: {
        "title": "Driver Has Your Food",
        "body": "Your driver has picked up your order from {restaurant}.",
        "urgency": "normal",
        "ttl": 1800,
    },
    OrderStatus.EN_ROUTE: {
        "title": "On the Way!",
        "body": "Your driver is heading to you with your {restaurant} order.",
        "urgency": "high",
        "ttl": 900,
    },
    OrderStatus.DELIVERED: {
        "title": "Delivered!",
        "body": "Your order from {restaurant} has been delivered. Enjoy your meal!",
        "urgency": "high",
        "ttl": 300,
    },
}


def _notification_for_status(order: Order) -> dict[str, Any] | None:
    """Get the notification template for an order's current status."""
    template = STATUS_NOTIFICATIONS.get(order.status)
    if not template:
        return None

    restaurant = db.get_restaurant(order.restaurant_id)
    restaurant_name = restaurant.name if restaurant else "the restaurant"

    return {
        "title": template["title"],
        "body": template["body"].format(restaurant=restaurant_name),
        "urgency": template["urgency"],
        "ttl": template["ttl"],
        "topic": f"order-status-{order.id}",
    }


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------


@app.get("/vapid-public-key")
def get_vapid_key() -> dict:
    """Return the server's VAPID public key.

    The browser needs this when calling PushManager.subscribe().
    It ties the subscription to this specific server — the push service
    will only accept pushes signed with the corresponding private key.
    """
    return {
        "public_key": get_vapid_public_key(),
        "note": (
            "Pass this to PushManager.subscribe({ "
            "applicationServerKey: urlBase64ToUint8Array(public_key) })"
        ),
    }


@app.post("/subscribe", response_model=SubscribeResponse, status_code=201)
async def subscribe(req: SubscribeRequest) -> SubscribeResponse:
    """Store a push subscription for a customer.

    This is called after the browser generates a PushSubscription object.
    The subscription contains:
    - endpoint: unique URL on the push service (FCM, Mozilla, etc.)
    - keys.p256dh: client's ECDH public key (65 bytes, base64url-encoded)
    - keys.auth: shared authentication secret (16 bytes, base64url-encoded)

    The server stores this and uses it later to send encrypted push messages.
    A customer can have multiple subscriptions (one per browser/device).
    """
    # Validate required keys
    if "p256dh" not in req.keys or "auth" not in req.keys:
        raise HTTPException(
            status_code=422,
            detail=(
                "Subscription must include 'p256dh' and 'auth' keys. "
                "These are generated by the browser during PushManager.subscribe()."
            ),
        )

    # Build the subscription info in the format pywebpush expects
    subscription_info = {
        "endpoint": req.endpoint,
        "keys": req.keys,
    }

    # Store the subscription
    if req.customer_id not in subscriptions:
        subscriptions[req.customer_id] = []

    # Avoid duplicate endpoints
    existing_endpoints = {s["endpoint"] for s in subscriptions[req.customer_id]}
    if req.endpoint not in existing_endpoints:
        subscriptions[req.customer_id].append(subscription_info)

    # Calculate subscription size
    sub_json = json.dumps(subscription_info)
    sub_size = len(sub_json.encode("utf-8"))

    return SubscribeResponse(
        customer_id=req.customer_id,
        subscription_count=len(subscriptions[req.customer_id]),
        message=(
            f"Subscription stored. {len(subscriptions[req.customer_id])} "
            f"active subscription(s) for this customer. "
            f"Each subscription is ~{sub_size} bytes — "
            f"compare to WebSocket's ~30KB per connection."
        ),
        subscription_size_bytes=sub_size,
    )


@app.post("/orders", response_model=PlaceOrderResponse, status_code=201)
async def place_order(req: PlaceOrderRequest) -> PlaceOrderResponse:
    """Place a new order. Sends a push notification to confirm.

    After creating the order, we immediately send a push notification
    to the customer. This works even if the customer has already closed
    the app — the push travels through the push service and appears
    on their lock screen.
    """
    restaurant = db.get_restaurant(req.restaurant_id)
    if restaurant is None:
        raise HTTPException(status_code=404, detail=f"Restaurant '{req.restaurant_id}' not found")

    menu_by_id: dict[str, MenuItem] = {item.id: item for item in restaurant.menu}
    order_items: list[OrderItem] = []
    for item_id in req.item_ids:
        menu_item = menu_by_id.get(item_id)
        if menu_item is None:
            raise HTTPException(status_code=422, detail=f"Item '{item_id}' not found")
        existing = next((oi for oi in order_items if oi.menu_item.id == item_id), None)
        if existing:
            existing.quantity += 1
        else:
            order_items.append(OrderItem(menu_item=menu_item, quantity=1))

    customer = Customer(name=req.customer_name)
    order = Order(
        customer=customer,
        restaurant_id=req.restaurant_id,
        items=order_items,
        status=OrderStatus.PLACED,
    )
    await db.place_order(order)

    # --- Send push notification ---
    notification = _notification_for_status(order)
    push_sent = False
    push_details = None

    if notification:
        payload = _build_push_payload(
            title=notification["title"],
            body=notification["body"],
            order_id=order.id,
            status=order.status.value,
        )
        results = await _send_push_to_customer(
            customer_id=customer.id,
            payload=payload,
            urgency=notification["urgency"],
            ttl=notification["ttl"],
            topic=notification["topic"],
        )
        push_sent = any(r.get("status") == "sent" or r.get("status") == "simulated" for r in results)
        push_details = {
            "notification": notification,
            "results": results,
        }

    return PlaceOrderResponse(
        order_id=order.id,
        status=order.status,
        items=[
            {"name": oi.menu_item.name, "quantity": oi.quantity, "subtotal_cents": oi.subtotal_cents}
            for oi in order.items
        ],
        total_cents=order.total_cents,
        created_at=order.created_at,
        push_sent=push_sent,
        push_details=push_details,
    )


@app.post("/orders/{order_id}/advance")
async def advance_order(order_id: str) -> dict:
    """Advance an order to its next status. Sends a push notification.

    Each status transition triggers a push notification with:
    - Appropriate urgency (low for "preparing", high for "delivered")
    - Appropriate TTL (short for time-sensitive, long for confirmations)
    - Topic header (replaces previous status notification instead of stacking)

    The Topic header is key: "order-status-{order_id}" means each new
    status REPLACES the previous notification. The user sees one notification
    with the latest status, not seven stacked notifications.
    """
    order = await db.update_order_status(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"Order '{order_id}' not found")

    # Send push notification for the new status
    notification = _notification_for_status(order)
    push_results = []

    if notification:
        payload = _build_push_payload(
            title=notification["title"],
            body=notification["body"],
            order_id=order.id,
            status=order.status.value,
        )
        push_results = await _send_push_to_customer(
            customer_id=order.customer.id,
            payload=payload,
            urgency=notification["urgency"],
            ttl=notification["ttl"],
            topic=notification["topic"],
        )

    return {
        "order_id": order.id,
        "previous_status": _previous_status(order.status),
        "new_status": order.status.value,
        "notification": notification,
        "push_results": push_results,
        "topic_explanation": (
            f"Topic '{notification['topic']}' means this notification REPLACES "
            f"any previous notification with the same topic. The user sees one "
            f"notification with the latest status, not multiple stacked ones."
        ) if notification and notification.get("topic") else None,
    }


@app.post("/notify/{order_id}", response_model=NotifyResponse)
async def send_custom_notification(order_id: str, req: NotifyRequest) -> NotifyResponse:
    """Send a custom push notification for an order.

    This endpoint lets you test push delivery with custom content,
    urgency, TTL, and topic. Useful for experimenting with how
    different settings affect delivery behavior.
    """
    order = await db.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"Order '{order_id}' not found")

    payload = _build_push_payload(
        title=req.title,
        body=req.body,
        order_id=order.id,
        status=order.status.value,
    )

    results = await _send_push_to_customer(
        customer_id=order.customer.id,
        payload=payload,
        urgency=req.urgency,
        ttl=req.ttl,
        topic=req.topic,
    )

    return NotifyResponse(
        order_id=order.id,
        customer_id=order.customer.id,
        subscriptions_targeted=len(subscriptions.get(order.customer.id, [])),
        results=results,
    )


@app.get("/subscriptions")
def list_subscriptions() -> dict:
    """List all stored push subscriptions.

    Educational: shows how little server-side state is needed.
    Each subscription is ~200 bytes. Compare to WebSocket, where
    each connection holds ~30KB of state (TCP buffers, TLS, app state).
    """
    total_subs = sum(len(subs) for subs in subscriptions.values())
    total_bytes = sum(
        len(json.dumps(sub).encode("utf-8"))
        for subs in subscriptions.values()
        for sub in subs
    )

    return {
        "total_customers": len(subscriptions),
        "total_subscriptions": total_subs,
        "total_storage_bytes": total_bytes,
        "comparison": {
            "push_per_user": f"~{total_bytes // max(total_subs, 1)} bytes/subscription",
            "websocket_per_user": "~30,000 bytes/connection (TCP + TLS + app state)",
            "ratio": f"Push uses ~{30000 // max(total_bytes // max(total_subs, 1), 1)}x less memory per user",
        },
        "customers": {
            cid: {
                "subscription_count": len(subs),
                "endpoints": [s["endpoint"][:60] + "..." for s in subs],
            }
            for cid, subs in subscriptions.items()
        },
    }


@app.get("/push-log")
def get_push_log() -> dict:
    """View the log of all push attempts.

    Shows every push notification the server has sent (or attempted
    to send), including encryption details, delivery status, and
    any errors. Useful for understanding the push lifecycle.
    """
    return {
        "total_pushes": len(push_log),
        "pushes": list(reversed(push_log[-50:])),  # Latest 50, newest first
        "webpush_available": WEBPUSH_AVAILABLE,
        "vapid_configured": VAPID_PRIVATE_KEY is not None,
    }


@app.get("/health")
def health() -> dict:
    """Health check with push notification capabilities."""
    total_subs = sum(len(subs) for subs in subscriptions.values())
    return {
        "status": "ok",
        "chapter": "06-push-notifications",
        "capabilities": {
            "webpush_library": WEBPUSH_AVAILABLE,
            "vapid_keys_generated": VAPID_PRIVATE_KEY is not None,
            "mode": "real" if (WEBPUSH_AVAILABLE and VAPID_PRIVATE_KEY) else "simulated",
        },
        "stats": {
            "total_subscriptions": total_subs,
            "total_pushes_sent": len(push_log),
        },
        "vapid_public_key": get_vapid_public_key(),
        "timestamp": time.time(),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _previous_status(current: OrderStatus) -> str | None:
    """Get the status that came before the current one."""
    from shared.models import ORDER_FLOW
    try:
        idx = ORDER_FLOW.index(current)
        if idx > 0:
            return ORDER_FLOW[idx - 1].value
    except ValueError:
        pass
    return None

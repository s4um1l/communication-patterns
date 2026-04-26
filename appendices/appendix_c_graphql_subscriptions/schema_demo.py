"""Appendix C — GraphQL Subscriptions Demo (pure Python, no graphql library).

This module demonstrates GraphQL concepts using only the Python standard
library and shared models. We simulate:

1. Schema definition as Python data structures
2. Query resolution — resolving only the requested fields
3. Over-fetching comparison: REST (full object) vs GraphQL (selected fields)
4. Subscription simulation with async generators
5. Payload size measurements

Run with:
    uv run python -m appendices.appendix_c_graphql_subscriptions.schema_demo
"""

from __future__ import annotations

import asyncio
import enum
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Ensure repo root is on sys.path so we can import shared models ──

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.models import (
    Customer,
    Driver,
    MenuItem,
    Order,
    OrderItem,
    OrderStatus,
    Restaurant,
)


# ═══════════════════════════════════════════════════════════════════════
# Part 1: Schema Definition as Python Data Structures
# ═══════════════════════════════════════════════════════════════════════
#
# In real GraphQL, the schema is defined in SDL (Schema Definition
# Language) and parsed by a library. Here we represent it as plain
# Python dicts and classes to show what the schema *means* without
# needing any dependency.

@dataclass
class FieldDef:
    """A field in a GraphQL type."""
    name: str
    type_name: str
    is_list: bool = False
    is_non_null: bool = True
    description: str = ""


@dataclass
class TypeDef:
    """A GraphQL object type."""
    name: str
    fields: dict[str, FieldDef] = field(default_factory=dict)
    description: str = ""


# ── Define the FoodDash GraphQL schema ──

SCHEMA: dict[str, TypeDef] = {
    "MenuItem": TypeDef(
        name="MenuItem",
        description="A menu item at a restaurant",
        fields={
            "id": FieldDef("id", "ID"),
            "name": FieldDef("name", "String"),
            "priceCents": FieldDef("priceCents", "Int"),
            "description": FieldDef("description", "String", is_non_null=False),
        },
    ),
    "OrderItem": TypeDef(
        name="OrderItem",
        description="An item within an order",
        fields={
            "menuItem": FieldDef("menuItem", "MenuItem"),
            "quantity": FieldDef("quantity", "Int"),
            "subtotalCents": FieldDef("subtotalCents", "Int"),
        },
    ),
    "Customer": TypeDef(
        name="Customer",
        fields={
            "id": FieldDef("id", "ID"),
            "name": FieldDef("name", "String"),
            "address": FieldDef("address", "String", is_non_null=False),
        },
    ),
    "Driver": TypeDef(
        name="Driver",
        fields={
            "id": FieldDef("id", "ID"),
            "name": FieldDef("name", "String"),
            "latitude": FieldDef("latitude", "Float"),
            "longitude": FieldDef("longitude", "Float"),
            "available": FieldDef("available", "Boolean"),
        },
    ),
    "Restaurant": TypeDef(
        name="Restaurant",
        fields={
            "id": FieldDef("id", "ID"),
            "name": FieldDef("name", "String"),
            "phone": FieldDef("phone", "String", is_non_null=False),
            "menu": FieldDef("menu", "MenuItem", is_list=True),
        },
    ),
    "Order": TypeDef(
        name="Order",
        fields={
            "id": FieldDef("id", "ID"),
            "status": FieldDef("status", "OrderStatus"),
            "customer": FieldDef("customer", "Customer"),
            "restaurant": FieldDef("restaurant", "Restaurant"),
            "driver": FieldDef("driver", "Driver", is_non_null=False),
            "items": FieldDef("items", "OrderItem", is_list=True),
            "totalCents": FieldDef("totalCents", "Int"),
            "createdAt": FieldDef("createdAt", "Float"),
            "updatedAt": FieldDef("updatedAt", "Float"),
        },
    ),
}


def print_schema() -> None:
    """Display the schema like SDL."""
    print("  GraphQL Schema (FoodDash):")
    print()
    for type_def in SCHEMA.values():
        desc = f'  "{type_def.description}"' if type_def.description else ""
        if desc:
            print(f"  {desc}")
        print(f"  type {type_def.name} {{")
        for f in type_def.fields.values():
            nullable = "!" if f.is_non_null else ""
            if f.is_list:
                print(f"    {f.name}: [{f.type_name}!]{nullable}")
            else:
                print(f"    {f.name}: {f.type_name}{nullable}")
        print("  }")
        print()


# ═══════════════════════════════════════════════════════════════════════
# Part 2: Simulated Data Store
# ═══════════════════════════════════════════════════════════════════════

# Build sample data using shared models

BURGER = MenuItem(id="item_01", name="Classic Burger", price_cents=899, description="Angus beef patty")
FRIES = MenuItem(id="item_02", name="Fries", price_cents=399, description="Crispy golden fries")
SHAKE = MenuItem(id="item_03", name="Milkshake", price_cents=549, description="Vanilla bean shake")

RESTAURANT = Restaurant(
    id="rest_01",
    name="Bob's Burgers",
    menu=[BURGER, FRIES, SHAKE],
)

CUSTOMER = Customer(id="cust_01", name="Alice", address="742 Evergreen Terrace")

DRIVER = Driver(id="drv_07", name="Bob", latitude=37.7749, longitude=-122.4194, available=False)

SAMPLE_ORDER = Order(
    id="ord_a1b2",
    customer=CUSTOMER,
    restaurant_id=RESTAURANT.id,
    items=[
        OrderItem(menu_item=BURGER, quantity=2),
        OrderItem(menu_item=FRIES, quantity=1),
    ],
    status=OrderStatus.EN_ROUTE,
    driver_id=DRIVER.id,
    created_at=1700000000.0,
    updated_at=1700003600.0,
)

# Full data store — what the "database" holds
DATA_STORE: dict[str, dict[str, Any]] = {
    "orders": {SAMPLE_ORDER.id: SAMPLE_ORDER},
    "customers": {CUSTOMER.id: CUSTOMER},
    "drivers": {DRIVER.id: DRIVER},
    "restaurants": {RESTAURANT.id: RESTAURANT},
}


# ═══════════════════════════════════════════════════════════════════════
# Part 3: Query Resolution Engine
# ═══════════════════════════════════════════════════════════════════════
#
# This is the heart of GraphQL: given a query specifying which fields
# the client wants, resolve ONLY those fields from the data.

def resolve_object(source: Any, requested_fields: dict[str, Any]) -> dict[str, Any]:
    """Resolve an object, returning only the requested fields.

    Args:
        source: The Python object (Pydantic model, dict, etc.) to resolve from
        requested_fields: A dict where keys are field names and values are
                         either True (scalar field) or a nested dict of
                         sub-fields (object/list field)

    Returns:
        A dict containing only the requested fields with their resolved values.
    """
    result: dict[str, Any] = {}

    for field_name, sub_fields in requested_fields.items():
        # Get the raw value from the source
        raw_value = _get_field_value(source, field_name)

        if raw_value is None:
            result[field_name] = None
        elif sub_fields is True:
            # Scalar field — return the value directly
            result[field_name] = _serialize_scalar(raw_value)
        elif isinstance(sub_fields, dict):
            # Object or list field — recurse
            if isinstance(raw_value, list):
                result[field_name] = [
                    resolve_object(item, sub_fields) for item in raw_value
                ]
            else:
                result[field_name] = resolve_object(raw_value, sub_fields)

    return result


def _get_field_value(source: Any, field_name: str) -> Any:
    """Extract a field value from various source types, with relationship resolution."""
    # Handle computed/relationship fields
    if field_name == "restaurant" and hasattr(source, "restaurant_id"):
        return DATA_STORE["restaurants"].get(source.restaurant_id)
    if field_name == "driver" and hasattr(source, "driver_id"):
        driver_id = source.driver_id
        return DATA_STORE["drivers"].get(driver_id) if driver_id else None
    if field_name == "totalCents" and hasattr(source, "total_cents"):
        return source.total_cents
    if field_name == "subtotalCents" and hasattr(source, "subtotal_cents"):
        return source.subtotal_cents
    if field_name == "priceCents" and hasattr(source, "price_cents"):
        return source.price_cents
    if field_name == "createdAt" and hasattr(source, "created_at"):
        return source.created_at
    if field_name == "updatedAt" and hasattr(source, "updated_at"):
        return source.updated_at
    if field_name == "menuItem" and hasattr(source, "menu_item"):
        return source.menu_item

    # Direct attribute access
    if hasattr(source, field_name):
        return getattr(source, field_name)
    # Dict access
    if isinstance(source, dict) and field_name in source:
        return source[field_name]
    return None


def _serialize_scalar(value: Any) -> Any:
    """Convert a scalar value to JSON-compatible form."""
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def execute_query(query_name: str, args: dict[str, Any],
                  requested_fields: dict[str, Any]) -> dict[str, Any]:
    """Execute a simulated GraphQL query.

    Args:
        query_name: The root query field (e.g., "order")
        args: Arguments (e.g., {"id": "ord_a1b2"})
        requested_fields: The field selection set

    Returns:
        The GraphQL-style response: {"data": {...}}
    """
    # Root resolver — look up the entity
    if query_name == "order":
        source = DATA_STORE["orders"].get(args.get("id", ""))
    elif query_name == "driver":
        source = DATA_STORE["drivers"].get(args.get("id", ""))
    elif query_name == "restaurant":
        source = DATA_STORE["restaurants"].get(args.get("id", ""))
    elif query_name == "customer":
        source = DATA_STORE["customers"].get(args.get("id", ""))
    else:
        return {"data": {query_name: None}, "errors": [{"message": f"Unknown query: {query_name}"}]}

    if source is None:
        return {"data": {query_name: None}}

    resolved = resolve_object(source, requested_fields)
    return {"data": {query_name: resolved}}


# ═══════════════════════════════════════════════════════════════════════
# Part 4: Demo — Query Resolution
# ═══════════════════════════════════════════════════════════════════════

def demo_query_resolution() -> None:
    """Show how GraphQL resolves only requested fields."""
    print("=" * 68)
    print("  PART 1: Query Resolution — Only Fetch What You Ask For")
    print("=" * 68)
    print()

    # Query 1: Just the status (minimal query)
    print("  Query 1: Just the order status")
    print("  ─────────────────────────────")
    print('  query { order(id: "ord_a1b2") { status } }')
    print()

    result1 = execute_query("order", {"id": "ord_a1b2"}, {"status": True})
    result1_json = json.dumps(result1, indent=4)
    for line in result1_json.split("\n"):
        print(f"    {line}")
    print(f"  Response size: {len(json.dumps(result1))} bytes")
    print()

    # Query 2: Order with driver info
    print("  Query 2: Order status + driver name and location")
    print("  ────────────────────────────────────────────────")
    print('  query { order(id: "ord_a1b2") { status, driver { name, latitude, longitude } } }')
    print()

    result2 = execute_query("order", {"id": "ord_a1b2"}, {
        "status": True,
        "driver": {
            "name": True,
            "latitude": True,
            "longitude": True,
        },
    })
    result2_json = json.dumps(result2, indent=4)
    for line in result2_json.split("\n"):
        print(f"    {line}")
    print(f"  Response size: {len(json.dumps(result2))} bytes")
    print()

    # Query 3: Complex query — order + driver + restaurant (the N+1 example)
    print("  Query 3: Order + driver + restaurant phone (N+1 solved)")
    print("  ────────────────────────────────────────────────────────")
    print('  query { order(id: "ord_a1b2") {')
    print("    status")
    print("    driver { name, latitude, longitude }")
    print("    restaurant { phone }")
    print("  } }")
    print()

    result3 = execute_query("order", {"id": "ord_a1b2"}, {
        "status": True,
        "driver": {
            "name": True,
            "latitude": True,
            "longitude": True,
        },
        "restaurant": {
            "phone": True,
        },
    })
    result3_json = json.dumps(result3, indent=4)
    for line in result3_json.split("\n"):
        print(f"    {line}")
    print(f"  Response size: {len(json.dumps(result3))} bytes")
    print()

    # Query 4: Full order with items (to compare with REST)
    print("  Query 4: Full order details (similar to what REST would return)")
    print("  ──────────────────────────────────────────────────────────────")

    result4 = execute_query("order", {"id": "ord_a1b2"}, {
        "id": True,
        "status": True,
        "customer": {"id": True, "name": True, "address": True},
        "restaurant": {"id": True, "name": True},
        "driver": {"id": True, "name": True, "latitude": True, "longitude": True},
        "items": {
            "menuItem": {"name": True, "priceCents": True},
            "quantity": True,
            "subtotalCents": True,
        },
        "totalCents": True,
        "createdAt": True,
        "updatedAt": True,
    })
    result4_json = json.dumps(result4, indent=4)
    for line in result4_json.split("\n"):
        print(f"    {line}")
    print(f"  Response size: {len(json.dumps(result4))} bytes")
    print()


# ═══════════════════════════════════════════════════════════════════════
# Part 5: Over-Fetching Comparison — REST vs GraphQL
# ═══════════════════════════════════════════════════════════════════════

def demo_overfetch_comparison() -> None:
    """Compare REST (full object) vs GraphQL (selected fields) payload sizes."""
    print("=" * 68)
    print("  PART 2: Over-Fetching — REST vs GraphQL Payload Sizes")
    print("=" * 68)
    print()

    order = SAMPLE_ORDER

    # REST response: full order object (server decides what to send)
    rest_response = {
        "id": order.id,
        "customer": {
            "id": order.customer.id,
            "name": order.customer.name,
            "address": order.customer.address,
        },
        "restaurant_id": order.restaurant_id,
        "items": [
            {
                "menu_item": {
                    "id": item.menu_item.id,
                    "name": item.menu_item.name,
                    "price_cents": item.menu_item.price_cents,
                    "description": item.menu_item.description,
                },
                "quantity": item.quantity,
            }
            for item in order.items
        ],
        "status": order.status.value,
        "driver_id": order.driver_id,
        "created_at": order.created_at,
        "updated_at": order.updated_at,
    }
    rest_bytes = json.dumps(rest_response).encode("utf-8")

    # Scenario: customer tracking screen needs just status + driver name + location
    graphql_response = execute_query("order", {"id": order.id}, {
        "status": True,
        "driver": {
            "name": True,
            "latitude": True,
            "longitude": True,
        },
    })
    graphql_bytes = json.dumps(graphql_response).encode("utf-8")

    print("  Scenario: Customer tracking screen")
    print("  Needs: order status, driver name, driver location")
    print()

    print("  REST approach:")
    print(f"    GET /api/orders/{order.id}")
    print(f"    Response size: {len(rest_bytes)} bytes (full order object)")
    print(f"    Fields returned: {_count_fields(rest_response)}")
    print(f"    Fields needed: 4 (status, driver.name, driver.lat, driver.lng)")
    print(f"    Over-fetched: {_count_fields(rest_response) - 4} unnecessary fields")
    print()

    # REST would also need a second request for driver details
    driver_rest_response = {
        "id": DRIVER.id,
        "name": DRIVER.name,
        "latitude": DRIVER.latitude,
        "longitude": DRIVER.longitude,
        "available": DRIVER.available,
    }
    driver_rest_bytes = json.dumps(driver_rest_response).encode("utf-8")

    print(f"    But wait — the REST order response has driver_id, not driver details.")
    print(f"    Need a second request: GET /api/drivers/{DRIVER.id}")
    print(f"    Second response size: {len(driver_rest_bytes)} bytes")
    print(f"    Total REST: {len(rest_bytes) + len(driver_rest_bytes)} bytes across 2 requests")
    print()

    print("  GraphQL approach:")
    print(f'    query {{ order(id: "{order.id}") {{ status, driver {{ name, latitude, longitude }} }} }}')
    print(f"    Response size: {len(graphql_bytes)} bytes (exactly what's needed)")
    print(f"    Requests: 1")
    print()

    rest_total = len(rest_bytes) + len(driver_rest_bytes)
    ratio = rest_total / len(graphql_bytes)
    saving = (1 - len(graphql_bytes) / rest_total) * 100

    max_bar = 50
    rest_bar_len = max_bar
    gql_bar_len = max(1, int(max_bar * len(graphql_bytes) / rest_total))

    print("  Size comparison:")
    print(f"    REST (2 requests): {rest_total:>4d} bytes  {'█' * rest_bar_len}")
    print(f"    GraphQL (1 query): {len(graphql_bytes):>4d} bytes  {'█' * gql_bar_len}")
    print(f"    Reduction: {ratio:.1f}x smaller ({saving:.0f}% less data)")
    print()

    # Show the N+1 scenario
    print("  ─── N+1 Problem: 3 Resources in 1 Request ───")
    print()
    print("  REST (3 sequential requests):")
    print(f"    1. GET /api/orders/{order.id}        → {len(rest_bytes):>3d} bytes")
    print(f"    2. GET /api/drivers/{DRIVER.id}       → {len(driver_rest_bytes):>3d} bytes")
    restaurant_rest = json.dumps({
        "id": RESTAURANT.id, "name": RESTAURANT.name, "phone": "(555) 123-4567",
        "menu": [{"id": m.id, "name": m.name, "price_cents": m.price_cents, "description": m.description} for m in RESTAURANT.menu]
    }).encode("utf-8")
    print(f"    3. GET /api/restaurants/{RESTAURANT.id} → {len(restaurant_rest):>3d} bytes")
    rest_total_3 = len(rest_bytes) + len(driver_rest_bytes) + len(restaurant_rest)
    print(f"    Total: 3 round trips, {rest_total_3} bytes")
    print()

    graphql_3 = execute_query("order", {"id": order.id}, {
        "status": True,
        "driver": {"name": True, "latitude": True, "longitude": True},
        "restaurant": {"phone": True},
    })
    graphql_3_bytes = json.dumps(graphql_3).encode("utf-8")
    print("  GraphQL (1 request):")
    print("    query { order(id: \"ord_a1b2\") {")
    print("      status")
    print("      driver { name, latitude, longitude }")
    print("      restaurant { phone }")
    print("    } }")
    print(f"    Total: 1 round trip, {len(graphql_3_bytes)} bytes")
    print()

    ratio_3 = rest_total_3 / len(graphql_3_bytes)
    print(f"  REST total:    {rest_total_3} bytes across 3 requests")
    print(f"  GraphQL total: {len(graphql_3_bytes)} bytes in 1 request")
    print(f"  Data reduction: {ratio_3:.1f}x")
    print(f"  Round trip reduction: 3x")
    print()


def _count_fields(obj: Any, prefix: str = "") -> int:
    """Count leaf fields in a nested dict/list."""
    count = 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                count += _count_fields(v, f"{prefix}{k}.")
            else:
                count += 1
    elif isinstance(obj, list):
        for item in obj:
            count += _count_fields(item, prefix)
    return count


# ═══════════════════════════════════════════════════════════════════════
# Part 6: Subscription Simulation
# ═══════════════════════════════════════════════════════════════════════
#
# GraphQL subscriptions are WebSocket streams with typed event data.
# We simulate this with async generators — the same concurrency
# primitive that powers real subscription resolvers.

ORDER_LIFECYCLE = [
    (OrderStatus.PLACED, None, "Order received"),
    (OrderStatus.CONFIRMED, None, "Restaurant confirmed"),
    (OrderStatus.PREPARING, None, "Kitchen is cooking"),
    (OrderStatus.READY, None, "Food is ready for pickup"),
    (OrderStatus.PICKED_UP, "drv_07", "Driver Bob picked up your order"),
    (OrderStatus.EN_ROUTE, "drv_07", "Bob is on the way — ETA 12 min"),
    (OrderStatus.DELIVERED, "drv_07", "Delivered! Enjoy your meal"),
]


async def subscription_order_updated(
    order_id: str,
    requested_fields: dict[str, Any],
) -> Any:
    """Simulate a GraphQL subscription: orderUpdated(orderId: ...).

    This is an async generator — the same pattern used in real GraphQL
    subscription resolvers. Each yield is a typed event pushed to the
    client over WebSocket.

    The key GraphQL behavior: each event is resolved against the
    requested_fields selection set, so the client only receives the
    fields it asked for.
    """
    order = Order(
        id=order_id,
        customer=CUSTOMER,
        restaurant_id=RESTAURANT.id,
        items=[OrderItem(menu_item=BURGER, quantity=2)],
        status=OrderStatus.PLACED,
        driver_id=None,
        created_at=time.time(),
        updated_at=time.time(),
    )

    for status, driver_id, _message in ORDER_LIFECYCLE:
        await asyncio.sleep(0.15)  # Simulate time between events
        order.status = status
        order.driver_id = driver_id
        order.updated_at = time.time()

        # Resolve only the fields the subscription query requested
        resolved = resolve_object(order, requested_fields)

        # This is what the server sends over WebSocket:
        # { "type": "next", "id": "1", "payload": { "data": { "orderUpdated": <resolved> } } }
        yield {
            "type": "next",
            "id": "1",
            "payload": {
                "data": {
                    "orderUpdated": resolved,
                },
            },
        }

    # Subscription complete
    yield {"type": "complete", "id": "1"}


async def demo_subscription() -> None:
    """Demonstrate a GraphQL subscription with field selection."""
    print("=" * 68)
    print("  PART 3: Subscription Simulation — Typed WebSocket Events")
    print("=" * 68)
    print()

    print("  Subscription query:")
    print('    subscription { orderUpdated(orderId: "ord_a1b2") {')
    print("      status")
    print("      driver { name }")
    print("    } }")
    print()
    print("  Lifecycle: connection_init → connection_ack → subscribe → events → complete")
    print()

    # Simulate the protocol handshake
    print("  [WS] Client → Server: connection_init { auth_token: \"...\" }")
    print("  [WS] Server → Client: connection_ack")
    print("  [WS] Client → Server: subscribe { id: \"1\", query: \"subscription { ... }\" }")
    print()

    # Requested fields — the client only wants status and driver.name
    requested = {
        "status": True,
        "driver": {"name": True},
    }

    # Also track what a raw WebSocket would send (full object) for comparison
    full_fields = {
        "id": True,
        "status": True,
        "customer": {"id": True, "name": True, "address": True},
        "driver": {"id": True, "name": True, "latitude": True, "longitude": True},
        "items": {"menuItem": {"name": True, "priceCents": True}, "quantity": True},
        "totalCents": True,
        "createdAt": True,
        "updatedAt": True,
    }

    total_graphql_bytes = 0
    total_raw_ws_bytes = 0
    event_count = 0

    async for event in subscription_order_updated("ord_a1b2", requested):
        if event["type"] == "complete":
            print(f"  [WS] Server → Client: complete {{ id: \"{event['id']}\" }}")
            break

        data = event["payload"]["data"]["orderUpdated"]
        event_json = json.dumps(event)
        event_size = len(event_json.encode("utf-8"))
        total_graphql_bytes += event_size
        event_count += 1

        # What raw WebSocket would send
        full_event = json.dumps({"orderUpdated": resolve_object(
            DATA_STORE["orders"].get("ord_a1b2", SAMPLE_ORDER), full_fields
        )})
        total_raw_ws_bytes += len(full_event.encode("utf-8"))

        status = data.get("status", "?")
        driver_name = data.get("driver", {})
        if isinstance(driver_name, dict):
            driver_name = driver_name.get("name", "none")
        else:
            driver_name = "none"

        print(f"  [WS] Server → Client: next {{ status: \"{status}\", "
              f"driver: {{ name: \"{driver_name}\" }} }}  ({event_size} bytes)")

    print()
    print("  Subscription complete.")
    print()
    print(f"  Payload comparison over {event_count} events:")
    print(f"    GraphQL subscription (selected fields): {total_graphql_bytes:>5d} bytes total")
    print(f"    Raw WebSocket (full objects):            {total_raw_ws_bytes:>5d} bytes total")
    if total_raw_ws_bytes > 0:
        saving = (1 - total_graphql_bytes / total_raw_ws_bytes) * 100
        print(f"    Savings: {saving:.0f}% less data over the wire")
    print()


# ═══════════════════════════════════════════════════════════════════════
# Part 7: Subscription Protocol Detail
# ═══════════════════════════════════════════════════════════════════════

def demo_protocol_messages() -> None:
    """Show the actual WebSocket message format for graphql-ws protocol."""
    print("=" * 68)
    print("  PART 4: graphql-ws Protocol — Message Format Detail")
    print("=" * 68)
    print()

    messages = [
        ("Client → Server", {
            "type": "connection_init",
            "payload": {"auth_token": "Bearer eyJhbG..."}
        }),
        ("Server → Client", {
            "type": "connection_ack",
        }),
        ("Client → Server", {
            "type": "subscribe",
            "id": "1",
            "payload": {
                "query": 'subscription { orderUpdated(orderId: "ord_a1b2") { status driver { name } } }',
            },
        }),
        ("Server → Client", {
            "type": "next",
            "id": "1",
            "payload": {
                "data": {
                    "orderUpdated": {
                        "status": "confirmed",
                        "driver": None,
                    }
                }
            },
        }),
        ("Server → Client", {
            "type": "next",
            "id": "1",
            "payload": {
                "data": {
                    "orderUpdated": {
                        "status": "en_route",
                        "driver": {"name": "Bob"},
                    }
                }
            },
        }),
        ("Server → Client", {
            "type": "complete",
            "id": "1",
        }),
    ]

    for direction, msg in messages:
        msg_json = json.dumps(msg, indent=6)
        size = len(json.dumps(msg).encode("utf-8"))
        print(f"  {direction} ({size} bytes):")
        for line in msg_json.split("\n"):
            print(f"    {line}")
        print()

    print("  Key observations:")
    print("    - Every message has a 'type' field — the protocol verb")
    print("    - Subscriptions have an 'id' for multiplexing on one connection")
    print("    - Data events ('next') carry the GraphQL response shape")
    print("    - The 'complete' message signals the subscription has ended")
    print("    - Compare with raw WebSocket: no standard message format")
    print()


# ═══════════════════════════════════════════════════════════════════════
# Part 8: Payload Size Analysis
# ═══════════════════════════════════════════════════════════════════════

def demo_payload_analysis() -> None:
    """Measure how field selection affects payload size."""
    print("=" * 68)
    print("  PART 5: Field Selection → Payload Size Impact")
    print("=" * 68)
    print()

    scenarios = [
        ("Just status", {"status": True}),
        ("Status + driver name", {"status": True, "driver": {"name": True}}),
        ("Status + driver full", {"status": True, "driver": {"id": True, "name": True, "latitude": True, "longitude": True}}),
        ("Status + driver + restaurant", {"status": True, "driver": {"name": True}, "restaurant": {"name": True, "phone": True}}),
        ("Full order details", {
            "id": True, "status": True,
            "customer": {"id": True, "name": True, "address": True},
            "restaurant": {"id": True, "name": True},
            "driver": {"id": True, "name": True, "latitude": True, "longitude": True},
            "items": {"menuItem": {"name": True, "priceCents": True}, "quantity": True, "subtotalCents": True},
            "totalCents": True, "createdAt": True, "updatedAt": True,
        }),
    ]

    # REST baseline
    rest_full = json.dumps(SAMPLE_ORDER.model_dump(), default=str).encode("utf-8")
    rest_size = len(rest_full)

    print(f"  REST baseline (full object, every request): {rest_size} bytes")
    print()
    print(f"  {'Query':<35s} {'Size':>6s} {'vs REST':>8s}  Bar")
    print(f"  {'─' * 35} {'─' * 6} {'─' * 8}  {'─' * 40}")

    for label, fields in scenarios:
        result = execute_query("order", {"id": "ord_a1b2"}, fields)
        size = len(json.dumps(result).encode("utf-8"))
        pct = size / rest_size * 100
        bar_len = max(1, int(40 * size / rest_size))
        print(f"  {label:<35s} {size:>5d}B {pct:>6.0f}%  {'█' * bar_len}")

    print(f"  {'REST (full object)':<35s} {rest_size:>5d}B {'100':>6s}%  {'█' * 40}")
    print()
    print("  The less you ask for, the less you get — that's the GraphQL contract.")
    print("  Mobile clients on cellular networks benefit most from this reduction.")
    print()


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════

def main() -> None:
    """Run all demonstrations."""
    print()
    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║   Appendix C — GraphQL Subscriptions Demo (Pure Python)        ║")
    print("║                                                                ║")
    print("║   This demo simulates GraphQL concepts without any GraphQL     ║")
    print("║   library. Schema, resolution, and subscriptions are built     ║")
    print("║   from scratch to show how GraphQL maps to patterns you        ║")
    print("║   already know from the main chapters.                         ║")
    print("╚══════════════════════════════════════════════════════════════════╝")
    print()

    # Part 1: Schema
    print("=" * 68)
    print("  SCHEMA: FoodDash GraphQL Types")
    print("=" * 68)
    print()
    print_schema()

    # Part 2: Query resolution
    demo_query_resolution()

    # Part 3: Over-fetching comparison
    demo_overfetch_comparison()

    # Part 4: Subscription simulation
    asyncio.run(demo_subscription())

    # Part 5: Protocol message detail
    demo_protocol_messages()

    # Part 6: Payload size analysis
    demo_payload_analysis()

    print("=" * 68)
    print("  Demo complete. See README.md for full educational content.")
    print("  See visual.html for interactive visualizations.")
    print("=" * 68)
    print()


if __name__ == "__main__":
    main()

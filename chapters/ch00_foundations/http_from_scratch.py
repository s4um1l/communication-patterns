"""
HTTP Server From Scratch -- What Flask/FastAPI Do Under the Hood
================================================================

This module implements a minimal HTTP/1.1 server using raw TCP sockets.
No http.server, no framework, no dependencies. Just socket + string parsing.

This is exactly what happens inside every web framework. When you write:

    @app.get("/api/v1/status")
    def status():
        return {"service": "fooddash", "status": "ok"}

...the framework is doing everything in this file: accepting TCP connections,
reading raw bytes, parsing the HTTP request text, routing to your handler,
serializing the response, formatting HTTP response text, and writing it back.

Run it:
    uv run python -m chapters.ch00_foundations.http_from_scratch

Test it:
    curl -v http://localhost:8000/
    curl -v http://localhost:8000/api/v1/status
    curl -v -X POST http://localhost:8000/api/v1/orders -d '{"items": [1,2]}'

    # Or in a browser: http://localhost:8000/

What you will learn:
    - HTTP is literally formatted text over TCP
    - How request parsing works (and why it is a security-sensitive operation)
    - How response serialization works
    - What "routing" actually means (string matching on the request path)
    - Why frameworks exist (to save you from writing all this boilerplate)
"""

from __future__ import annotations

import json
import socket
import sys
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HOST = "127.0.0.1"
PORT = 8000
BACKLOG = 5
BUFFER_SIZE = 8192  # 8 KB should be enough for most HTTP requests.
                     # In production, you would handle partial reads for
                     # large request bodies using Content-Length.

# HTTP uses CRLF (\r\n) as line endings. This is specified in RFC 7230.
# A blank line (CRLF CRLF) separates headers from body.
CRLF = "\r\n"

# Maximum header size (to prevent denial-of-service via huge headers).
# nginx defaults to 8 KB, Apache to 8 KB. We match that.
MAX_HEADER_SIZE = 8192


# ---------------------------------------------------------------------------
# HTTP Parsing
# ---------------------------------------------------------------------------

class HttpRequest:
    """
    Parsed HTTP request.

    This is a simplified version of what frameworks like Starlette (the ASGI
    server under FastAPI) build from raw bytes. A production parser would:
    - Handle chunked transfer encoding
    - Handle multipart form data
    - Handle URL encoding (%20 for spaces, etc.)
    - Validate header names and values against RFC 7230
    - Support HTTP/2 binary framing (completely different format)

    Attributes
    ----------
    method : str
        GET, POST, PUT, DELETE, etc.
    path : str
        The request path, e.g., "/api/v1/status"
    version : str
        Protocol version, e.g., "HTTP/1.1"
    headers : dict[str, str]
        Header name -> value mapping. Names are lowercased for easy lookup.
    body : str
        The request body (for POST/PUT). Empty string if no body.
    """

    def __init__(
        self,
        method: str,
        path: str,
        version: str,
        headers: dict[str, str],
        body: str,
    ) -> None:
        self.method = method
        self.path = path
        self.version = version
        self.headers = headers
        self.body = body

    def __repr__(self) -> str:
        return f"HttpRequest({self.method} {self.path} {self.version})"


def parse_request(raw: bytes) -> HttpRequest:
    """
    Parse raw bytes into an HttpRequest.

    This is the core of every HTTP server. The raw bytes look like:

        GET /api/v1/status HTTP/1.1\r\n
        Host: localhost:8000\r\n
        Accept: application/json\r\n
        \r\n

    We need to:
    1. Split on the first blank line (CRLF+CRLF) to separate headers from body
    2. Parse the first line (request line) into method, path, version
    3. Parse subsequent lines into header name-value pairs
    4. The rest (after the blank line) is the body

    Security note: This is a naive parser for educational purposes. A production
    parser must defend against:
    - Request smuggling (ambiguous Content-Length / Transfer-Encoding)
    - Header injection (newlines in header values)
    - Slowloris attacks (extremely slow header delivery to tie up connections)
    - Buffer overflow (headers larger than MAX_HEADER_SIZE)

    Parameters
    ----------
    raw : bytes
        Raw bytes read from the TCP socket.

    Returns
    -------
    HttpRequest
        Parsed request object.

    Raises
    ------
    ValueError
        If the request is malformed.
    """
    # Decode bytes to string. HTTP/1.1 headers are ASCII (RFC 7230 Section 3.2.4).
    # The body encoding depends on Content-Type, but we treat it as UTF-8.
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Request contains invalid UTF-8: {exc}") from exc

    # Split headers from body. The blank line (CRLF+CRLF) is the delimiter.
    # This is why HTTP headers cannot contain blank lines.
    header_end = f"{CRLF}{CRLF}"
    if header_end not in text:
        # We might have received a partial request. In production, we would
        # buffer and keep reading. For simplicity, we reject it.
        raise ValueError("Incomplete HTTP request (no header-body separator found)")

    header_section, body = text.split(header_end, maxsplit=1)
    lines = header_section.split(CRLF)

    # --- Parse the request line ---
    # Format: METHOD SP REQUEST-TARGET SP HTTP-VERSION
    # Example: GET /api/v1/status HTTP/1.1
    #
    # This is where routing begins. The METHOD tells us what operation,
    # the path tells us what resource. Everything after this is metadata.
    request_line = lines[0]
    parts = request_line.split(" ")
    if len(parts) != 3:
        raise ValueError(f"Malformed request line: {request_line!r}")

    method, path, version = parts

    # Validate method (basic check)
    valid_methods = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
    if method not in valid_methods:
        raise ValueError(f"Unknown HTTP method: {method!r}")

    # --- Parse headers ---
    # Format: Header-Name: Header-Value
    # Headers can span multiple lines (obsolete line folding, RFC 7230 Section 3.2.4),
    # but we do not support that here. Modern HTTP forbids it.
    #
    # We lowercase header names because HTTP headers are case-insensitive
    # (RFC 7230 Section 3.2). "Content-Type" and "content-type" are the same header.
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ": " not in line:
            # Tolerate headers with just ":" (no space), which some clients send.
            if ":" in line:
                name, value = line.split(":", maxsplit=1)
                headers[name.strip().lower()] = value.strip()
            continue
        name, value = line.split(": ", maxsplit=1)
        headers[name.lower()] = value

    return HttpRequest(
        method=method,
        path=path,
        version=version,
        headers=headers,
        body=body,
    )


# ---------------------------------------------------------------------------
# HTTP Response Building
# ---------------------------------------------------------------------------

def build_response(
    status_code: int,
    status_text: str,
    headers: dict[str, str],
    body: str = "",
) -> bytes:
    """
    Build a raw HTTP response as bytes, ready to send over TCP.

    The response format is:

        HTTP/1.1 200 OK\r\n
        Content-Type: application/json\r\n
        Content-Length: 42\r\n
        \r\n
        {"service": "fooddash", "status": "ok"}

    This is what frameworks like Flask and FastAPI construct when you
    return a dict from a route handler. Starlette (FastAPI's ASGI server)
    builds exactly this string and writes it to the socket.

    Parameters
    ----------
    status_code : int
        HTTP status code (200, 404, 500, etc.).
    status_text : str
        Human-readable status ("OK", "Not Found", "Internal Server Error").
    headers : dict[str, str]
        Response headers.
    body : str
        Response body (typically JSON for API servers).

    Returns
    -------
    bytes
        Complete HTTP response, encoded as bytes.
    """
    # Encode the body first so we can compute Content-Length.
    # Content-Length is in BYTES, not characters. A Unicode character can be
    # multiple bytes in UTF-8. Getting this wrong causes subtle bugs:
    # the browser reads fewer/more bytes than expected, and either truncates
    # the response or hangs waiting for more data.
    body_bytes = body.encode("utf-8")

    # Always set Content-Length so the client knows when the response ends.
    # Without it, the client must rely on the connection closing to detect
    # the end of the response (HTTP/1.0 behavior) or use chunked encoding.
    headers["Content-Length"] = str(len(body_bytes))

    # Add a Date header (required by RFC 7231 Section 7.1.1.2 for origin servers)
    headers["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")

    # Add Server header (informational)
    headers["Server"] = "FoodDash-FromScratch/0.1"

    # Keep the connection alive by default (HTTP/1.1 behavior)
    if "Connection" not in headers:
        headers["Connection"] = "keep-alive"

    # --- Build the response string ---
    # Status line
    response = f"HTTP/1.1 {status_code} {status_text}{CRLF}"

    # Headers
    for name, value in headers.items():
        response += f"{name}: {value}{CRLF}"

    # Blank line separating headers from body
    response += CRLF

    # Encode the header portion and concatenate with the pre-encoded body.
    # We do this separately because headers are ASCII but the body might
    # have different encoding (though we use UTF-8 for both).
    return response.encode("utf-8") + body_bytes


def json_response(data: Any, status_code: int = 200, status_text: str = "OK") -> bytes:
    """
    Convenience function: serialize data to JSON and build an HTTP response.

    This is equivalent to FastAPI's `return {"key": "value"}` from a route
    handler, which gets serialized to JSON automatically.
    """
    body = json.dumps(data, indent=2)
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        # CORS headers (allow browser requests from any origin).
        # In production, you would restrict this to your domain.
        "Access-Control-Allow-Origin": "*",
        # Cache control: do not cache API responses by default.
        "Cache-Control": "no-store",
    }
    return build_response(status_code, status_text, headers, body)


def html_response(html: str, status_code: int = 200, status_text: str = "OK") -> bytes:
    """Build an HTTP response with HTML content."""
    headers = {"Content-Type": "text/html; charset=utf-8"}
    return build_response(status_code, status_text, headers, html)


def error_response(status_code: int, status_text: str, message: str) -> bytes:
    """Build a JSON error response."""
    return json_response(
        {"error": status_text, "message": message, "status": status_code},
        status_code=status_code,
        status_text=status_text,
    )


# ---------------------------------------------------------------------------
# Routing (the simplest possible implementation)
# ---------------------------------------------------------------------------

def route_request(request: HttpRequest) -> bytes:
    """
    Route an HTTP request to the appropriate handler.

    In a framework like FastAPI, this is done with decorators:
        @app.get("/api/v1/status")
        def status(): ...

    Under the hood, the framework maintains a mapping of (method, path_pattern)
    to handler functions. When a request arrives, it iterates through the
    mappings, finds a match (possibly with path parameters like /users/{id}),
    and calls the handler.

    Our version is just if/elif for clarity. The principle is the same:
    match method + path, call handler, return response.
    """
    # Log the request (like access logs in nginx/Apache)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {request.method} {request.path}")

    # --- Route: GET / ---
    # A simple welcome page showing this is the FoodDash API
    if request.method == "GET" and request.path == "/":
        return html_response(
            """<!DOCTYPE html>
<html>
<head><title>FoodDash API</title></head>
<body style="font-family: monospace; background: #1a1a2e; color: #eee; padding: 40px;">
    <h1 style="color: #e94560;">FoodDash API</h1>
    <p>This HTTP response was built from raw TCP sockets. No framework involved.</p>
    <h2>Available Endpoints:</h2>
    <ul>
        <li><a href="/api/v1/status" style="color: #0f3460;">/api/v1/status</a> - Service status</li>
        <li><code>POST /api/v1/orders</code> - Place an order (try with curl)</li>
    </ul>
    <h2>How This Works:</h2>
    <ol>
        <li>Your browser opened a TCP connection to localhost:8000 (3-way handshake)</li>
        <li>It sent: <code>GET / HTTP/1.1\\r\\nHost: localhost:8000\\r\\n...</code></li>
        <li>This server parsed that raw text into a method, path, and headers</li>
        <li>It matched the path "/" to this handler</li>
        <li>It built this HTML response with proper HTTP headers</li>
        <li>It wrote the response bytes back over the TCP connection</li>
    </ol>
    <p>View source of this file to see every step annotated.</p>
</body>
</html>"""
        )

    # --- Route: GET /api/v1/status ---
    # The classic health-check endpoint. Every service needs one.
    if request.method == "GET" and request.path == "/api/v1/status":
        return json_response({
            "service": "fooddash",
            "status": "ok",
            "version": "0.0.1",
            "chapter": "ch00_foundations",
            "note": "This response was built from raw TCP sockets, not a framework.",
        })

    # --- Route: POST /api/v1/orders ---
    # Demonstrates reading a request body (JSON parsing)
    if request.method == "POST" and request.path == "/api/v1/orders":
        # Parse the JSON body. In a framework, this is automatic.
        # request.body contains everything after the blank line in the HTTP request.
        if not request.body:
            return error_response(400, "Bad Request", "Request body is required")

        content_type = request.headers.get("content-type", "")
        if "json" not in content_type and request.body.strip().startswith("{"):
            # Be lenient: accept JSON even without proper Content-Type
            # (many tools forget to set it). But log a warning.
            print(f"    Warning: Content-Type is '{content_type}', expected application/json")

        try:
            order_data = json.loads(request.body)
        except json.JSONDecodeError as exc:
            return error_response(400, "Bad Request", f"Invalid JSON: {exc}")

        # "Process" the order (in later chapters, this will hit a database)
        order_id = 42  # Hardcoded for now
        return json_response(
            {
                "order_id": order_id,
                "status": "confirmed",
                "items": order_data.get("items", []),
                "message": "Order placed successfully (simulated)",
            },
            status_code=201,
            status_text="Created",
        )

    # --- Route: OPTIONS (CORS preflight) ---
    # Browsers send OPTIONS before cross-origin POST/PUT/DELETE requests.
    if request.method == "OPTIONS":
        return build_response(
            204,
            "No Content",
            {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
                "Access-Control-Max-Age": "86400",
            },
        )

    # --- 404: No route matched ---
    return error_response(
        404,
        "Not Found",
        f"No route matches {request.method} {request.path}",
    )


# ---------------------------------------------------------------------------
# Server Loop
# ---------------------------------------------------------------------------

def handle_client(client_sock: socket.socket, client_addr: tuple[str, int]) -> None:
    """
    Handle a single HTTP client connection.

    This reads the raw bytes, parses the HTTP request, routes it, builds
    the response, and sends it back. Then it checks if the client wants
    to keep the connection alive for more requests (HTTP/1.1 default).

    In production, this would be wrapped in an async event loop or run
    in a thread pool. Here, it is synchronous for clarity.
    """
    print(f"[+] Connection from {client_addr[0]}:{client_addr[1]}")

    try:
        # For keep-alive support, we loop and handle multiple requests
        # on the same connection. HTTP/1.1 defaults to keep-alive.
        while True:
            # Read raw bytes from the TCP connection.
            #
            # WARNING: This is simplified! In production, you cannot assume
            # that one recv() gives you a complete HTTP request. TCP is a
            # byte stream --- you might get half a request, or two requests
            # concatenated. A proper implementation:
            # 1. Read until you find CRLFCRLF (end of headers).
            # 2. Parse Content-Length from headers.
            # 3. Read exactly Content-Length more bytes for the body.
            # 4. Handle Transfer-Encoding: chunked (variable-length body).
            #
            # For our educational server with small requests, one recv()
            # is usually sufficient.
            raw = client_sock.recv(BUFFER_SIZE)

            if not raw:
                # Client closed the connection.
                print(f"[-] Client {client_addr[0]}:{client_addr[1]} disconnected")
                break

            # Parse the raw bytes into a structured HttpRequest.
            # This is where all the string splitting happens.
            try:
                request = parse_request(raw)
            except ValueError as exc:
                print(f"    Parse error: {exc}")
                response = error_response(
                    400, "Bad Request", f"Could not parse HTTP request: {exc}"
                )
                client_sock.sendall(response)
                break

            # Print what we received (like a verbose access log)
            print(f"    {request}")
            for name, value in request.headers.items():
                print(f"    Header: {name}: {value}")
            if request.body:
                print(f"    Body ({len(request.body)} chars): {request.body[:200]}")

            # Route the request to a handler and get the response bytes.
            response = route_request(request)

            # Send the response back over TCP.
            # sendall() ensures all bytes are transmitted, looping internally
            # if the kernel's send buffer is full.
            client_sock.sendall(response)

            # Check if the client wants to keep the connection alive.
            # HTTP/1.1 defaults to keep-alive; HTTP/1.0 defaults to close.
            connection = request.headers.get("connection", "").lower()
            if connection == "close" or request.version == "HTTP/1.0":
                print(f"    Connection: close (client requested)")
                break

            # If keep-alive, loop back to recv() for the next request.
            # The client can send another request on the same TCP connection,
            # saving the cost of a new handshake.

    except ConnectionResetError:
        print(f"[!] Connection reset by {client_addr[0]}:{client_addr[1]}")
    except BrokenPipeError:
        print(f"[!] Broken pipe with {client_addr[0]}:{client_addr[1]}")
    except Exception as exc:
        print(f"[!] Unexpected error: {exc}")
        # Try to send a 500 response
        try:
            response = error_response(
                500, "Internal Server Error", "An unexpected error occurred"
            )
            client_sock.sendall(response)
        except Exception:
            pass  # Socket might already be dead
    finally:
        client_sock.close()


def main() -> None:
    """
    Start the HTTP server.

    This is structurally identical to the TCP echo server --- because an HTTP
    server IS a TCP server that speaks a specific text protocol. The only
    differences are:
    1. We parse incoming bytes as HTTP requests instead of echoing them.
    2. We format outgoing bytes as HTTP responses instead of echoing.

    The socket setup (socket, bind, listen, accept) is exactly the same.
    """
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    server_sock.bind((HOST, PORT))
    server_sock.listen(BACKLOG)

    print("=" * 60)
    print(f"HTTP Server (from scratch) listening on http://{HOST}:{PORT}")
    print("=" * 60)
    print()
    print("This server is built from raw TCP sockets. No framework.")
    print("It does exactly what FastAPI/Flask do under the hood.")
    print()
    print("Try these commands:")
    print(f"  curl -v http://localhost:{PORT}/")
    print(f"  curl -v http://localhost:{PORT}/api/v1/status")
    print(f"  curl -v -X POST http://localhost:{PORT}/api/v1/orders \\")
    print(f"       -H 'Content-Type: application/json' \\")
    print(f"       -d '{{\"items\": [1, 2, 3]}}'")
    print()
    print(f"Or open http://localhost:{PORT}/ in your browser.")
    print("Press Ctrl+C to stop.")
    print()

    try:
        while True:
            client_sock, client_addr = server_sock.accept()
            handle_client(client_sock, client_addr)
    except KeyboardInterrupt:
        print("\n[*] Server shutting down.")
    finally:
        server_sock.close()
        print("[*] Socket closed. Goodbye.")


if __name__ == "__main__":
    main()

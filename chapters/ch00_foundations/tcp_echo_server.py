"""
TCP Echo Server -- Understanding Sockets from First Principles
==============================================================

This module implements a bare TCP echo server using Python's `socket` module.
Every line maps to a real syscall that the kernel executes. There is no framework,
no abstraction --- just the same primitives that nginx, PostgreSQL, and every
network service on earth use under the hood.

Run it:
    uv run python -m chapters.ch00_foundations.tcp_echo_server

Test it:
    telnet localhost 9000
    # or
    nc localhost 9000
    # Type anything and press Enter. The server echoes it back.

What you will learn:
    - What socket(), bind(), listen(), accept(), recv(), send() actually do
    - How the kernel manages connection state
    - The relationship between file descriptors and network connections
    - Why blocking I/O means one-client-at-a-time (and what we do about it)
"""

from __future__ import annotations

import socket
import sys


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HOST = "127.0.0.1"  # Listen only on loopback. Use "0.0.0.0" for all interfaces.
PORT = 9000          # Ephemeral-safe port. Ports < 1024 require root on Unix.
BACKLOG = 5          # Max queued connections waiting in the accept queue.
BUFFER_SIZE = 4096   # Max bytes to read per recv() call. Not a message boundary!


def create_server_socket() -> socket.socket:
    """
    Create, configure, and bind a TCP server socket.

    Under the hood, this triggers these kernel operations:

    1. socket(AF_INET, SOCK_STREAM, 0)
       - Allocates a new socket structure in the kernel.
       - AF_INET = IPv4 address family.
       - SOCK_STREAM = TCP (reliable, ordered byte stream).
       - Returns a file descriptor (integer) that refers to this socket.
       - At this point, the socket is not bound to any address.

    2. setsockopt(SO_REUSEADDR)
       - Tells the kernel: "Allow binding to an address that is in TIME_WAIT state."
       - Without this, if you restart the server quickly, bind() fails with
         "Address already in use" because the old socket is still in TIME_WAIT
         (waiting the standard 2*MSL = 60 seconds).
       - This is safe for servers. It does NOT allow two active sockets on the
         same port --- it only allows reuse of TIME_WAIT sockets.

    3. setsockopt(TCP_NODELAY)
       - Disables Nagle's algorithm.
       - Nagle buffers small writes, waiting for an ACK before sending more data.
       - Combined with delayed ACKs on the receiver, this creates 40ms latency spikes.
       - For an echo server (small messages, low latency), we want every write to
         go out immediately.

    4. bind(("127.0.0.1", 9000))
       - Associates the socket with a specific IP address and port.
       - The kernel records this in the socket structure and in its port-to-socket
         lookup table.
       - After bind(), the socket "owns" this (ip, port) pair. No other socket can
         bind to the same pair (unless SO_REUSEADDR/SO_REUSEPORT is set).

    5. listen(backlog=5)
       - Transitions the socket from CLOSED to LISTEN state.
       - The kernel creates two queues:
         a) SYN queue (half-open connections): clients that have sent SYN but
            haven't completed the 3-way handshake yet.
         b) Accept queue (fully established connections): clients that have
            completed the handshake but haven't been picked up by accept() yet.
       - The `backlog` parameter historically controlled the SYN queue size.
            On modern Linux, it controls the accept queue size. The SYN queue
            size is controlled by net.ipv4.tcp_max_syn_backlog.
       - If both queues are full, new SYN packets are dropped (or SYN cookies
            are used if enabled).
    """
    # Step 1: Create the socket
    # AF_INET  = IPv4
    # SOCK_STREAM = TCP (as opposed to SOCK_DGRAM for UDP)
    server_sock = socket.socket(
        family=socket.AF_INET,
        type=socket.SOCK_STREAM,
        proto=0,  # 0 = let the OS pick the protocol (TCP for SOCK_STREAM)
    )

    # Step 2: Allow address reuse (avoid "Address already in use" on restart)
    # SOL_SOCKET = socket-level option (not protocol-specific)
    # SO_REUSEADDR = the specific option
    # 1 = enabled (True)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # Step 3: Disable Nagle's algorithm for low-latency echoing
    # IPPROTO_TCP = TCP-level option
    # TCP_NODELAY = disable Nagle
    server_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    # Step 4: Bind to the address
    server_sock.bind((HOST, PORT))

    # Step 5: Start listening
    # After this call, the kernel will respond to SYN packets on this port
    # by completing the 3-way handshake automatically, without our code being
    # involved. The application only gets notified when accept() returns.
    server_sock.listen(BACKLOG)

    return server_sock


def handle_client(client_sock: socket.socket, client_addr: tuple[str, int]) -> None:
    """
    Handle a single client connection: read data, echo it back, repeat until
    the client disconnects.

    This function demonstrates the core read/write loop that EVERY network
    server implements, whether it is nginx, Redis, or PostgreSQL.

    Key concepts:
    - recv() maps to the read() syscall on the socket fd.
    - send() maps to the write() syscall on the socket fd.
    - TCP is a BYTE STREAM, not a message stream. recv() might return:
        - Fewer bytes than you asked for (partial read)
        - Multiple messages concatenated together
        - Data split across arbitrary boundaries
      There is no concept of "one recv() = one message." If you need message
      boundaries, YOU must implement framing (length prefix, delimiter, etc.).
      HTTP uses CRLF delimiters for headers and Content-Length for the body.

    Parameters
    ----------
    client_sock : socket.socket
        The connected socket returned by accept(). This is a NEW socket,
        distinct from the listening socket. It represents one specific
        connection (one 4-tuple).
    client_addr : tuple
        (ip, port) of the client. The port is the client's ephemeral port,
        chosen by the client's kernel.
    """
    print(f"[+] Connection from {client_addr[0]}:{client_addr[1]}")

    # We can inspect the socket's file descriptor. This is the integer the
    # kernel uses to track this connection. Every I/O operation on this socket
    # goes through this fd.
    print(f"    Socket fd: {client_sock.fileno()}")

    # We can also see the full 4-tuple that identifies this connection:
    local = client_sock.getsockname()   # (local_ip, local_port)
    remote = client_sock.getpeername()  # (remote_ip, remote_port)
    print(f"    4-tuple: {local[0]}:{local[1]} <-> {remote[0]}:{remote[1]}")

    try:
        while True:
            # recv(BUFFER_SIZE) asks the kernel: "Give me up to BUFFER_SIZE
            # bytes from this socket's receive buffer."
            #
            # What happens in the kernel:
            # 1. If the receive buffer has data, copy up to BUFFER_SIZE bytes
            #    to userspace and return immediately.
            # 2. If the receive buffer is empty, BLOCK (put this thread to sleep)
            #    until data arrives or the connection closes.
            # 3. If the connection has been closed by the peer (FIN received),
            #    return b"" (empty bytes).
            #
            # The blocking behavior is why this simple server can only handle
            # one client at a time. While we are blocked in recv() waiting for
            # Client A to type, we cannot accept or serve Client B.
            # Solutions: threads, processes, or event loops (select/poll/epoll).
            # We will explore these in later chapters.
            data = client_sock.recv(BUFFER_SIZE)

            if not data:
                # Empty bytes means the client closed the connection (sent FIN).
                # The TCP 4-way close has begun:
                #   Client -> FIN -> Server   (client calls close())
                #   Server -> ACK -> Client   (kernel auto-ACKs)
                #   Server -> FIN -> Client   (we call close() below)
                #   Client -> ACK -> Server   (client kernel auto-ACKs)
                # After this, the server-side socket enters TIME_WAIT (because
                # we are the side that sends the second FIN in this case).
                print(f"[-] Client {client_addr[0]}:{client_addr[1]} disconnected")
                break

            # Decode the bytes to see what the client sent.
            # TCP carries raw bytes; interpretation is up to the application.
            # We assume UTF-8 text here, but it could be anything.
            message = data.decode("utf-8", errors="replace")
            print(f"    Received ({len(data)} bytes): {message.rstrip()}")

            # Echo the data back. send() copies data to the kernel's send buffer
            # for this socket. The kernel will segment it into TCP segments,
            # add headers, and transmit when the congestion window allows.
            #
            # Important: send() may not send all bytes in one call! It returns
            # the number of bytes actually copied to the send buffer. If the
            # send buffer is full (receiver's window is zero, or congestion
            # window is exhausted), send() may block or send fewer bytes.
            #
            # For production code, use sendall() which loops until all bytes
            # are sent. We use sendall() here for correctness.
            response = f"echo: {message}"
            client_sock.sendall(response.encode("utf-8"))

    except ConnectionResetError:
        # The client sent a RST (reset) instead of a clean FIN close.
        # This happens when the client crashes, or when the client's kernel
        # decides to abort (e.g., client calls close() with data still in
        # the receive buffer on the client side, triggering SO_LINGER behavior).
        print(f"[!] Connection reset by {client_addr[0]}:{client_addr[1]}")

    except BrokenPipeError:
        # We tried to write to a socket that the other side has closed.
        # The kernel delivered a SIGPIPE signal (which Python converts to
        # this exception). In C, you would set MSG_NOSIGNAL or handle SIGPIPE.
        print(f"[!] Broken pipe with {client_addr[0]}:{client_addr[1]}")

    finally:
        # Close the socket. This:
        # 1. Decrements the fd reference count.
        # 2. If refcount hits 0, sends FIN to the peer (beginning graceful close).
        # 3. Frees the kernel socket structure (after TIME_WAIT expires).
        # 4. Frees the fd number for reuse.
        client_sock.close()


def main() -> None:
    """
    Main server loop: accept connections and handle them one at a time.

    This is the simplest possible server architecture: single-threaded,
    blocking, sequential. It has an obvious limitation --- while handling
    one client, all other clients wait in the accept queue.

    Production servers solve this with:
    - Threading: one thread per connection (simple but memory-heavy)
    - Forking: one process per connection (isolated but expensive)
    - Event loop: one thread, non-blocking I/O with epoll/kqueue (efficient
      but complex --- this is what nginx, Node.js, and asyncio do)

    We will build all of these in later chapters. For now, this sequential
    approach makes the syscall sequence crystal clear.
    """
    server_sock = create_server_socket()

    print("=" * 60)
    print(f"TCP Echo Server listening on {HOST}:{PORT}")
    print("=" * 60)
    print()
    print("This is a raw TCP server. No HTTP, no framing, just bytes.")
    print("Connect with:  telnet localhost 9000")
    print("           or: nc localhost 9000")
    print()
    print("Type anything and press Enter. The server echoes it back.")
    print("Press Ctrl+C to stop the server.")
    print()

    try:
        while True:
            # accept() is the most important call here. What it does:
            #
            # 1. Check the accept queue (fully-established connections).
            # 2. If the queue is empty, BLOCK (sleep) until a connection
            #    completes the 3-way handshake.
            # 3. When a connection is ready:
            #    a) Remove it from the accept queue.
            #    b) Create a NEW socket (new fd) representing this specific
            #       connection. The original server_sock stays in LISTEN state
            #       and continues accepting new connections.
            #    c) Return (new_socket, client_address).
            #
            # The new socket inherits some properties from the listening socket
            # but is a completely independent socket with its own buffers,
            # its own TCP state (ESTABLISHED), and its own 4-tuple.
            client_sock, client_addr = server_sock.accept()

            # Handle this client. Because handle_client() blocks (it loops
            # on recv()), we cannot accept another client until this one
            # disconnects. This is the fundamental limitation of blocking I/O.
            handle_client(client_sock, client_addr)

    except KeyboardInterrupt:
        print("\n[*] Server shutting down.")
    finally:
        # Close the listening socket. Any connections in the SYN queue or
        # accept queue are dropped. Already-established connections (handled
        # by handle_client) are unaffected because they have their own sockets.
        server_sock.close()
        print("[*] Socket closed. Goodbye.")


if __name__ == "__main__":
    main()

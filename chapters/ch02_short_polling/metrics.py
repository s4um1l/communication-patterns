"""Chapter 02 — Short Polling Metrics Simulation

Simulates N clients polling at various intervals and calculates the true cost
of short polling at scale. No server needed — this is pure math brought to life.

Outputs:
    - Per-interval breakdown: total requests, useful requests, waste ratio
    - Bandwidth calculations
    - CPU cost estimates
    - ASCII bar charts comparing different poll intervals

Run with:
    uv run python -m chapters.ch02_short_polling.metrics
"""

from __future__ import annotations

import random


def simulate_order(poll_interval: float, order_duration: float = 1800.0, num_status_changes: int = 6) -> dict:
    """Simulate one client polling one order through its entire lifecycle.

    Args:
        poll_interval: seconds between polls
        order_duration: total order lifetime in seconds (default 30 min)
        num_status_changes: how many times status changes during the order

    Returns:
        dict with total_polls, useful_polls, wasted_polls
    """
    # Distribute status changes randomly across the order duration
    change_times = sorted(random.uniform(0, order_duration) for _ in range(num_status_changes))

    total_polls = 0
    useful_polls = 0
    current_change_idx = 0
    last_known_change_idx = -1  # haven't seen any status yet

    t = poll_interval  # first poll happens after one interval
    while t <= order_duration:
        total_polls += 1

        # How many status changes have happened by time t?
        while current_change_idx < len(change_times) and change_times[current_change_idx] <= t:
            current_change_idx += 1

        # Did the client learn something new?
        if current_change_idx > last_known_change_idx:
            useful_polls += 1
            last_known_change_idx = current_change_idx

        t += poll_interval

    return {
        "total_polls": total_polls,
        "useful_polls": useful_polls,
        "wasted_polls": total_polls - useful_polls,
    }


def run_simulation(
    num_clients: int,
    poll_interval: float,
    order_duration: float = 1800.0,
    num_status_changes: int = 6,
) -> dict:
    """Simulate many clients to get aggregate statistics."""
    totals = {"total_polls": 0, "useful_polls": 0, "wasted_polls": 0}

    for _ in range(num_clients):
        result = simulate_order(poll_interval, order_duration, num_status_changes)
        for key in totals:
            totals[key] += result[key]

    efficiency = (totals["useful_polls"] / totals["total_polls"] * 100) if totals["total_polls"] > 0 else 0

    # Bandwidth: ~1100 bytes per round trip (500 req headers + 300 resp headers + 300 body)
    bytes_per_poll = 1100
    total_bytes = totals["total_polls"] * bytes_per_poll
    wasted_bytes = totals["wasted_polls"] * bytes_per_poll

    # CPU: ~1ms per request
    cpu_ms_per_poll = 1.0
    total_cpu_ms = totals["total_polls"] * cpu_ms_per_poll

    # Requests per second (aggregate across all clients)
    rps = num_clients / poll_interval

    return {
        **totals,
        "num_clients": num_clients,
        "poll_interval": poll_interval,
        "efficiency_pct": efficiency,
        "total_bytes": total_bytes,
        "wasted_bytes": wasted_bytes,
        "rps": rps,
        "cpu_cores_needed": rps * cpu_ms_per_poll / 1000,
        "polls_per_client": totals["total_polls"] / num_clients,
    }


def bar(value: float, max_value: float, width: int = 50, char: str = "#") -> str:
    """Create an ASCII bar of proportional length."""
    if max_value == 0:
        return ""
    filled = int(value / max_value * width)
    return char * filled


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def main() -> None:
    print()
    print("=" * 75)
    print("  SHORT POLLING METRICS SIMULATION")
    print("  Quantifying the cost at scale")
    print("=" * 75)

    # ------------------------------------------------------------------
    # Simulation parameters
    # ------------------------------------------------------------------
    num_clients = 10_000
    order_duration = 1800.0  # 30 minutes
    num_status_changes = 6
    intervals = [1.0, 2.0, 5.0, 10.0, 30.0]

    print(f"\n  Simulating {num_clients:,} concurrent clients")
    print(f"  Order duration: {order_duration / 60:.0f} minutes")
    print(f"  Status changes per order: {num_status_changes}")
    print(f"  Simulating 100 sample clients per interval, extrapolating to {num_clients:,}")

    # ------------------------------------------------------------------
    # Run simulations at each interval
    # ------------------------------------------------------------------
    # Simulate 100 clients for statistical accuracy, then scale
    sample_size = 100
    results = []

    for interval in intervals:
        sim = run_simulation(sample_size, interval, order_duration, num_status_changes)
        # Scale up to full client count
        scale = num_clients / sample_size
        scaled = {
            "poll_interval": interval,
            "polls_per_client": sim["polls_per_client"],
            "total_polls": int(sim["total_polls"] * scale),
            "useful_polls": int(sim["useful_polls"] * scale),
            "wasted_polls": int(sim["wasted_polls"] * scale),
            "efficiency_pct": sim["efficiency_pct"],
            "total_bytes": int(sim["total_bytes"] * scale),
            "wasted_bytes": int(sim["wasted_bytes"] * scale),
            "rps": num_clients / interval,
            "cpu_cores_needed": num_clients / interval * 0.001,
        }
        results.append(scaled)

    # ------------------------------------------------------------------
    # Detailed breakdown table
    # ------------------------------------------------------------------
    print("\n" + "-" * 75)
    print("  DETAILED BREAKDOWN BY POLL INTERVAL")
    print("-" * 75)

    for r in results:
        print(f"\n  Poll Interval: {r['poll_interval']}s")
        print(f"  {'Polls per client:':<30} {r['polls_per_client']:>12,.0f}")
        print(f"  {'Total polls (all clients):':<30} {r['total_polls']:>12,}")
        print(f"  {'Useful polls:':<30} {r['useful_polls']:>12,}")
        print(f"  {'Wasted polls:':<30} {r['wasted_polls']:>12,}")
        print(f"  {'Efficiency:':<30} {r['efficiency_pct']:>11.2f}%")
        print(f"  {'Requests/second:':<30} {r['rps']:>12,.0f}")
        print(f"  {'Total bandwidth:':<30} {human_bytes(r['total_bytes']):>12}")
        print(f"  {'Wasted bandwidth:':<30} {human_bytes(r['wasted_bytes']):>12}")
        print(f"  {'CPU cores (polling only):':<30} {r['cpu_cores_needed']:>12.1f}")

    # ------------------------------------------------------------------
    # ASCII bar charts
    # ------------------------------------------------------------------
    print("\n" + "=" * 75)
    print("  REQUESTS PER SECOND (lower is better)")
    print("=" * 75)
    max_rps = max(r["rps"] for r in results)
    for r in results:
        label = f"  {r['poll_interval']:>5.0f}s"
        b = bar(r["rps"], max_rps, width=45)
        print(f"{label}  |{b}| {r['rps']:,.0f} req/s")

    print("\n" + "=" * 75)
    print("  EFFICIENCY % (higher is better)")
    print("=" * 75)
    for r in results:
        label = f"  {r['poll_interval']:>5.0f}s"
        b = bar(r["efficiency_pct"], 100, width=45)
        empty = 45 - len(b)
        print(f"{label}  |{b}{'.' * empty}| {r['efficiency_pct']:.2f}%")

    print("\n" + "=" * 75)
    print("  WASTED BANDWIDTH PER 30-MIN WINDOW (lower is better)")
    print("=" * 75)
    max_waste = max(r["wasted_bytes"] for r in results)
    for r in results:
        label = f"  {r['poll_interval']:>5.0f}s"
        b = bar(r["wasted_bytes"], max_waste, width=45)
        print(f"{label}  |{b}| {human_bytes(r['wasted_bytes'])}")

    print("\n" + "=" * 75)
    print("  CPU CORES NEEDED JUST FOR POLLING (lower is better)")
    print("=" * 75)
    max_cpu = max(r["cpu_cores_needed"] for r in results)
    for r in results:
        label = f"  {r['poll_interval']:>5.0f}s"
        b = bar(r["cpu_cores_needed"], max_cpu, width=45)
        print(f"{label}  |{b}| {r['cpu_cores_needed']:.1f} cores")

    # ------------------------------------------------------------------
    # The punchline
    # ------------------------------------------------------------------
    best = results[0]  # 1s interval (worst waste)
    print("\n" + "=" * 75)
    print("  THE PUNCHLINE")
    print("=" * 75)
    print()
    print(f"  At {num_clients:,} users polling every {best['poll_interval']}s:")
    print(f"    - {best['rps']:,.0f} requests per second hit your server")
    print(f"    - {best['wasted_polls']:,} of {best['total_polls']:,} polls are wasted")
    print(f"    - {human_bytes(best['wasted_bytes'])} of bandwidth is pure waste")
    print(f"    - {best['cpu_cores_needed']:.0f} CPU cores burn for 'nothing changed'")
    print()
    print("  Even at the most conservative interval (30s):")
    conservative = results[-1]
    print(f"    - Still {conservative['rps']:,.0f} req/s")
    print(f"    - Still {conservative['wasted_polls']:,} wasted polls")
    print(f"    - Still {human_bytes(conservative['wasted_bytes'])} of waste")
    print(f"    - And avg detection latency jumps to 15 seconds")
    print()

    # ------------------------------------------------------------------
    # Detection latency comparison
    # ------------------------------------------------------------------
    print("=" * 75)
    print("  DETECTION LATENCY (how long until client knows about a change)")
    print("=" * 75)
    print()
    print(f"  {'Interval':>10}  {'Avg Delay':>10}  {'Worst Case':>12}  {'Visualization'}")
    print(f"  {'--------':>10}  {'---------':>10}  {'----------':>12}  {'-' * 30}")
    for interval in intervals:
        avg = interval / 2
        worst = interval
        bar_len = int(worst / max(intervals) * 30)
        bar_str = "X" * bar_len
        print(f"  {interval:>9.0f}s  {avg:>9.1f}s  {worst:>11.0f}s  |{bar_str}")

    print()
    print("  X = time the client is UNAWARE that the status changed.")
    print("  Short polling trades server resources for lower latency,")
    print("  but it can never achieve instant notification.")
    print()

    # ------------------------------------------------------------------
    # Comparison: what if we only made requests when status changed?
    # ------------------------------------------------------------------
    print("=" * 75)
    print("  THE IDEAL: WHAT IF WE ONLY REQUESTED WHEN STATUS CHANGED?")
    print("=" * 75)
    print()
    ideal_requests = num_clients * num_status_changes
    ideal_bytes = ideal_requests * 1100
    actual_best = results[2]  # 5s interval as "reasonable" choice
    print(f"  Ideal scenario:  {ideal_requests:>12,} requests  ({human_bytes(ideal_bytes):>10})")
    print(f"  Polling at 5s:   {actual_best['total_polls']:>12,} requests  ({human_bytes(actual_best['total_bytes']):>10})")
    print(f"  Waste factor:    {actual_best['total_polls'] / ideal_requests:>12,.0f}x")
    print()
    print("  That waste factor is why Ch03 (Long Polling), Ch04 (SSE),")
    print("  and Ch05 (WebSockets) exist.")
    print()


if __name__ == "__main__":
    main()

"""Correctness Harness (Requirement 9).

Standalone async script that asserts the no-overselling invariant against the
durable database after a load/chaos run:

    For every course_section:  confirmed_count <= capacity

It also cross-checks the actual count of confirmed enrollment rows against the
section's confirmed_count column, so a divergence between the counter and the
real rows is surfaced too.

Exit code 0 on success, 1 on any violation (CI-friendly).
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

DSN = os.environ.get(
    "CLASSQ_POSTGRES_DSN",
    "postgresql://{user}:{pw}@{host}:{port}/{db}".format(
        user=os.environ.get("CLASSQ_POSTGRES_USER", "classq"),
        pw=os.environ.get("CLASSQ_POSTGRES_PASSWORD", "classq"),
        host=os.environ.get("CLASSQ_POSTGRES_HOST", "localhost"),
        port=os.environ.get("CLASSQ_POSTGRES_PORT", "5432"),
        db=os.environ.get("CLASSQ_POSTGRES_DB", "classq"),
    ),
)

# ANSI colors (Windows Terminal / VS Code support these).
RED = "\033[1;37;41m"
GREEN = "\033[1;30;42m"
DIM = "\033[2m"
RESET = "\033[0m"


async def main() -> int:
    conn = await asyncpg.connect(DSN)
    try:
        # Primary invariant check (R9.1/9.2): confirmed_count > capacity anywhere?
        oversold = await conn.fetch(
            """
            SELECT section_id, section_label, capacity, confirmed_count
            FROM course_sections
            WHERE confirmed_count > capacity
            ORDER BY section_id
            """
        )

        # Per-section report rows, plus an actual-rows cross-check.
        report = await conn.fetch(
            """
            SELECT
                cs.section_id,
                cs.section_label,
                cs.capacity,
                cs.confirmed_count,
                (
                    SELECT count(*) FROM enrollments e
                    WHERE e.section_id = cs.section_id
                      AND e.status = 'confirmed'
                ) AS actual_confirmed
            FROM course_sections cs
            ORDER BY cs.confirmed_count DESC, cs.section_id
            """
        )
    finally:
        await conn.close()

    # --- Report ---
    print()
    print("=" * 72)
    print("  ClassQ Correctness Harness — No-Overselling Invariant (R9)")
    print("=" * 72)
    header = f"  {'section':<14} {'label':<8} {'cap':>5} {'confirmed':>10} {'rows':>6}  status"
    print(header)
    print("  " + "-" * 68)
    for row in report:
        sid = str(row["section_id"])[:8]
        cap = row["capacity"]
        confirmed = row["confirmed_count"]
        actual = row["actual_confirmed"]
        ok = confirmed <= cap
        rows_match = actual == confirmed
        status = "OK" if ok else "OVERSOLD"
        if ok and not rows_match:
            status = "OK (counter≠rows)"
        marker = "" if ok else "  <== VIOLATION"
        print(
            f"  {sid:<14} {row['section_label']:<8} {cap:>5} {confirmed:>10} "
            f"{actual:>6}  {status}{marker}"
        )
    print("  " + "-" * 68)

    violations = len(oversold)
    print()
    if violations > 0:
        print(RED + "  ╔" + "═" * 60 + "╗" + RESET)
        print(RED + "  ║  ❌  NO-OVERSELLING INVARIANT VIOLATED                       ║" + RESET)
        print(RED + f"  ║  {violations} section(s) have confirmed_count > capacity".ljust(61) + "║" + RESET)
        print(RED + "  ╚" + "═" * 60 + "╝" + RESET)
        for row in oversold:
            print(
                RED
                + f"    section {row['section_id']} ({row['section_label']}): "
                + f"{row['confirmed_count']} > capacity {row['capacity']}"
                + RESET
            )
        print()
        return 1

    print(GREEN + "  ╔" + "═" * 60 + "╗" + RESET)
    print(GREEN + "  ║  ✓  NO-OVERSELLING INVARIANT HELD                            ║" + RESET)
    print(GREEN + "  ║  every section: confirmed_count <= capacity                 ║" + RESET)
    print(GREEN + "  ╚" + "═" * 60 + "╝" + RESET)
    print()
    print(f"{DIM}  sections checked: {len(report)}{RESET}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

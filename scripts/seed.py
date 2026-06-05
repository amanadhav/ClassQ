"""Seed ClassQ PostgreSQL with a realistic CS prerequisite DAG and test data.

Idempotent: re-running deletes the previously seeded rows (by fixed UUIDs) in
foreign-key-safe order, then re-inserts them.

Prerequisite DAG (edge "A -> B" means B is a prerequisite of A):

    Linear Algebra (LA) ───────────────► Machine Learning (ML)
    Systems Programming (SP) ──────────► Binary Exploitation (BE)
    Machine Learning (ML) ─────────────► Advanced AI Security (AAS)
    Binary Exploitation (BE) ──────────► Advanced AI Security (AAS)

Base courses (no prerequisites): Linear Algebra, Systems Programming.
The seeded Student has a COMPLETED enrollment in Linear Algebra only.

Expected prerequisite-evaluation outcomes for that student:
  - Machine Learning      -> satisfied   (LA completed)
  - Binary Exploitation   -> unmet: {Systems Programming}
  - Advanced AI Security  -> unmet: {Systems Programming, Binary Exploitation, Machine Learning}
"""

from __future__ import annotations

import asyncio
import os

import asyncpg

# --- Connection (defaults match infrastructure/docker-compose.yml) ----------
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

# --- Fixed UUIDs so the seed is deterministic and easy to test against ------
STUDENT = "11111111-1111-1111-1111-111111111111"

LA = "0a000000-0000-0000-0000-000000000001"   # Linear Algebra        (base)
SP = "0a000000-0000-0000-0000-000000000002"   # Systems Programming   (base)
ML = "0a000000-0000-0000-0000-000000000003"   # Machine Learning
BE = "0a000000-0000-0000-0000-000000000004"   # Binary Exploitation
AAS = "0a000000-0000-0000-0000-000000000005"  # Advanced AI Security

WINDOW = "0b000000-0000-0000-0000-000000000001"

SEC = {
    LA: "0c000000-0000-0000-0000-000000000001",
    SP: "0c000000-0000-0000-0000-000000000002",
    ML: "0c000000-0000-0000-0000-000000000003",
    BE: "0c000000-0000-0000-0000-000000000004",
    AAS: "0c000000-0000-0000-0000-000000000005",
}

ENROLLMENT = "0d000000-0000-0000-0000-000000000001"

COURSES = [
    (LA, "MAT201", "Linear Algebra"),
    (SP, "CSE240", "Systems Programming"),
    (ML, "CSE475", "Machine Learning"),
    (BE, "CSE466", "Binary Exploitation"),
    (AAS, "CSE598", "Advanced AI Security"),
]

# (course, prereq) — 4 edges forming a valid DAG.
PREREQS = [
    (ML, LA),
    (BE, SP),
    (AAS, ML),
    (AAS, BE),
]


async def main() -> None:
    conn = await asyncpg.connect(DSN)
    try:
        async with conn.transaction():
            # --- Clean previously seeded rows (FK-safe order) ---
            section_ids = list(SEC.values())
            await conn.execute(
                "DELETE FROM outbox WHERE section_id = ANY($1::uuid[])", section_ids
            )
            # Clear ALL enrollments for seeded sections (includes chaos-bot rows)
            await conn.execute(
                "DELETE FROM enrollments WHERE section_id = ANY($1::uuid[])", section_ids
            )
            # Reset confirmed_count to 0 so DB and Redis are back in sync
            await conn.execute(
                "UPDATE course_sections SET confirmed_count = 0 WHERE window_id = $1::uuid",
                WINDOW,
            )
            await conn.execute(
                "DELETE FROM course_sections WHERE window_id = $1::uuid", WINDOW
            )
            course_ids = [c[0] for c in COURSES]
            await conn.execute(
                "DELETE FROM prerequisites WHERE course_id = ANY($1::uuid[])",
                course_ids,
            )
            await conn.execute(
                "DELETE FROM courses WHERE course_id = ANY($1::uuid[])", course_ids
            )
            await conn.execute(
                "DELETE FROM registration_windows WHERE window_id = $1::uuid", WINDOW
            )
            await conn.execute(
                "DELETE FROM students WHERE student_id = $1::uuid", STUDENT
            )

            # --- Student ---
            await conn.execute(
                """
                INSERT INTO students (student_id, external_id, display_name)
                VALUES ($1::uuid, $2, $3)
                """,
                STUDENT,
                "asu-1000001",
                "Ada Lovelace",
            )

            # --- Courses ---
            await conn.executemany(
                """
                INSERT INTO courses (course_id, course_code, title)
                VALUES ($1::uuid, $2, $3)
                """,
                COURSES,
            )

            # --- Prerequisites (the DAG edges) ---
            await conn.executemany(
                """
                INSERT INTO prerequisites (course_id, prereq_course_id)
                VALUES ($1::uuid, $2::uuid)
                """,
                PREREQS,
            )

            # --- Registration window (open now) ---
            await conn.execute(
                """
                INSERT INTO registration_windows
                    (window_id, name, opens_at, closes_at, drain_seconds, max_queue, state)
                VALUES
                    ($1::uuid, $2, now() - interval '1 day', now() + interval '30 days',
                     30, 1000, 'open')
                """,
                WINDOW,
                "Fall 2025 Registration",
            )

            # --- Course sections (one per course, capacity 30) ---
            section_rows = [
                (SEC[course_id], course_id, WINDOW, "A", 30)
                for (course_id, _code, _title) in COURSES
            ]
            await conn.executemany(
                """
                INSERT INTO course_sections
                    (section_id, course_id, window_id, section_label, capacity)
                VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5)
                """,
                section_rows,
            )

            # --- One completed enrollment: student completed Linear Algebra ---
            await conn.execute(
                """
                INSERT INTO enrollments (enrollment_id, student_id, section_id, status)
                VALUES ($1::uuid, $2::uuid, $3::uuid, 'completed')
                """,
                ENROLLMENT,
                STUDENT,
                SEC[LA],
            )

        # --- Summary ---
        print("Seed complete.")
        print(f"  Student: {STUDENT} (Ada Lovelace) — completed Linear Algebra")
        print(f"  Window:  {WINDOW} (open)")
        print("  Courses / sections:")
        for course_id, code, title in COURSES:
            print(f"    {code:<7} {title:<22} course={course_id} section={SEC[course_id]}")
        print("  Prerequisite edges (course requires prereq):")
        names = {c[0]: c[2] for c in COURSES}
        for course_id, prereq_id in PREREQS:
            print(f"    {names[course_id]} requires {names[prereq_id]}")
        print()
        print("  Try the test route (course_id):")
        print(f"    satisfied : /test/prereq/{STUDENT}/{ML}   (Machine Learning)")
        print(f"    unmet     : /test/prereq/{STUDENT}/{BE}   (Binary Exploitation)")
        print(f"    unmet x3  : /test/prereq/{STUDENT}/{AAS}  (Advanced AI Security)")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

"""Prerequisite_Checker service (Requirement 4 / design.md "Prerequisite DAG BFS Design").

evaluate_prerequisites(student_id, course_id) traverses the prerequisite DAG
from the requested course using breadth-first search, determines whether the
student satisfies every reachable prerequisite, detects cycles, and caches the
result in Redis.

Graph convention (matches the `prerequisites` table and the seed):
    an edge stored as (course_id, prereq_course_id) means
    "prereq_course_id is a prerequisite of course_id".
The adjacency expansion for a course is therefore:
    SELECT prereq_course_id FROM prerequisites WHERE course_id = $1

Outcomes (R4):
    satisfied  -> all reachable prerequisites are completed by the student
    unmet      -> one or more reachable prerequisites are not completed
    invalid    -> the reachable subgraph contains a cycle (R4.5)
    error      -> the graph / completed-enrollment data could not be retrieved (R4.7)

Complexity: O(V + E) over the subgraph reachable from `course_id` — a single BFS
pass to collect the subgraph plus a Kahn topological pass for cycle detection.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

import asyncpg

from app.db import postgres, redis

Outcome = Literal["satisfied", "unmet", "invalid", "error"]

# Cache key templates (design.md Redis key design).
_VER_KEY = "classq:prereq:ver:{course}"
_RES_KEY = "classq:prereq:res:{stu}:{course}:{ver}"
_RES_TTL_SECONDS = 300


@dataclass
class PrereqResult:
    outcome: Outcome
    satisfied: bool
    unmet: list[str] = field(default_factory=list)  # course_ids not yet completed
    cached: bool = False

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "satisfied": self.satisfied,
            "unmet": self.unmet,
            "cached": self.cached,
        }


async def _get_version(course_id: str) -> int:
    """Current cache version for a course (0 if never bumped)."""
    client = redis.get_client()
    raw = await client.get(_VER_KEY.format(course=course_id))
    return int(raw) if raw is not None else 0


async def evaluate_prerequisites(student_id: str, course_id: str) -> PrereqResult:
    """Evaluate whether `student_id` satisfies the prerequisites of `course_id`."""
    client = redis.get_client()

    # --- Cache lookup (R4.4): result keys embed the course version tag ---
    try:
        version = await _get_version(course_id)
        res_key = _RES_KEY.format(stu=student_id, course=course_id, ver=version)
        cached_raw = await client.get(res_key)
    except Exception:
        # Treat a cache-read failure as a miss; fall through to recompute.
        version = 0
        res_key = None
        cached_raw = None

    if cached_raw is not None:
        data = json.loads(cached_raw)
        return PrereqResult(
            outcome=data["outcome"],
            satisfied=data["satisfied"],
            unmet=data.get("unmet", []),
            cached=True,
        )

    # --- Load graph + completed enrollments from Postgres (R4.7 on failure) ---
    try:
        adjacency, completed = await _load_graph_and_completed(student_id, course_id)
    except Exception:
        return PrereqResult(outcome="error", satisfied=False)

    # --- BFS over the reachable subgraph (R4.1, R4.3) ---
    reachable_prereqs, subgraph = _bfs_reachable(course_id, adjacency)

    # --- Cycle detection over the collected subgraph (R4.5) ---
    if _has_cycle(subgraph):
        return PrereqResult(outcome="invalid", satisfied=False)

    # --- Satisfaction: every reachable prerequisite must be completed (R4.2) ---
    unmet = sorted(c for c in reachable_prereqs if c not in completed)
    if unmet:
        result = PrereqResult(outcome="unmet", satisfied=False, unmet=unmet)
    else:
        result = PrereqResult(outcome="satisfied", satisfied=True)

    # --- Cache the freshly computed result (R4.4) ---
    if res_key is not None:
        try:
            await client.set(
                res_key,
                json.dumps(
                    {
                        "outcome": result.outcome,
                        "satisfied": result.satisfied,
                        "unmet": result.unmet,
                    }
                ),
                ex=_RES_TTL_SECONDS,
            )
        except Exception:
            # Caching is best-effort; never fail the evaluation on a cache write.
            pass

    return result


async def _load_graph_and_completed(
    student_id: str, course_id: str
) -> tuple[dict[str, list[str]], set[str]]:
    """Load the full prerequisite adjacency map and the student's completed courses.

    Loading the whole edge set once keeps this O(V+E) without per-node round
    trips; the BFS then walks only the subgraph reachable from `course_id`.
    """
    pool = postgres.get_pool()
    async with pool.acquire() as conn:
        edge_rows = await conn.fetch(
            "SELECT course_id, prereq_course_id FROM prerequisites"
        )
        completed_rows = await conn.fetch(
            """
            SELECT c.course_id
            FROM enrollments e
            JOIN course_sections c ON c.section_id = e.section_id
            WHERE e.student_id = $1::uuid AND e.status = 'completed'
            """,
            student_id,
        )

    adjacency: dict[str, list[str]] = {}
    for row in edge_rows:
        adjacency.setdefault(str(row["course_id"]), []).append(
            str(row["prereq_course_id"])
        )

    completed = {str(row["course_id"]) for row in completed_rows}
    return adjacency, completed


def _bfs_reachable(
    course_id: str, adjacency: dict[str, list[str]]
) -> tuple[set[str], dict[str, list[str]]]:
    """BFS from `course_id` over prerequisite edges.

    Returns:
      reachable_prereqs: every prerequisite course reachable (excludes the root),
                         each visited at most once.
      subgraph:          adjacency restricted to visited nodes (for cycle check).
    """
    reachable_prereqs: set[str] = set()
    subgraph: dict[str, list[str]] = {}
    visited: set[str] = {course_id}
    frontier: deque[str] = deque([course_id])

    while frontier:
        node = frontier.popleft()
        neighbors = adjacency.get(node, [])
        subgraph[node] = list(neighbors)
        for prereq in neighbors:
            if prereq != course_id:
                reachable_prereqs.add(prereq)
            if prereq not in visited:
                visited.add(prereq)
                frontier.append(prereq)

    return reachable_prereqs, subgraph


def _has_cycle(subgraph: dict[str, list[str]]) -> bool:
    """Kahn topological sort over the reachable subgraph; True if a cycle exists.

    Runs in O(V+E) over the subgraph. A plain BFS visited-set cannot tell a
    diamond (shared prerequisite via two paths) apart from a true cycle, so we
    use indegree elimination here to avoid false positives.
    """
    nodes: set[str] = set(subgraph.keys())
    for neighbors in subgraph.values():
        nodes.update(neighbors)

    indegree: dict[str, int] = {n: 0 for n in nodes}
    for node in subgraph:
        for prereq in subgraph[node]:
            indegree[prereq] += 1

    queue: deque[str] = deque(n for n in nodes if indegree[n] == 0)
    removed = 0
    while queue:
        node = queue.popleft()
        removed += 1
        for prereq in subgraph.get(node, []):
            indegree[prereq] -= 1
            if indegree[prereq] == 0:
                queue.append(prereq)

    # If not every node was removed, at least one cycle remains.
    return removed != len(nodes)

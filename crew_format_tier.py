"""
crew_format_tier.py — CrewAI Agent and Task that formats job routing records
returned by the export-tier skill into a human-readable table.

Usage (standalone):
    python crew_format_tier.py                  # reads from stdin (JSON)
    python crew_format_tier.py records.json     # reads from a file

Or import and use from another crew:
    from crew_format_tier import format_tier_task, formatter_agent
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any

from crewai import Agent, Task, Crew


# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

TIER_LABEL = {"T1": "T1 — Urgent", "T2": "T2 — Standard", "T3": "T3 — Low"}


def _fmt_deadline(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        diff = dt - now
        minutes = int(diff.total_seconds() / 60)
        tag = f"{dt.strftime('%Y-%m-%d %H:%M')} UTC"
        if minutes < 0:
            return f"{tag}  *** BREACHED ({abs(minutes)}m ago) ***"
        if minutes < 60:
            return f"{tag}  (in {minutes}m)"
        return f"{tag}  (in {minutes // 60}h {minutes % 60}m)"
    except ValueError:
        return str(value)


def _fmt_score(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_stalled(value: Any) -> str:
    if value is True or str(value).lower() in ("true", "1", "yes"):
        return "YES !"
    return "no"


def format_records_as_table(records: list[dict]) -> str:
    """
    Render a list of job routing dicts as a fixed-width text table.

    Expected keys per record (all optional except job_id and current_tier):
        job_id, current_tier, priority_score, sla_deadline, is_stalled,
        routed_at, status, assigned_recruiter_queue
    """
    if not records:
        return "No records to display."

    COLS = [
        ("Job ID",         "job_id",                  10),
        ("Tier",           "current_tier",             12),
        ("Score",          "priority_score",           8),
        ("SLA Deadline",   "sla_deadline",             42),
        ("Stalled",        "is_stalled",               8),
        ("Status",         "status",                   22),
    ]

    def cell(record: dict, key: str, label: str) -> str:
        raw = record.get(key)
        if key == "sla_deadline":
            return _fmt_deadline(raw)
        if key == "priority_score":
            return _fmt_score(raw)
        if key == "is_stalled":
            return _fmt_stalled(raw)
        if key == "current_tier":
            return TIER_LABEL.get(str(raw), str(raw) if raw is not None else "—")
        return str(raw) if raw is not None else "—"

    rows: list[list[str]] = []
    for rec in records:
        rows.append([cell(rec, key, label) for label, key, _ in COLS])

    col_widths = [
        max(len(label), max(len(r[i]) for r in rows))
        for i, (label, _, _) in enumerate(COLS)
    ]

    sep = "+-" + "-+-".join("-" * w for w in col_widths) + "-+"
    header = "| " + " | ".join(
        f"{label:<{col_widths[i]}}" for i, (label, _, _) in enumerate(COLS)
    ) + " |"

    lines = [sep, header, sep]
    for row in rows:
        lines.append(
            "| " + " | ".join(f"{cell:<{col_widths[i]}}" for i, cell in enumerate(row)) + " |"
        )
    lines.append(sep)

    by_tier: dict[str, int] = {}
    stalled = 0
    breached = 0
    for rec in records:
        t = rec.get("current_tier", "?")
        by_tier[t] = by_tier.get(t, 0) + 1
        if _fmt_stalled(rec.get("is_stalled")) == "YES !":
            stalled += 1
        dl = rec.get("sla_deadline")
        if dl:
            try:
                dt = datetime.fromisoformat(dl.replace("Z", "+00:00"))
                if dt < datetime.now(timezone.utc):
                    breached += 1
            except ValueError:
                pass

    summary_parts = [f"Total: {len(records)}"]
    for tier in ("T1", "T2", "T3"):
        if tier in by_tier:
            summary_parts.append(f"{tier}: {by_tier[tier]}")
    if stalled:
        summary_parts.append(f"Stalled: {stalled}")
    if breached:
        summary_parts.append(f"SLA Breached: {breached}")

    lines.append("  " + "  |  ".join(summary_parts))
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# CrewAI Agent
# ─────────────────────────────────────────────────────────────────────────────

formatter_agent = Agent(
    role="Job Routing Report Formatter",
    goal=(
        "Take raw job routing records from the scoring pipeline and present "
        "them as a clear, human-readable table that operations staff can act on immediately."
    ),
    backstory=(
        "You are a data presentation specialist embedded in the staffing operations "
        "workflow. You receive structured routing data (tier assignments, priority "
        "scores, SLA deadlines, stall flags) and render it in a format that lets "
        "recruiters and managers instantly see which jobs need attention."
    ),
    verbose=False,
    allow_delegation=False,
)


# ─────────────────────────────────────────────────────────────────────────────
# CrewAI Task
# ─────────────────────────────────────────────────────────────────────────────

format_tier_task = Task(
    description=(
        "Format the job routing records provided in {records} into a human-readable "
        "table. The table must include columns for Job ID, Tier, Priority Score, "
        "SLA Deadline (with time-remaining or breach notice), Stalled flag, and "
        "Job Status. Append a one-line summary showing total job count, per-tier "
        "counts, and any stall or SLA breach counts. Use the format_records_as_table "
        "helper to produce the output."
    ),
    expected_output=(
        "A fixed-width text table with a header row, one data row per job, a "
        "separator line, and a summary footer. SLA deadlines must show remaining "
        "time or a breach notice. Stalled jobs must be clearly flagged."
    ),
    agent=formatter_agent,
)


# ─────────────────────────────────────────────────────────────────────────────
# Standalone entry point
# ─────────────────────────────────────────────────────────────────────────────

def _load_records(argv: list[str]) -> list[dict]:
    if len(argv) > 1:
        with open(argv[1]) as f:
            data = json.load(f)
    else:
        data = json.load(sys.stdin)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "records" in data:
        return data["records"]
    raise ValueError("Input must be a JSON list of records or {\"records\": [...]}")


def main() -> None:
    records = _load_records(sys.argv)
    print(format_records_as_table(records))


if __name__ == "__main__":
    main()

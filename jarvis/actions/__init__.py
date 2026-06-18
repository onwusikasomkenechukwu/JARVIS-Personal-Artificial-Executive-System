"""Phase 2+ action/report entry points, each invoked via its own CLI.

Side-effecting actions (e.g. `send_email`) compose the full safety spine —
authorization, out-of-band confirmation, idempotency, decorrelated verification —
rather than relying on any single check.

`digest` is the exception that proves the boundary: it is read-and-report only (Level
0/1), so it carries no send/confirm spine — it has no side effect to gate. Its security
property is upstream of rendering: external calendar-invite descriptions are excluded
structurally by type (see `providers/calendar_state.py`), never reaching the report."""

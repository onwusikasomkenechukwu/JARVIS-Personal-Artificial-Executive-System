# JARVIS — Personal Artificial Executive System

A persistent personal AI executive system: it understands, plans, acts, monitors,
remembers, and coordinates across a user's digital and physical environments — while
keeping the human in control.

JARVIS is a continuously operating personal operating system, not a question-answering
chatbot. **Core objective:** reduce human operational workload while maintaining human
oversight and control.

> Full architecture and decision record: [`docs/JARVIS-architecture.pdf`](docs/JARVIS-architecture.pdf)

## Security thesis

The defining risk of this system is not a conventional web vulnerability. It is that JARVIS
reads **attacker-influenceable content** (web pages, emails, documents) and holds
**high-consequence capabilities** (money, locks, devices) in the same system. Every design
decision is evaluated against one question:

> If an attacker controls the text JARVIS is reading, what is the worst thing they can make it do?

## Core design principles

1. **Human remains in control** — actions are risk-tiered (Level 0–5); higher risk requires stronger authorization.
2. **Plan before acting** — every request follows Intent → Plan → Review → Execute → Verify.
3. **Continuous world model** — provenance-tagged representation of user, digital, and physical state.
4. **Verify everything** — no agent verifies its own work; verification is decorrelated from untrusted input.
5. **Capability and exposure are separated** — the component that reads untrusted content is never the one that holds high-risk capabilities.
6. **Untrusted content is data, never instruction** — enforced structurally, not by prompting.
7. **Default-safe on ambiguity** — timeouts, conflicts, and auth failures resolve to *no action*.

## Architecture layers

| Layer | Responsibility |
|-------|----------------|
| 1. Conversational | Voice/text dialogue, multimodal understanding (the only channel that can *instruct*) |
| 2. Agentic | Planner, Research, Project Manager, Memory, Scheduling, Safety, Auditor, Introspection agents — split into **untrusted-read** and **privileged-action** trust zones |
| 3. Operating System | Runtime Governor, Tool Router (default-deny allowlist), File/Browser/Comms/Device/Cloud/Smart-Home agents |
| 4. Embodied | Vision, gesture, audio perception, robotics (Level 4 minimum, hardware kill path) |

## Authorization levels

| Level | Scope | Confirmation |
|-------|-------|--------------|
| 0 | Read-only | None |
| 1 | Drafting / recommendations | None |
| 2 | Non-destructive actions | Single confirmation |
| 3 | Communication / publishing | Strong confirmation |
| 4 | Financial / physical actions | Multi-factor (out-of-band) |
| 5 | Emergency override | Explicit predefined permissions |

**Provenance gate:** any action derived from untrusted content is escalated by at least one level
and can never exceed Level 2 automatically, no matter what the content claims.

## Development roadmap

Capability does not ship ahead of its controls.

- **Phase 1 — Personal AI Assistant:** conversation, memory, tool calling, browser control. *(instruction/data separation, credential non-exposure, tool allowlist, memory provenance, fact lifecycle)*
- **Phase 2 — Executive Agent:** planning, project management, scheduling, research. *(trust-boundary isolation, decorrelated auditor, provenance pre-filter, Runtime Governor, Skill Registry)*
- **Phase 3 — Personal OS:** device control, smart home, world model. *(Level 4 hard ceilings, autonomy budget governors)*
- **Phase 4 — Embodied Intelligence:** vision, gesture, robotics. *(hardware kill path, force/rate ceilings)*
- **Phase 5 — Autonomous Executive:** predictive assistance, cross-system orchestration. *(audit-trail tamper-evidence, red-team pass)*

## Status

🚧 **Phase 1 in progress.** Phase 1 is deliberately small: a single async Python process.
The single most important deliverable is **milestone 0 — the browser reliability gate**: if
real-world browser control isn't reliable enough, the rest of the architecture doesn't matter.

### Phase 1 scope (single process)

1. Drive a browser to navigate / read / fill / click (Playwright, async).
2. **Measure its own reliability** over 100 repeated runs of a task.
3. Carry every action as a **typed request with a provenance label** (`jarvis/requests.py`).
4. Route requests through a **default-deny tool allowlist parameterized by provenance** (`jarvis/router.py`).
5. Store memory in Postgres with **provenance tags + fact expiration** (`jarvis/memory/`).
6. Gate higher-risk actions through an **out-of-band confirmation** step (`jarvis/confirm.py`).

| Milestone | Module | Acceptance test |
|-----------|--------|-----------------|
| 0 — Browser + reliability harness (**the gate**) | `tools/browser.py`, `harness/reliability.py` | `tests/test_reliability_harness.py` |
| 1 — Typed request + provenance escalation | `requests.py`, `provenance.py` | `tests/test_provenance.py` |
| 2 — Default-deny tool router | `router.py` | `tests/test_router.py` |
| 3 — Memory with provenance + expiration | `memory/` | `tests/test_memory_expiration.py` |
| 4 — Out-of-band confirmation gate | `confirm.py` | `tests/test_confirm.py` |

### Setup

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate     |  macOS/Linux:  source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium          # browser binary for milestone 0
cp .env.example .env                 # then edit (Postgres URL etc.)
```

### Run the tests (logic milestones 1–4 + harness mechanics)

```bash
pytest
```

These run without a browser or database (the harness test uses a stub browser; the
memory tests cover the pure expiration/policy logic).

### Run the reliability gate (milestone 0 — manual)

```bash
# Stage A — validate the harness against a scrape-friendly site (expect ~99%+):
python -m jarvis.harness.reliability --target books --n 100

# Stage B — the gating brittleness number, against Wikipedia:
python -m jarvis.harness.reliability --target wikipedia --n 100
```

**Interpretation.** Stage A proves the harness's own `harness`-class error rate is ~0.
Stage B is the number that gates the project — but Wikipedia is cooperative, so treat it
as a *floor* on difficulty, not a representative one. If the Stage B `browser`-class
failure rate is unacceptable (discuss the line, e.g. >5%), **stop and reconsider** before
building further. Capability does not ship ahead of reliability.

### Out-of-band confirmation (milestone 4)

When the app requests a Level ≥2 action it writes a pending request; you approve it from
a **separate** terminal/process so the requesting path can't self-approve:

```bash
python -m jarvis.confirm list
python -m jarvis.confirm approve <id>
python -m jarvis.confirm deny <id>
```

## License

TBD.

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

- **Phase 1 — Personal AI Assistant** ✅ **complete & validated:** browser control, typed requests, memory, tool calling. *(instruction/data separation, credential non-exposure, tool allowlist, memory provenance, fact lifecycle)*
- **Phase 2 — Executive Agent:** planning, project management, scheduling, research. *(trust-boundary isolation, decorrelated auditor, provenance pre-filter, Runtime Governor, Skill Registry)*
- **Phase 3 — Personal OS:** device control, smart home, world model. *(Level 4 hard ceilings, autonomy budget governors)*
- **Phase 4 — Embodied Intelligence:** vision, gesture, robotics. *(hardware kill path, force/rate ceilings)*
- **Phase 5 — Autonomous Executive:** predictive assistance, cross-system orchestration. *(audit-trail tamper-evidence, red-team pass)*

## Status

✅ **Phase 1 complete and validated.** A single async Python process. The defining
deliverable — **milestone 0, the browser reliability gate** — passed, so the rest of the
architecture earns the right to be built. Phase 2 has not started (it is gated on an
explicit decision that the reliability result justifies it).

**The gate result (`--target wikipedia --n 100`):**

```
runs:                  100
first-attempt success: 100  (100.0%)   <- no retry needed
total retry events:      0
browser-class:           0  (0.0%)     <- the gating signal
harness-class:           0  (0.0%)     <- ~0, so the number is trustworthy
```

100/100 on the first attempt with zero retries. *Caveat:* Wikipedia is cooperative, so
this is a **floor on difficulty, not a representative number** — it proves the executor
works when a site behaves, not that browsing is reliable against hostile sites.

### Milestones (all built, tested — 41 passing tests)

| Milestone | Module | Status | Tests |
|-----------|--------|--------|-------|
| 0 — Browser + reliability harness (**the gate**) | `tools/browser.py`, `harness/reliability.py` | ✅ passed gate | `tests/test_reliability_harness.py` |
| 1 — Typed request + provenance escalation | `requests.py`, `provenance.py` | ✅ | `tests/test_provenance.py` |
| 2 — Default-deny tool router | `router.py` | ✅ | `tests/test_router.py` |
| 3 — Memory with provenance + expiration | `memory/` | ✅ validated on real Postgres | `tests/test_memory_expiration.py` |
| 4 — Out-of-band confirmation gate | `confirm.py` | ✅ | `tests/test_confirm.py` |

The reliability harness reports **first-attempt** success separately from post-retry
success, so retries can never silently inflate the gate number (`browser_retries` default
is 3).

### Decorrelated-verification spike

A time-boxed experiment (not a shipped feature) testing whether an auditor can confirm a
side-effecting action through a channel that did **not** consume the untrusted input that
drove it. An untrusted-derived memory write (live Wikipedia read → `facts` store) is
verified by querying Postgres only — never re-reading the page. Measured: **executor
untrusted reads = 1, verifier = 0, VERIFIED = True**, confirmed durable on PostgreSQL 18.

Full honest findings — including why the green result came cheap and where the principle
is still unproven — in [`docs/verification-spike-findings.md`](docs/verification-spike-findings.md).
Code: `jarvis/spikes/decorrelated_verification.py`.

## Phase 2 (in progress) — provider state-read

The first **trusted, decorrelated ground-truth channel**: confirm a message shows as
*sent* in Gmail, reading only provider state (labels + the user's own headers) and
**never** message content. This is the ground-truth check a future send action's verifier
will call — built and proven in isolation before any send exists.

A provider API has two trust characters that must not be conflated:

- **Provider state** (labels, existence, provider/user-set headers) is the provider's own
  assertion, trusted-ish ground truth through an authenticated channel.
- **Message content** (bodies, attacker-authored inbound headers) is `UNTRUSTED_DERIVED`
  regardless of the authenticated envelope it arrives in — and is never read here.

The state/content boundary is enforced in **two independent layers**:

1. **Scope** — OAuth scope is `gmail.metadata` *only*. That scope grants labels/headers
   and cannot return a body; the provider enforces it at the API. Widening it is a
   separate, re-consented change — a scope-widening deliberately breaks a test.
2. **Type** — `MessageState` carries only `{exists, gmail_id, labels, headers, is_sent}`
   and structurally cannot hold a body, snippet, or payload. Belt and suspenders; the
   belt is Google's.

The metadata-scoped token is stored in the **credential vault** (`jarvis/vault.py`),
outside the repo; the agent code path holds a `SecretHandle`, never the raw token.

```bash
# First run opens a browser for consent (click through the "unverified app" warning in
# testing mode); later runs reuse the stored token.
python -m jarvis.providers.gmail_state --rfc-id "<CAKs...@mail.gmail.com>"
```

Prereqs (configured by the user, outside the repo): Gmail API enabled, OAuth consent
screen in testing mode with scope `gmail.metadata`, a Desktop-app client-secret JSON in
`JARVIS_GMAIL_CLIENT_SECRET_DIR` (default `C:\Users\onwus\.jarvis`), discovered by glob.
Code: `jarvis/providers/gmail_state.py`. Tests (mocked Gmail, no live API):
`tests/test_gmail_state.py`.

**Out of scope for this build:** any send/compose/modify, any body/snippet/inbound read,
any scope beyond `gmail.metadata`, and the verifier exercise itself (there is no
side-effecting action to verify yet).

### Setup

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate     |  macOS/Linux:  source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium          # browser binary for milestone 0 / the spike
cp .env.example .env                 # then edit (Postgres DSN etc.)
```

**Postgres** (needed only for the durable memory store / the spike's `--postgres` run).
Create the role + database the default DSN expects:

```sql
CREATE ROLE jarvis LOGIN PASSWORD 'jarvis';
CREATE DATABASE jarvis OWNER jarvis;
```

### Run the tests (41 — logic, harness mechanics, spike, Gmail state-read)

```bash
pytest
```

These run without a browser or database (stub browser; in-memory fact fixture; pure
expiration/policy logic).

### Run the reliability gate (milestone 0)

```bash
# Stage A — validate the harness against a scrape-friendly site (expect ~99%+):
python -m jarvis.harness.reliability --target books --n 100

# Stage B — the gating brittleness number, against Wikipedia:
python -m jarvis.harness.reliability --target wikipedia --n 100
```

Stage A proves the harness's own `harness`-class error rate is ~0; Stage B is the number
that gates the project. If the Stage B `browser`-class rate is unacceptable (discuss the
line, e.g. >5%), **stop and reconsider** before building further.

### Run the decorrelated-verification spike

```bash
python -m jarvis.spikes.decorrelated_verification              # in-memory fact store
python -m jarvis.spikes.decorrelated_verification --postgres   # durable, real Postgres
```

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

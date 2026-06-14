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

🚧 Early development — scaffolding Phase 1.

## License

TBD.

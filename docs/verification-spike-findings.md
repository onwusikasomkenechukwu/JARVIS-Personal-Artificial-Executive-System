# Decorrelated-Verification Spike — Findings

**Date:** 2026-06-14
**Status:** complete
**Question:** Can an auditor confirm a side-effecting action actually happened, by
checking ground-truth state through a channel that did *not* consume the same
untrusted input that drove the action — and what does that cost?

**Action class under test:** an untrusted-derived memory write. A browser reads a
fact off a live Wikipedia article (untrusted content), the fact is written to the
`facts` store tagged `UNTRUSTED_DERIVED`, and an auditor confirms the write by
querying the store only.

Code: [`jarvis/spikes/decorrelated_verification.py`](../jarvis/spikes/decorrelated_verification.py) ·
Tests: [`tests/test_verification_spike.py`](../tests/test_verification_spike.py)

---

## Outcome: (1) Decorrelation achieved — *for this action class*

The verifier confirmed the write through an independent channel (SQL against the fact
store) and never re-read the page. Measured against a live Wikipedia read:

| Measurement | Value |
|---|---|
| executor untrusted reads (incl. retries) | **1** |
| verifier untrusted reads | **0** |
| verification result | **VERIFIED = True** |
| duplicate rows | 1 (none) |

The verifier's zero is **structural, not lucky**: `verify_memory_write(repo, request,
fact_id)` is handed a repository and a typed request and *no browser/page*. It is
incapable of reaching the untrusted source. The spike measures the count anyway
(snapshotting `browser._read_attempts` around the verifier call) rather than asserting
it — and the delta is 0.

Because Task A's first-attempt run showed **0 retries** on the cooperative read path,
the executor consumes the untrusted input exactly **once** per action. The retry-driven
multiple-read path therefore did not trigger here; the harness counts it if it ever does.

### What was run

- **In-memory path (real read):** live Wikipedia read → write → verify against the
  in-memory fact repository. Result: `executor=1, verifier=0, VERIFIED=True`.
- **Postgres durability path (confirmed 2026-06-14):** the same flow against real
  PostgreSQL 18 via `python -m jarvis.spikes.decorrelated_verification --postgres`,
  exercising `AsyncpgFactRepository` against the actual `facts` table. **Identical
  result** (`executor=1, verifier=0, VERIFIED=True`); the row survives a real
  transactional store. Durability does not change the decorrelation finding, as
  predicted.
- **Milestone-3 memory store, end-to-end on Postgres (confirmed 2026-06-14):** stable
  fact never expires; a volatile fact past its interval reads back stale *and* the
  `stale` column is persisted (flag-not-delete); an untrusted write without review is
  rejected; an untrusted write with review is stored tagged `UNTRUSTED_DERIVED`. This
  was previously only covered at the pure-logic level.
- **Tests:** 6 spike tests run against the in-memory fixture (no Postgres needed),
  covering clean-verify-true, missing-verify-false, duplicate-detected,
  untrusted-without-review rejected, content-mismatch-fails, and the
  verifier-reads-no-page property.

---

## Double-write (the retry-double-action seam)

**Can duplicates occur?** Yes. `write_untrusted_fact` always `INSERT`s and the `facts`
table has no uniqueness constraint or idempotency key. A blindly retried side-effecting
write would produce two rows for the same logical fact.

**Does the verifier catch it?** Yes. It queries `find_logical_duplicates` and a count
> 1 forces `verified = False` (test: `test_duplicate_write_is_detected`). Ground-truth
state reveals the double-write that the action itself was oblivious to — which is
exactly the argument for checking state instead of trusting the executor's narrative.

**Honest limit:** detection is *after the fact*. For a benign memory write that is fine.
For a non-benign side-effecting action (a payment, a sent message), a double-action has
already done harm by the time the auditor sees two rows. The spike could only
demonstrate *detection* because it used a benign action class; it did **not** prove
detection is *timely enough to matter* for a harmful one. Prevention (idempotency key /
unique constraint) is the real fix and is a Phase 2 concern — not built here.

---

## The honest cost — where the principle strained

The green result is real but it came cheap **because the action class is the favorable
one**, and that is the most important thing this spike has to say.

1. **The ground truth lived in a system-controlled store, not in the untrusted medium.**
   "Did a row get written" is answerable in Postgres, which the untrusted web page never
   touches. Decorrelation was nearly free here. The architecture's hard case is the
   opposite: actions whose only ground truth lives *back in the untrusted medium* — "did
   my email actually send?" (read the sent-folder web UI), "did the form submit?" (read
   the confirmation page). Verifying those tends to require re-reading a web surface,
   which **is** the untrusted channel. This spike validated the easy end of the spectrum
   and did not test the hard end. That hard end is where decorrelation will actually
   strain, and it remains unproven.

2. **The verifier checks faithful execution, not truth — by design, and this must not be
   oversold.** It confirms "the system stored what the typed request said it would," not
   "the stored fact is true." If the page were poisoned to say something false, the
   verifier still returns `VERIFIED = True`: the system faithfully stored a
   provenance-tagged piece of untrusted content. Decorrelated verification **does not
   launder untrusted content into trusted content** — the `UNTRUSTED_DERIVED` provenance
   tag remains the actual defense. The verifier protects write-integrity, nothing more.

3. **Both compared artifacts descend from the same untrusted read.** `request.args`
   (content) and the stored row both trace back to the one browser read. The verifier
   checks they match — i.e. that nothing corrupted the value between request and store —
   not that the value was independently re-derived. This is correct (it is a write-
   integrity check, deliberately not a truth check), but it means "content_matches" is
   weaker than the word "verify" might suggest to a casual reader.

4. **Defining `untrusted_read_count` required a judgement call.** It counts `read()`
   attempts (content extraction). `navigate()` also loads the page; it is treated as
   page-load, not content-consumption. Reasonable, but a different definition would move
   the number.

---

## Recommendation

**Decorrelated verification looks buildable for Phase 2 — but only for the action-class
family it was tested on**, and the roadmap should say so rather than assume the
principle generalizes.

- **Buildable now:** actions whose ground truth lives in a **system-controlled store**
  (DB writes, file writes, internal state transitions). For these, the verifier reaches
  state through a channel the untrusted input never used, and decorrelation holds at
  zero extra untrusted reads. Adopt it for this family in Phase 2.
- **Needs design before being relied on:** actions whose ground truth lives **back in
  the untrusted medium** (web confirmations, third-party UI status). For these,
  decorrelation requires a *trusted side-channel* — an authenticated provider API whose
  response is not attacker-shaped, not a re-scrape of a web page. If no such channel
  exists for an action, that is finding (2) for *that* action, and it should be surfaced
  as "this action cannot be decorrelated-verified" rather than papered over by letting
  the auditor re-read the page.
- **Before any non-benign side-effecting action is retried:** add write idempotency
  (unique constraint or idempotency key). The verifier detecting a double-write is a
  backstop, not a substitute for not doing the action twice.

**Bottom line:** the principle is sound and cheap on the favorable half of the action
space, which is enough to build Phase 2's memory and file paths on. It is unproven on
the half the architecture was actually worried about, and Phase 2 should treat
"verify against a trusted channel" as a per-action-class question with a real answer,
not a universal guarantee.

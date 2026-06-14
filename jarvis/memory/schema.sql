-- JARVIS Phase 1 memory schema.
-- Every fact carries provenance (source + trust_label) and an expiration policy
-- keyed to fact_type. Untrusted-source content cannot enter durable memory without
-- review (enforced in store.py, not by the DB).

CREATE TABLE IF NOT EXISTS facts (
    id          BIGSERIAL PRIMARY KEY,
    content     TEXT        NOT NULL,
    source      TEXT        NOT NULL,                 -- url / file path / "user"
    trust_label TEXT        NOT NULL,                 -- USER_DIRECT | UNTRUSTED_DERIVED
    fact_type   TEXT        NOT NULL,                 -- volatile | stable
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ,                          -- NULL for stable facts
    stale       BOOLEAN     NOT NULL DEFAULT false,   -- flagged, never auto-deleted
    reviewed    BOOLEAN     NOT NULL DEFAULT false    -- required true for untrusted writes
);

CREATE INDEX IF NOT EXISTS idx_facts_expires_at ON facts (expires_at);
CREATE INDEX IF NOT EXISTS idx_facts_trust_label ON facts (trust_label);

-- E004 — agent local H2S : preuves locales d'effacement en coupure (lot 3, E188 ; §8 B11).
--
--   1. Relevé signé par période (30 minutes) : charge utile exacte, signature de la clé d'identité (logicielle ou TPM),
--      empreinte, empreinte du relevé précédent (chaîne : une période retirée ou modifiée se voit).
--   2. Buffer : type LOCAL_PROOF.
--
-- Rejouable ; appliquée par h2s_agent.storage.migrate.

CREATE TABLE IF NOT EXISTS local_proofs (
    proof_id      BIGSERIAL PRIMARY KEY,
    period_start  TIMESTAMPTZ NOT NULL,
    period_end    TIMESTAMPTZ NOT NULL,
    payload       TEXT NOT NULL,
    signature     VARCHAR(100) NOT NULL,
    key_algorithm VARCHAR(20) NOT NULL,
    proof_hash    VARCHAR(64) NOT NULL,
    prev_hash     VARCHAR(64),
    offline       BOOLEAN NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_local_proofs_period UNIQUE (period_start),
    CONSTRAINT uq_local_proofs_hash UNIQUE (proof_hash),
    CONSTRAINT ck_local_proofs_values CHECK (period_end > period_start AND proof_hash ~ '^[0-9a-f]{64}$'
        AND (prev_hash IS NULL OR prev_hash ~ '^[0-9a-f]{64}$') AND key_algorithm IN ('Ed25519', 'ECDSA-P256'))
);

ALTER TABLE local_buffer DROP CONSTRAINT IF EXISTS ck_local_buffer_values;
ALTER TABLE local_buffer ADD CONSTRAINT ck_local_buffer_values
    CHECK (entity_type IN ('MEASUREMENT', 'METRIC', 'STATE', 'DECISION_REPORT', 'ALERT', 'GRADIENT', 'LOCAL_PROOF')
       AND priority BETWEEN 1 AND 4 AND retry_count >= 0);

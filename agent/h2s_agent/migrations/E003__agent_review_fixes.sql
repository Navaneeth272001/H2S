-- E003 — agent local H2S : correctifs de la double relecture (D258).
--
--   1. Durée de chaque exécution conservée : un ordre manuel du technicien de 10 minutes ne dure pas une heure.
--   2. Petit état persistant de l'agent : curseur du lot d'ordres, plus haute heure du cloud vue (horloge, D256).
--
-- Rejouable ; appliquée par h2s_agent.storage.migrate.

ALTER TABLE local_decisions ADD COLUMN IF NOT EXISTS duration_s INTEGER;
ALTER TABLE local_decisions DROP CONSTRAINT IF EXISTS ck_local_decisions_duration;
ALTER TABLE local_decisions ADD CONSTRAINT ck_local_decisions_duration CHECK (duration_s IS NULL OR duration_s >= 0);

CREATE TABLE IF NOT EXISTS agent_kv (
    key        VARCHAR(50) PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

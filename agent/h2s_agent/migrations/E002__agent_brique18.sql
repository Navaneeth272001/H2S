-- E002 — agent local H2S, brique 18 : ce que le schéma client (E001, copie de db/edge/agent_local_postgres.sql) n'a pas.
--
--   1. Buffer : identifiant de message (idempotence côté cloud, D251), prochaine tentative (reprise, D252), types et
--      priorités bornés.
--   2. Cache des décisions : ordre signé tel que reçu (charge utile, signature, clé), batterie, validité (D250).
--   3. Décisions exécutées : sources de la version « exécutant » (D247), référence de l'ordre cloud, règle de rejet.
--   4. Règles et profils par batterie ; identité de l'agent ; modèles fédérés signés ; contributions idempotentes.
--
-- Rejouable ; appliquée par h2s_agent.storage.migrate.

-- 1. buffer
ALTER TABLE local_buffer ADD COLUMN IF NOT EXISTS message_id UUID NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE local_buffer ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE local_buffer ADD COLUMN IF NOT EXISTS event_at TIMESTAMPTZ;
CREATE UNIQUE INDEX IF NOT EXISTS uq_local_buffer_message ON local_buffer (message_id);
CREATE INDEX IF NOT EXISTS ix_local_buffer_pending ON local_buffer (priority, created_at, buffer_id) WHERE NOT synced;
ALTER TABLE local_buffer DROP CONSTRAINT IF EXISTS ck_local_buffer_values;
ALTER TABLE local_buffer ADD CONSTRAINT ck_local_buffer_values
    CHECK (entity_type IN ('MEASUREMENT', 'METRIC', 'STATE', 'DECISION_REPORT', 'ALERT', 'GRADIENT')
       AND priority BETWEEN 1 AND 4 AND retry_count >= 0);

-- 2. cache des décisions
ALTER TABLE decision_cache ADD COLUMN IF NOT EXISTS stock_id INTEGER;
ALTER TABLE decision_cache ADD COLUMN IF NOT EXISTS origin VARCHAR(20);
ALTER TABLE decision_cache ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ;
ALTER TABLE decision_cache ADD COLUMN IF NOT EXISTS payload TEXT;
ALTER TABLE decision_cache ADD COLUMN IF NOT EXISTS signature VARCHAR(100);
ALTER TABLE decision_cache ADD COLUMN IF NOT EXISTS key_id VARCHAR(20);
CREATE UNIQUE INDEX IF NOT EXISTS uq_decision_cache_decision ON decision_cache (decision_id);
ALTER TABLE decision_cache DROP CONSTRAINT IF EXISTS ck_decision_cache_values;
ALTER TABLE decision_cache ADD CONSTRAINT ck_decision_cache_values
    CHECK (action IN ('CHARGE', 'DISCHARGE', 'STANDBY') AND (power_w IS NULL OR power_w >= 0)
       AND (expires_at IS NULL OR expires_at > received_at - interval '1 day'));

-- 3. décisions exécutées
ALTER TABLE local_decisions ALTER COLUMN source SET DEFAULT 'CLOUD_LIVE';
UPDATE local_decisions SET source = 'CLOUD_LIVE' WHERE source NOT IN ('CLOUD_LIVE', 'CLOUD_CACHE', 'MANUAL', 'SAFE_STANDBY');
ALTER TABLE local_decisions ADD COLUMN IF NOT EXISTS decision_ref VARCHAR(100);
ALTER TABLE local_decisions ADD COLUMN IF NOT EXISTS stock_id INTEGER;
ALTER TABLE local_decisions ADD COLUMN IF NOT EXISTS rejection_rule VARCHAR(20);
ALTER TABLE local_decisions DROP CONSTRAINT IF EXISTS ck_local_decisions_values;
ALTER TABLE local_decisions ADD CONSTRAINT ck_local_decisions_values
    CHECK (source IN ('CLOUD_LIVE', 'CLOUD_CACHE', 'MANUAL', 'SAFE_STANDBY')
       AND execution_status IN ('PENDING', 'EXECUTED', 'FAILED', 'REJECTED')
       AND action IN ('CHARGE', 'DISCHARGE', 'STANDBY'));
CREATE INDEX IF NOT EXISTS ix_local_decisions_stock ON local_decisions (stock_id, created_at DESC);

-- 4. règles, profils, identité, modèles, contributions
ALTER TABLE local_security_rules ADD COLUMN IF NOT EXISTS stock_id INTEGER;
CREATE UNIQUE INDEX IF NOT EXISTS uq_local_security_rules_stock_rule ON local_security_rules (stock_id, rule_code);
ALTER TABLE local_battery_profiles ADD COLUMN IF NOT EXISTS stock_id INTEGER;
CREATE UNIQUE INDEX IF NOT EXISTS uq_local_battery_profiles_stock ON local_battery_profiles (stock_id);

CREATE TABLE IF NOT EXISTS agent_identity (
    singleton           BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    agent_uuid          UUID NOT NULL,
    site_id             INTEGER NOT NULL,
    site_name           VARCHAR(255),
    cloud_url           VARCHAR(255) NOT NULL,
    decision_key_id     VARCHAR(20) NOT NULL,
    decision_public_key VARCHAR(64) NOT NULL,
    gradient_public_key TEXT NOT NULL,
    enrolled_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE local_models ADD COLUMN IF NOT EXISTS weights JSONB;
ALTER TABLE local_models ADD COLUMN IF NOT EXISTS payload TEXT;
ALTER TABLE local_models ADD COLUMN IF NOT EXISTS signature VARCHAR(100);
ALTER TABLE local_gradients ADD COLUMN IF NOT EXISTS message_id UUID;
CREATE UNIQUE INDEX IF NOT EXISTS uq_local_gradients_message ON local_gradients (message_id) WHERE message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_local_measurements_series ON local_measurements (device_id, data_type, measurement_timestamp DESC);
CREATE INDEX IF NOT EXISTS ix_agent_status_history_open ON agent_status_history (started_at DESC) WHERE ended_at IS NULL;

-- =====================================================================
-- H2S — Agent local (brique 18) — schéma PostgreSQL embarqué
-- =====================================================================
-- GÉNÉRÉ automatiquement par tools/puml2sql.py --emit
-- Source de vérité : db/schema/h2s-schema.puml
-- NE PAS ÉDITER À LA MAIN : modifier le .puml puis régénérer.
-- =====================================================================


-- Cette base tourne SUR LE RASPBERRY PI, séparée du cloud. Dans le modèle,
-- `site_id` y est un VARCHAR sans clé étrangère : l'agent doit fonctionner
-- hors réseau, il ne peut pas dépendre d'une FK vers le cloud OVH. La
-- réconciliation passe par agent_sync_log.
--
-- ATTENTION — divergence entre les deux documents client :
--   * le diagramme PlantUML décrit 13 tables préfixées local_/agent_ ;
--   * la spec brique 18 décrit 8 tables, dont certaines portent d'autres
--     noms pour les mêmes concepts (measurements, aggregated_measurements,
--     buffer) et une table decision_cache absente du diagramme.
-- Les noms du diagramme font foi ici (modèle le plus complet), et
-- decision_cache est reprise de la spec car le mode dégradé en dépend.
-- À faire arbitrer par le client — cf. db/README.md.

-- 1 - Mesures locales collectées par l'Agent
CREATE TABLE IF NOT EXISTS local_measurements (
    measurement_id        BIGSERIAL PRIMARY KEY,
    site_id               VARCHAR(50) NOT NULL,
    device_id             VARCHAR(50) NOT NULL,
    device_type           VARCHAR(50) NOT NULL,
    data_type             VARCHAR(50) NOT NULL,
    "value"               FLOAT NOT NULL,
    unit                  VARCHAR(20) NOT NULL,
    measurement_timestamp TIMESTAMPTZ NOT NULL,
    received_timestamp    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    data_quality          VARCHAR(20) DEFAULT 'OK',
    source                VARCHAR(20) DEFAULT 'MQTT',
    CONSTRAINT unique_local_measurement UNIQUE (device_id, data_type, measurement_timestamp)
);

-- 2 - Données agrégées (pour l'inférence)
CREATE TABLE IF NOT EXISTS local_aggregated (
    agg_id             BIGSERIAL PRIMARY KEY,
    site_id            VARCHAR(50) NOT NULL,
    device_id          VARCHAR(50) NOT NULL,
    data_type          VARCHAR(50) NOT NULL,
    avg_value          FLOAT,
    min_value          FLOAT,
    max_value          FLOAT,
    count              INTEGER,
    aggregation_period VARCHAR(20) NOT NULL,
    period_start       TIMESTAMPTZ NOT NULL,
    period_end         TIMESTAMPTZ NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 3 - Buffer en cas de perte réseau
CREATE TABLE IF NOT EXISTS local_buffer (
    buffer_id     BIGSERIAL PRIMARY KEY,
    site_id       VARCHAR(50) NOT NULL,
    entity_type   VARCHAR(50) NOT NULL,
    entity_id     BIGINT,
    payload       JSONB NOT NULL,
    priority      INTEGER DEFAULT 2,
    synced        BOOLEAN DEFAULT FALSE,
    retry_count   INTEGER DEFAULT 0,
    max_retries   INTEGER DEFAULT 5,
    last_attempt  TIMESTAMPTZ,
    error_message TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 4 - Modèles IA embarqués
CREATE TABLE IF NOT EXISTS local_models (
    model_id              SERIAL PRIMARY KEY,
    site_id               VARCHAR(50) NOT NULL,
    model_version         VARCHAR(50) NOT NULL,
    model_type            VARCHAR(50) NOT NULL,
    model_path            VARCHAR(255) NOT NULL,
    model_hash            VARCHAR(64) NOT NULL,
    accuracy_metric       FLOAT,
    trained_on_data_until TIMESTAMPTZ,
    is_active             BOOLEAN DEFAULT TRUE,
    downloaded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT unique_local_model UNIQUE (site_id, model_type, model_version)
);

-- 5 - Gradients locaux (apprentissage fédéré)
CREATE TABLE IF NOT EXISTS local_gradients (
    gradient_id      BIGSERIAL PRIMARY KEY,
    site_id          VARCHAR(50) NOT NULL,
    round_id         INTEGER NOT NULL,
    model_version    VARCHAR(50) NOT NULL,
    model_type       VARCHAR(50) NOT NULL,
    gradient_data    JSONB NOT NULL,
    sample_count     INTEGER NOT NULL,
    data_range_start TIMESTAMPTZ,
    data_range_end   TIMESTAMPTZ,
    loss             FLOAT,
    accuracy         FLOAT,
    is_synced        BOOLEAN DEFAULT FALSE,
    synced_at        TIMESTAMPTZ,
    signature        VARCHAR(255),
    certificate      VARCHAR(255),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 6 - Décisions locales
CREATE TABLE IF NOT EXISTS local_decisions (
    decision_id      BIGSERIAL PRIMARY KEY,
    site_id          VARCHAR(50) NOT NULL,
    action           VARCHAR(20) NOT NULL,
    power_w          FLOAT,
    soc_before       FLOAT,
    soc_after        FLOAT,
    source           VARCHAR(20) DEFAULT 'LOCAL_IA',
    execution_status VARCHAR(20) DEFAULT 'PENDING',
    rejection_reason TEXT,
    command_id       VARCHAR(50),
    executed_at      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 7 - Commandes envoyées aux équipements
CREATE TABLE IF NOT EXISTS local_commands (
    command_id      BIGSERIAL PRIMARY KEY,
    decision_id     BIGINT,
    site_id         VARCHAR(50) NOT NULL,
    target_device   VARCHAR(50) NOT NULL,
    action          VARCHAR(20) NOT NULL,
    power_w         FLOAT,
    duration_s      INTEGER,
    priority        INTEGER DEFAULT 1,
    status          VARCHAR(20) DEFAULT 'SENT',
    mqtt_topic      VARCHAR(100),
    mqtt_payload    JSONB,
    sent_at         TIMESTAMPTZ,
    acknowledged_at TIMESTAMPTZ,
    executed_at     TIMESTAMPTZ,
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 8 - Métriques locales (monitoring)
CREATE TABLE IF NOT EXISTS local_metrics (
    metric_id    BIGSERIAL PRIMARY KEY,
    site_id      VARCHAR(50) NOT NULL,
    metric_type  VARCHAR(50) NOT NULL,
    "value"      FLOAT NOT NULL,
    unit         VARCHAR(20) NOT NULL,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 9 - Historique des états de l'Agent
CREATE TABLE IF NOT EXISTS agent_status_history (
    status_id       BIGSERIAL PRIMARY KEY,
    site_id         VARCHAR(50) NOT NULL,
    status          VARCHAR(20) NOT NULL,
    previous_status VARCHAR(20),
    reason          TEXT,
    details         JSONB,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMPTZ
);

-- 10 - Configuration locale
CREATE TABLE IF NOT EXISTS local_config (
    config_id    SERIAL PRIMARY KEY,
    site_id      VARCHAR(50) NOT NULL,
    config_key   VARCHAR(100) NOT NULL,
    config_value JSONB NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by   VARCHAR(50) DEFAULT 'system',
    CONSTRAINT unique_local_config UNIQUE (site_id, config_key)
);

-- 11 - Synchronisation Cloud
CREATE TABLE IF NOT EXISTS agent_sync_log (
    sync_id            BIGSERIAL PRIMARY KEY,
    site_id            VARCHAR(50) NOT NULL,
    sync_type          VARCHAR(30) NOT NULL,
    payload_size_bytes INTEGER,
    status             VARCHAR(20) DEFAULT 'PENDING',
    retry_count        INTEGER DEFAULT 0,
    error_message      TEXT,
    started_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at       TIMESTAMPTZ
);

-- 12 - Profils de batterie locaux (copie pour mode dégradé)
CREATE TABLE IF NOT EXISTS local_battery_profiles (
    profile_id        SERIAL PRIMARY KEY,
    site_id           VARCHAR(50) NOT NULL,
    profile_name      VARCHAR(100) NOT NULL,
    manufacturer      VARCHAR(100),
    capacity_wh       INTEGER,
    voltage_nominal_v FLOAT,
    soc_min           FLOAT DEFAULT 15,
    soc_max           FLOAT DEFAULT 95,
    eta_charge        FLOAT DEFAULT 0.95,
    eta_discharge     FLOAT DEFAULT 0.92,
    p_charge_max_w    INTEGER DEFAULT 500,
    p_discharge_max_w INTEGER DEFAULT 1000,
    r0                FLOAT,
    r1                FLOAT,
    c1                FLOAT,
    r2                FLOAT,
    c2                FLOAT,
    synced_from_cloud BOOLEAN DEFAULT FALSE,
    last_sync_at      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 13 - Règles SEC locales (copie pour mode dégradé)
CREATE TABLE IF NOT EXISTS local_security_rules (
    rule_id           SERIAL PRIMARY KEY,
    site_id           VARCHAR(50) NOT NULL,
    rule_code         VARCHAR(10) NOT NULL,
    rule_name         VARCHAR(50) NOT NULL,
    threshold_value   FLOAT,
    unit              VARCHAR(20),
    is_active         BOOLEAN DEFAULT TRUE,
    synced_from_cloud BOOLEAN DEFAULT FALSE,
    last_sync_at      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------
-- decision_cache — ABSENTE du diagramme PlantUML, reprise textuellement de
-- la spec brique 18 (§4.2, testée par les critères CA-18.13 et CA-18.15).
-- Sans elle, le mode dégradé n'a rien à rejouer quand le réseau tombe.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decision_cache (
    cache_id       BIGSERIAL PRIMARY KEY,
    site_id        VARCHAR(50) NOT NULL,
    decision_id    VARCHAR(100) NOT NULL,
    action         VARCHAR(20) NOT NULL,
    power_w        FLOAT,
    duration_s     INTEGER,
    priority       INTEGER,
    target_devices JSONB,
    received_at    TIMESTAMPTZ NOT NULL,
    expires_at     TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
COMMENT ON COLUMN decision_cache.action IS 'CHARGE, DISCHARGE, STANDBY';
COMMENT ON COLUMN decision_cache.expires_at IS 'Expiration du cache (défaut : 7 jours)';

CREATE INDEX IF NOT EXISTS idx_decision_cache_site_received
    ON decision_cache (site_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_decision_cache_expires
    ON decision_cache (expires_at);

-- Index de purge et de synchronisation
CREATE INDEX IF NOT EXISTS idx_local_measurements_measurement_timestamp ON local_measurements (measurement_timestamp);
CREATE INDEX IF NOT EXISTS idx_local_aggregated_period_start ON local_aggregated (period_start);
CREATE INDEX IF NOT EXISTS idx_local_buffer_last_attempt ON local_buffer (last_attempt);
CREATE INDEX IF NOT EXISTS idx_local_models_trained_on_data_until ON local_models (trained_on_data_until);
CREATE INDEX IF NOT EXISTS idx_local_gradients_data_range_start ON local_gradients (data_range_start);
CREATE INDEX IF NOT EXISTS idx_local_decisions_executed_at ON local_decisions (executed_at);
CREATE INDEX IF NOT EXISTS idx_local_commands_sent_at ON local_commands (sent_at);
CREATE INDEX IF NOT EXISTS idx_local_metrics_collected_at ON local_metrics (collected_at);
CREATE INDEX IF NOT EXISTS idx_agent_status_history_started_at ON agent_status_history (started_at);
CREATE INDEX IF NOT EXISTS idx_local_config_updated_at ON local_config (updated_at);
CREATE INDEX IF NOT EXISTS idx_agent_sync_log_started_at ON agent_sync_log (started_at);
CREATE INDEX IF NOT EXISTS idx_local_battery_profiles_last_sync_at ON local_battery_profiles (last_sync_at);
CREATE INDEX IF NOT EXISTS idx_local_security_rules_last_sync_at ON local_security_rules (last_sync_at);

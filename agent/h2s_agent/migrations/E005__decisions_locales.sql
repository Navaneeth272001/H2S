-- E005 — agent local H2S : décisions locales §5.1 en mode dégradé (lot 3, R5-20, C-9, E182 ; CDC §2.18).
--
--   1. Source d'exécution LOCAL_AUTO : décision prise par l'agent hors connexion, selon la règle §5.1 et SEC1-6 locales.
--   2. Prix spot day-ahead reçus du cloud, gardés par heure : la condition de prix d'une décision locale lit le prix de l'heure.
--
-- Rejouable ; appliquée par h2s_agent.storage.migrate.

ALTER TABLE local_decisions DROP CONSTRAINT IF EXISTS ck_local_decisions_values;
ALTER TABLE local_decisions ADD CONSTRAINT ck_local_decisions_values
    CHECK (source IN ('CLOUD_LIVE', 'CLOUD_CACHE', 'MANUAL', 'SAFE_STANDBY', 'LOCAL_AUTO')
       AND execution_status IN ('PENDING', 'EXECUTED', 'FAILED', 'REJECTED')
       AND action IN ('CHARGE', 'DISCHARGE', 'STANDBY'));

CREATE TABLE IF NOT EXISTS local_spot_prices (
    hour_start  TIMESTAMPTZ PRIMARY KEY,
    eur_per_kwh FLOAT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_local_spot_prices_values CHECK (eur_per_kwh BETWEEN -10 AND 10 AND mod(extract(epoch FROM hour_start)::numeric, 3600) = 0)
);

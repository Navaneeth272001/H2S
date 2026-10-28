#!/usr/bin/env bash
# H2S edge agent — diagnostic de la base locale (h2s_agent sur le Raspberry Pi).
#
# Repond en une passe aux questions qu'on se pose quand /api/site ou une requete psql
# montre des mesures figees : la collecte est-elle arretee, le publieur envoie-t-il un
# horodatage perime, l'agent a-t-il deja parle au cloud (data_quality = ESTIMATED),
# quelles alertes ont ete levees, le tampon grossit-il sans etre vide.
#
# Usage :
#   agent/tools/diagnose_local_db.sh                 # lecture de /etc/h2s-agent/agent.json
#   H2S_AGENT_CONFIG=/chemin/agent.json ... --dry-run  # verifie seulement la connexion resolue
#
# La connexion est deduite de "db_url" dans agent.json (le meme compte que l'agent) :
# inutile de connaitre le mot de passe de "postgres", qui n'en a pas.
set -uo pipefail

CONFIG="${H2S_AGENT_CONFIG:-/etc/h2s-agent/agent.json}"
STOCK_ID="${H2S_STOCK_ID:-1}"
DRY_RUN=0

for argument in "$@"; do
    case "$argument" in
        --dry-run) DRY_RUN=1 ;;
        -h | --help)
            sed -n '2,16p' "$0"
            exit 0
            ;;
        *)
            echo "option inconnue : $argument" >&2
            exit 2
            ;;
    esac
done

# Sans locale UTF-8, psql affiche « Â°C » alors que la base contient bien « °C ».
export LANG="${LANG:-C.UTF-8}"
export LC_ALL="${LC_ALL:-$LANG}"

[[ -e "$CONFIG" ]] || {
    echo "configuration introuvable : $CONFIG" >&2
    exit 2
}

read_config() {
    if [[ -r "$CONFIG" ]]; then
        cat "$CONFIG"
    else
        sudo cat "$CONFIG"
    fi
}

CONNECTION_ENV="$(read_config | python3 -c '
import json, shlex, sys, urllib.parse

config = json.load(sys.stdin)
url = config.get("db_url", "")
parts = urllib.parse.urlsplit(url)
if parts.scheme not in ("postgresql", "postgres"):
    sys.exit("db_url absente ou non PostgreSQL : %r" % url)
values = {
    "PGHOST": parts.hostname or "127.0.0.1",
    "PGPORT": str(parts.port or 5432),
    "PGUSER": urllib.parse.unquote(parts.username or ""),
    "PGPASSWORD": urllib.parse.unquote(parts.password or ""),
    "PGDATABASE": (parts.path or "/").lstrip("/") or "h2s_agent",
}
for key, value in values.items():
    print("export %s=%s" % (key, shlex.quote(value)))
' 2>&1)" || {
    echo "$CONNECTION_ENV" >&2
    exit 3
}

eval "$CONNECTION_ENV"
unset PGSERVICE PGSERVICEFILE PGHOSTADDR 2>/dev/null || true

if ((DRY_RUN)); then
    echo "configuration : $CONFIG"
    echo "connexion      : $PGUSER@$PGHOST:$PGPORT/$PGDATABASE (mot de passe masque)"
    exit 0
fi

command -v psql >/dev/null || {
    echo "psql absent : sudo apt install postgresql-client" >&2
    exit 4
}

if ! psql -X -q -t -A -c 'SELECT 1' >/dev/null 2>&1; then
    echo "connexion impossible : $PGUSER@$PGHOST:$PGPORT/$PGDATABASE" >&2
    psql -X -q -t -A -c 'SELECT 1' >&2
    exit 4
fi

q() {
    psql -X -q -A -F ' | ' -v ON_ERROR_STOP=1 -c "$1" 2>&1
}

section() {
    printf '\n=== %s ===\n' "$1"
}

section "Connexion"
echo "$PGUSER@$PGHOST:$PGPORT/$PGDATABASE"

section "Fraicheur des mesures"
q "SELECT count(*) AS lignes,
          to_char(max(measurement_timestamp), 'YYYY-MM-DD HH24:MI:SS TZ') AS dernier_releve,
          to_char(max(received_timestamp), 'YYYY-MM-DD HH24:MI:SS TZ')    AS dernier_insert,
          date_trunc('second', now() - max(received_timestamp))          AS age_insert
   FROM local_measurements;"

section "Derniere mesure par equipement"
q "SELECT device_id, count(*) AS lignes,
          max(measurement_timestamp) AS dernier_releve,
          max(received_timestamp)    AS dernier_insert
   FROM local_measurements
   GROUP BY device_id
   ORDER BY 4 DESC NULLS LAST
   LIMIT 15;"

section "Dernieres valeurs de la batterie stock:$STOCK_ID"
q "SELECT DISTINCT ON (data_type)
          data_type, value, unit, measurement_timestamp, data_quality
   FROM local_measurements
   WHERE device_id = 'stock:$STOCK_ID'
   ORDER BY data_type, measurement_timestamp DESC;"

section "Qualite des mesures (7 derniers jours, tous equipements)"
q "SELECT data_quality, count(*) AS lignes, max(measurement_timestamp) AS dernier_releve
   FROM local_measurements
   WHERE received_timestamp > now() - interval '7 days'
   GROUP BY data_quality
   ORDER BY 2 DESC;"

section "Alertes levees par l'agent"
q "SELECT created_at, payload->>'code' AS code, payload->>'severity' AS niveau,
          left(coalesce(payload->>'message', ''), 70) AS message, synced
   FROM local_buffer
   WHERE entity_type = 'ALERT'
   ORDER BY created_at DESC
   LIMIT 15;"

section "Historique d'etat de l'agent"
q "SELECT started_at, status, previous_status,
          left(coalesce(reason, ''), 50) AS raison, ended_at
   FROM agent_status_history
   ORDER BY started_at DESC
   LIMIT 8;"

section "Synchronisation cloud"
q "SELECT status, count(*) AS n, min(started_at) AS depuis, max(started_at) AS jusqu_a
   FROM agent_sync_log
   GROUP BY status
   ORDER BY 2 DESC;"

section "Tampon local (non synchronise)"
q "SELECT entity_type AS type, count(*) AS en_attente, min(event_at) AS plus_ancien
   FROM local_buffer
   WHERE NOT synced
   GROUP BY entity_type
   ORDER BY 2 DESC;"

section "Dernieres decisions locales"
q "SELECT created_at, action, execution_status, left(coalesce(rejection_reason, ''), 40) AS motif
   FROM local_decisions
   ORDER BY created_at DESC
   LIMIT 5;"

section "Processus"
pgrep -af 'h2s_agent|main_collector' || echo "aucun processus h2s_agent / main_collector"

section "Services"
for unit in h2s-agent h2s-collector mosquitto postgresql; do
    printf '%-14s %s\n' "$unit" "$(systemctl is-active "$unit" 2>/dev/null || true)"
done

section "Horloge systeme"
timedatectl show --property=NTPSynchronized --property=TimeUSec 2>/dev/null ||
    timedatectl 2>/dev/null | head -6 ||
    date -u

section "Ports PostgreSQL en ecoute"
(ss -ltnH 2>/dev/null || netstat -ltn 2>/dev/null) | grep -E ':543[0-9]' ||
    echo "aucun port 543x en ecoute"

section "Lecture"
AGES="$(psql -X -q -t -A -F ' ' -c "SELECT coalesce(extract(epoch FROM now() - max(received_timestamp))::bigint, -1),
                                            coalesce(extract(epoch FROM now() - max(measurement_timestamp))::bigint, -1)
                                     FROM local_measurements;")"
AGE_INSERT="${AGES%% *}"
AGE_MEASURE="${AGES##* }"

if [[ -z "${AGES:-}" || "$AGE_INSERT" == "-1" ]]; then
    echo "aucune mesure en base : la migration n'a peut-etre jamais tourne (h2s_agent migrate)."
elif ((AGE_INSERT > 300)); then
    echo "ingestion arretee depuis $((AGE_INSERT / 60)) min : rien n'entre en base."
    echo "  verifier le service collecteur, le courtier local et les sujets souscrits."
elif ((AGE_MEASURE > 300)); then
    echo "insertion recente ($((AGE_INSERT / 60)) min) mais horodatage de mesure vieux de $((AGE_MEASURE / 60)) min :"
    echo "  le publieur envoie un \"timestamp\" perime que l'agent accepte tant qu'il a moins de 24 h."
else
    echo "collecte a jour (dernier releve il y a $((AGE_MEASURE / 60)) min)."
fi

echo "data_quality = ESTIMATED signifie qu'aucune reponse horodatee du cloud n'a ete vue"
echo "depuis le demarrage de l'agent (cloud.py -> Clock.observe_server_time) : ce n'est pas un"
echo "probleme d'horloge locale. L'alerte CLOUD_UNSYNCED correspondante est levee chaque heure."

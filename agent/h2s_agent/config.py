"""Configuration de l'agent : fichier JSON du site (équipements, batteries, broker), surchargée par l'environnement.

Aucun secret dans le fichier : le mot de passe MQTT et le jeton de l'API locale sont lus dans des fichiers 0600.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEVICE_KINDS = ("shelly_gen2", "h2s_json", "powerlogic_modbus", "linky_tic")
#: Rôle d'un équipement dans le bilan du site (E181) : consommation (défaut) ou production (prise d'un onduleur, compteur d'injection).
DEVICE_ROLES = ("CONSUMPTION", "PRODUCTION")
#: Équipements dont la puissance active (``power_w``) entre dans le bilan des décisions locales ; la TIC Linky ne donne qu'une
#: puissance apparente et un index d'énergie.
BALANCE_KINDS = ("shelly_gen2", "h2s_json", "powerlogic_modbus")
KEY_STORES = ("software", "tpm2")


@dataclass(frozen=True)
class DeviceConf:
    """Équipement de mesure : identifiant cloud (table devices) et abonnement MQTT."""

    device_id: int
    kind: str
    mqtt_id: str | None = None
    topic: str | None = None
    #: Centrale Modbus (``modbus``) ou compteur Linky (``tic``) lus directement par l'agent (E172).
    options: dict | None = None
    #: ``CONSUMPTION`` ou ``PRODUCTION`` ; le rôle enregistré au cloud (brique 2), reçu avec les règles, prévaut.
    role: str = "CONSUMPTION"

    def topics(self) -> list[str]:
        if self.kind == "shelly_gen2":
            return [
                f"{self.mqtt_id}/status/switch:0",
                f"{self.mqtt_id}/status/em:0",
                f"{self.mqtt_id}/status/pm1:0",
                # Le sujet qu'une prise publie SANS réglage préalable : « Generic status update over MQTT »
                # est décoché en sortie d'usine, et les trois sujets ci-dessus restent alors muets.
                f"{self.mqtt_id}/events/rpc",
            ]
        if self.kind in ("powerlogic_modbus", "linky_tic"):
            return []
        return [self.topic] if self.topic else []


@dataclass(frozen=True)
class BatteryConf:
    """Batterie (table storages) : topic d'état au format H2S et préfixe des commandes /charge, /discharge, /standby."""

    stock_id: int
    status_topic: str
    command_prefix: str


@dataclass(frozen=True)
class MqttConf:
    host: str = "127.0.0.1"
    port: int = 1883
    username: str | None = None
    password_file: str | None = None
    tls: bool = False
    ca_file: str | None = None


@dataclass(frozen=True)
class CloudMqttConf:
    """Courtier du cloud (lot 5, canal temps réel) : distinct du broker local du site (``MqttConf``).

    ``host`` vide = canal désactivé, l'agent sonde comme avant. Le nom d'utilisateur vaut l'identifiant de l'agent par
    défaut : les règles d'accès du courtier n'ouvrent à un compte que les sujets ``h2s/agents/<son nom>/…`` (docs/MQTT.md).
    """

    host: str | None = None
    port: int = 8883
    tls: bool = True
    ca_file: str | None = None
    username: str | None = None
    password_file: str | None = None
    keepalive_s: int = 30
    publish_timeout_s: float = 2.0

    @property
    def enabled(self) -> bool:
        return bool(self.host)


@dataclass(frozen=True)
class SourceConf:
    """Caractéristiques techniques d'une source de production, servant au calcul théorique (brique 18 §4.1.4).

    Elles viennent du cloud, qui les tient de « Paramètres → Production ». Aucune n'a de valeur devinée :
    une surface de panneau absente annule la contribution du panneau, elle ne lui en invente pas une.
    Seuls les rendements ont un défaut, et c'est celui de la spécification (0,22 et 0,35).
    """

    source_id: int
    type: str
    surface_m2: float | None = None
    rendement_pv: float | None = None
    diametre_rotor_m: float | None = None
    rendement_eolien: float | None = None
    vent_demarrage_m_s: float | None = None
    vent_arret_m_s: float | None = None
    puissance_nominale_w: float | None = None


@dataclass(frozen=True)
class AgentConfig:
    cloud_url: str
    db_url: str
    data_dir: Path
    mqtt: MqttConf = field(default_factory=MqttConf)
    #: Canal temps réel vers le cloud (E173) ; désactivé tant qu'aucun courtier n'est configuré.
    cloud_mqtt: CloudMqttConf = field(default_factory=CloudMqttConf)
    devices: tuple[DeviceConf, ...] = ()
    batteries: tuple[BatteryConf, ...] = ()
    #: Sources de production déclarées du site : le calcul théorique de la brique 18 §4.1.4 s'appuie dessus.
    sources: tuple[SourceConf, ...] = ()
    weather_topic: str | None = None
    local_api_port: int = 8765
    buffer_max: int = 50_000
    buffer_retention_days: int = 30
    measurement_retention_days: int = 90
    backup_keep: int = 7
    interval_scale: float = 1.0
    #: Clé publique Ed25519 (base64url, 32 octets) de publication du paquet : vérifie MANIFEST.json (D254, D258). Distincte de la
    #: clé des ordres : la graine qui signe les ordres ne quitte jamais le cloud.
    package_public_key: str | None = None
    #: Magasin de la clé d'identité (WP8) : ``software`` (Ed25519 en fichier 0600) ou ``tpm2`` (ECDSA P-256 dans le TPM).
    key_store: str = "software"
    #: Accès au TPM (chaîne TCTI de tpm2-tss) : ``device:/dev/tpmrm0`` sur le boîtier.
    tpm_tcti: str = "device:/dev/tpmrm0"
    #: Production chez le client qui l'active : magasin ``tpm2`` sur un module réel exigé, l'agent refuse de démarrer sinon.
    require_tpm: bool = False
    #: Équipement déclaré hors ligne sans mesure depuis ce délai (§4.4.1 « > 5 min », E033), 60 à 3 600 s.
    device_offline_after_s: int = 300
    #: Fichier lu au démarrage : ses champs de sécurité sont relus à chaque vérification d'intégrité (E176).
    config_path: Path | None = None

    @property
    def key_path(self) -> Path:
        return self.data_dir / "keys" / "agent.pem"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "keys" / "local-api.token"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"


_TOPIC = re.compile(r"^[A-Za-z0-9_./:+-]{1,200}$")
#: Adresse MAC telle que l'interface Shelly l'affiche (12 hexadécimaux) ou avec séparateurs. Les deux-points sont
#: déjà refusés par l'expression de l'identifiant ; les tirets et la forme nue passaient.
_ADRESSE_MAC = re.compile(r"^(?:[0-9A-Fa-f]{12}|(?:[0-9A-Fa-f]{2}-){5}[0-9A-Fa-f]{2})$")


def _topic(value: str | None, what: str) -> str | None:
    if value is None:
        return None
    if not _TOPIC.match(value) or "#" in value:
        raise ValueError(f"{what} : topic MQTT invalide ({value!r})")
    return value


def _package_key(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.match(r"^[A-Za-z0-9_-]{43}$", value):
        raise ValueError("package_public_key : clé Ed25519 attendue (32 octets en base64url)")
    return value


def _cloud_mqtt(raw: dict) -> CloudMqttConf:
    """Courtier du cloud, surchargé par l'environnement (``H2S_AGENT_MQTT_HOST``…). TLS obligatoire hors poste de développement."""
    host = os.environ.get("H2S_AGENT_MQTT_HOST", raw.get("host") or "").strip()
    if not host:
        return CloudMqttConf()
    if not re.match(r"^[A-Za-z0-9._-]{1,253}$", host):
        raise ValueError("cloud_mqtt.host : nom d'hôte ou adresse attendu")
    port = int(os.environ.get("H2S_AGENT_MQTT_PORT", raw.get("port", 8883)))
    if not 1 <= port <= 65535:
        raise ValueError("cloud_mqtt.port : port TCP attendu")
    tls_on = str(os.environ.get("H2S_AGENT_MQTT_TLS", raw.get("tls", True))).lower() not in ("false", "0", "no")
    if not tls_on and host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("cloud_mqtt : TLS 1.3 obligatoire hors poste de développement (CDC §3.3)")
    return CloudMqttConf(
        host=host,
        port=port,
        tls=tls_on,
        ca_file=raw.get("ca_file"),
        username=raw.get("username"),
        password_file=os.environ.get("H2S_AGENT_MQTT_PASSWORD_FILE", raw.get("password_file")),
        keepalive_s=min(300, max(10, int(raw.get("keepalive_s", 30)))),
        publish_timeout_s=min(30.0, max(0.5, float(raw.get("publish_timeout_s", 2.0)))),
    )


def _key_store(raw: dict) -> tuple[str, str, bool]:
    store = str(raw.get("key_store", "software")).lower()
    if store not in KEY_STORES:
        raise ValueError(f"key_store : {' ou '.join(KEY_STORES)} attendu ({store!r})")
    tcti = str(raw.get("tpm_tcti", "device:/dev/tpmrm0"))
    if not re.match(r"^(device|swtpm|mssim)(:[A-Za-z0-9_./=,:-]{0,200})?$", tcti):
        raise ValueError("tpm_tcti : chaîne TCTI device:, swtpm: ou mssim: attendue")
    require = bool(raw.get("require_tpm", False))
    if require and store != "tpm2":
        raise ValueError("require_tpm : magasin de clés tpm2 exigé (key_store)")
    if require and tcti.split(":", 1)[0] != "device":
        raise ValueError("require_tpm : un TPM simulateur (swtpm, mssim) n'est pas un module matériel")
    return store, tcti, require


def _reel(valeur, minimum: float, maximum: float) -> float | None:
    """Réel d'une configuration, borné ; ``None`` si absent, illisible ou hors domaine.

    Hors domaine rend ``None`` et non la borne : un diamètre de rotor saisi à 3 000 m est une faute de
    saisie, pas une éolienne géante, et la borner donnerait une production crédible pour une donnée
    fausse. L'absence, elle, se voit — la source ne contribue pas et le total le montre.
    """
    if valeur is None:
        return None
    try:
        v = float(valeur)
    except (TypeError, ValueError):
        return None
    if not (minimum <= v <= maximum):
        return None
    return v


def load(path: str | Path) -> AgentConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    cloud = os.environ.get("H2S_AGENT_CLOUD_URL", raw.get("cloud_url", ""))
    if not re.match(r"^https?://[^\s]+$", cloud):
        raise ValueError("cloud_url : adresse http(s) attendue")
    if cloud.startswith("http://") and not re.match(r"^http://(localhost|127\.0\.0\.1)(:\d+)?(/|$)", cloud):
        raise ValueError("cloud_url : HTTPS obligatoire hors poste de développement (§6 VPN/TLS)")
    db_url = os.environ.get("H2S_AGENT_DB_URL", raw.get("db_url", ""))
    if not db_url:
        raise ValueError("db_url : base PostgreSQL locale attendue")
    devices = []
    for d in raw.get("devices", []):
        kind = d.get("kind")
        if kind not in DEVICE_KINDS:
            raise ValueError(f"équipement {d.get('device_id')} : type {kind!r} inconnu")
        if kind == "shelly_gen2" and not re.match(r"^[A-Za-z0-9_-]{1,64}$", str(d.get("mqtt_id", ""))):
            raise ValueError(f"équipement {d.get('device_id')} : identifiant MQTT Shelly attendu")
        if kind == "shelly_gen2" and _ADRESSE_MAC.match(str(d.get("mqtt_id", ""))):
            # Une adresse MAC passe l'expression du dessus (hexadécimal, parfois des tirets) et donne un
            # abonnement sur un sujet que la prise ne publiera JAMAIS : aucune mesure, aucune erreur de
            # l'agent, et une alerte « équipement hors ligne » cinq minutes plus tard qui ne dit pas pourquoi.
            # Constaté en clientèle le 02/10/2026. Le mqtt_id est le préfixe de sujet que la prise se donne,
            # « <modèle>-<mac en minuscules> » ; on propose la forme la plus probable plutôt que de laisser
            # chercher.
            mac = re.sub(r"[-:]", "", str(d.get("mqtt_id"))).lower()
            raise ValueError(
                f"équipement {d.get('device_id')} : {d.get('mqtt_id')!r} est une adresse MAC, pas un identifiant"
                f" MQTT. Le mqtt_id est le préfixe de sujet affiché par « Rechercher sur le réseau » (colonne"
                f" « Nom »), de la forme shellyplusplugs-{mac} pour une Plug S Gen2."
            )
        options = None
        if kind == "powerlogic_modbus":
            from . import modbus

            options = dict(d.get("modbus") or {})
            try:
                modbus.device_from_config(int(d["device_id"]), options)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"équipement {d.get('device_id')} : configuration Modbus invalide ({exc})") from exc
        elif kind == "linky_tic":
            options = dict(d.get("tic") or {})
            if options.get("mode") not in ("historique", "standard") or not re.match(
                r"^/dev/[A-Za-z0-9_./-]{1,100}$", str(options.get("port", ""))
            ):
                raise ValueError(
                    f"équipement {d.get('device_id')} : TIC : port /dev/… et mode historique ou standard attendus"
                )
        role = str(d.get("role", "consumption")).upper()
        if role not in DEVICE_ROLES:
            raise ValueError(f"équipement {d.get('device_id')} : rôle consumption ou production attendu ({role!r})")
        devices.append(
            DeviceConf(
                int(d["device_id"]),
                kind,
                d.get("mqtt_id"),
                _topic(d.get("topic"), "équipement"),
                options,
                role,
            )
        )
    batteries = tuple(
        BatteryConf(
            int(b["stock_id"]),
            _topic(b["status_topic"], "batterie"),
            _topic(b["command_prefix"], "commandes"),
        )
        for b in raw.get("batteries", [])
    )
    m = raw.get("mqtt", {})
    store, tcti, require_tpm = _key_store(raw)
    cloud_mqtt = _cloud_mqtt(raw.get("cloud_mqtt") or {})
    sources = tuple(
        SourceConf(
            source_id=int(x["source_id"]),
            type=str(x.get("type") or ""),
            surface_m2=_reel(x.get("surface_m2"), 0.1, 100_000.0),
            rendement_pv=_reel(x.get("rendement_pv"), 0.01, 1.0),
            diametre_rotor_m=_reel(x.get("diametre_rotor_m"), 0.1, 300.0),
            rendement_eolien=_reel(x.get("rendement_eolien"), 0.01, 1.0),
            vent_demarrage_m_s=_reel(x.get("vent_demarrage_m_s"), 0.5, 60.0),
            vent_arret_m_s=_reel(x.get("vent_arret_m_s"), 0.5, 60.0),
            puissance_nominale_w=_reel(x.get("puissance_nominale_w"), 1.0, 50_000_000.0),
        )
        for x in raw.get("sources", [])
        if x.get("source_id") is not None
    )

    return AgentConfig(
        cloud_url=cloud.rstrip("/"),
        db_url=db_url,
        data_dir=Path(os.environ.get("H2S_AGENT_DATA_DIR", raw.get("data_dir", "/var/lib/h2s-agent"))),
        mqtt=MqttConf(
            m.get("host", "127.0.0.1"),
            int(m.get("port", 1883)),
            m.get("username"),
            m.get("password_file"),
            bool(m.get("tls", False)),
            m.get("ca_file"),
        ),
        cloud_mqtt=cloud_mqtt,
        devices=tuple(devices),
        batteries=batteries,
        sources=sources,
        weather_topic=_topic(raw.get("weather_topic"), "météo"),
        local_api_port=int(raw.get("local_api_port", 8765)),
        buffer_max=int(raw.get("buffer_max", 50_000)),
        # Recette seulement : cadences accélérées (0.05 = battement de cœur toutes les 15 s). Jamais sous 1 % des cadences.
        interval_scale=min(1.0, max(0.01, float(raw.get("interval_scale", 1.0)))),
        package_public_key=_package_key(raw.get("package_public_key")),
        key_store=store,
        tpm_tcti=tcti,
        require_tpm=require_tpm,
        device_offline_after_s=min(3600, max(60, int(raw.get("device_offline_after_s", 300)))),
        config_path=Path(path),
    )

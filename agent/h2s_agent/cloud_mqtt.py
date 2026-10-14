"""Canal temps réel agent ↔ cloud (lot 5 ; CDC §3.2, §3.3 « MQTT (EMQX) avec QoS 1, TLS 1.3 », §4.1 « latence Edge → Cloud
< 500 ms » ; E173, E027, E039 ; D314).

Le sondage signé de l'API agent (``cloud.py``) reste le socle : enrôlement, règles, modèle, synchronisation du buffer,
battement de cœur. Ce module y ajoute un **canal poussé** :

* ordres de pilotage et commandes d'équipement reçus sur ``h2s/agents/<uuid>/ordres`` dès leur émission, au lieu d'être
  relevés toutes les 60 secondes ;
* mesures instantanées (E027) et relevés bruts (E039) publiés sur ``h2s/agents/<uuid>/telemetrie`` toutes les 5 secondes ;
* comptes rendus d'exécution et résultats de commandes sur ``h2s/agents/<uuid>/comptes-rendus`` ;
* état de l'agent sur ``h2s/agents/<uuid>/etat``.

**Rien de nouveau côté sécurité.** Un message publié par l'agent porte la même signature que ses requêtes HTTP
(``crypto.canonical``, schéma ``H2S-AGENT-V1``), avec la méthode ``MQTT`` et le sujet à la place du chemin ; horodatage,
nonce à usage unique et clé enrôlée sont vérifiés par le cloud avec le même code. Les ordres reçus sont vérifiés par
``DecisionManager`` et ``DeviceCommands``, exactement comme ceux du sondage : le transport ne décide de rien.

**Repli.** Le courtier injoignable n'est pas une panne : ``connected`` retombe à faux, l'agent reprend le sondage HTTP
(``Agent.run_once``) et revient au canal poussé dès la reconnexion. Un ordre reçu deux fois (QoS 1, ou une fois par chaque
chemin) n'est jamais exécuté deux fois : le cache des décisions et la mémoire des commandes l'écartent.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import paho.mqtt.client as paho

from . import crypto, tls
from .clock import Clock
from .config import CloudMqttConf

LOG = logging.getLogger("h2s_agent.cloud_mqtt")

ROOT = "h2s/agents/"
TELEMETRY = "telemetrie"
ORDERS = "ordres"
REPORTS = "comptes-rendus"
STATE = "etat"
VERSION = 1
#: Enveloppe publiée au plus (la même borne qu'au cloud) : un lot de relevés bruts est découpé pour tenir dessous.
MAX_BYTES = 262_144
#: Relevés bruts par message : 500 au plus au cloud ; 200 laissent de la marge sous la borne de taille.
RAW_BATCH = 200


class CloudMqtt:
    """Client du courtier du cloud. Aucun appel ne lève : un canal indisponible rend ``False`` et l'agent sonde."""

    def __init__(
        self,
        conf: CloudMqttConf,
        keys: crypto.KeyStore,
        agent_uuid: str,
        clock: Clock,
        on_decisions: Callable[[list[dict]], None] | None = None,
    ) -> None:
        self._conf = conf
        self._keys = keys
        self._uuid = agent_uuid
        self._clock = clock
        self._on_decisions = on_decisions
        self._connected = threading.Event()
        self._commands: list[dict] = []
        self._lock = threading.Lock()
        self.received = 0
        self.published = 0
        self.refused = 0
        self.reconnections = 0
        #: Délai de la dernière publication acquittée par le courtier (QoS 1, PUBACK) : mesure de la latence du canal.
        self.last_publish_ms: int | None = None
        # Session persistante (``clean_session=False``) et identifiant stable : un ordre publié pendant une coupure courte
        # du réseau du site est remis par le courtier à la reconnexion, sans sondage.
        self._client = paho.Client(
            paho.CallbackAPIVersion.VERSION2,
            client_id=f"h2s-agent-{agent_uuid}",
            clean_session=False,
        )
        # Compte propre à l'agent : son identifiant est son nom d'utilisateur, ce dont les règles d'accès du courtier se
        # servent pour ne lui ouvrir que ses propres sujets (docs/MQTT.md).
        password = Path(conf.password_file).read_text(encoding="utf-8").strip() if conf.password_file else None
        self._client.username_pw_set(conf.username or agent_uuid, password)
        if conf.tls:
            self._client.tls_set_context(tls.client_context(conf.ca_file))
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)

    # ------------------------------------------------------------------ cycle de vie

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def bind_decisions(self, on_decisions: Callable[[list[dict]], None]) -> None:
        """Traitement des ordres poussés, fourni une fois l'agent construit (avant ``start``)."""
        self._on_decisions = on_decisions

    def topic(self, leaf: str) -> str:
        return f"{ROOT}{self._uuid}/{leaf}"

    def start(self) -> None:
        self._client.connect_async(self._conf.host, self._conf.port, keepalive=self._conf.keepalive_s)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.loop_stop()
        try:
            self._client.disconnect()
        except OSError:
            pass

    def wait(self, timeout: float) -> bool:
        return self._connected.wait(timeout)

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            LOG.warning("connexion au courtier du cloud refusée : %s", reason_code)
            return
        client.subscribe(self.topic(ORDERS), qos=1)
        self._connected.set()
        self.reconnections += 1
        LOG.info("canal MQTT du cloud connecté (ordres poussés) ; le sondage passe en filet de sécurité")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        self._connected.clear()
        LOG.warning("canal MQTT du cloud coupé (%s) : repli sur le sondage signé", reason_code)

    # ------------------------------------------------------------------ réception des ordres

    def _on_message(self, client, userdata, msg) -> None:
        self.received += 1
        try:
            self._dispatch(msg.payload)
        except Exception:
            LOG.exception("ordre reçu sur %s non traité", msg.topic)

    def _dispatch(self, raw: bytes) -> None:
        if not raw or len(raw) > MAX_BYTES:
            self.refused += 1
            return
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.refused += 1
            return
        if not isinstance(message, dict) or message.get("v") != VERSION or not isinstance(message.get("payload"), str):
            self.refused += 1
            return
        signed = {
            "payload": message["payload"],
            "signature": message.get("signature"),
            "key_id": message.get("key_id"),
        }
        kind = message.get("type")
        if kind == "decision":
            # Vérification de signature, destinataire et validité : celle du sondage (DecisionManager.receive).
            if self._on_decisions is not None:
                self._on_decisions([signed])
        elif kind == "command":
            with self._lock:
                self._commands.append(signed)
                # File bornée : un courtier qui déverserait des commandes ne remplit pas la mémoire du boîtier.
                del self._commands[:-100]
        else:
            self.refused += 1

    def take_commands(self) -> list[dict]:
        """Commandes reçues depuis le dernier appel (le port ``CommandChannel`` les vérifie puis les exécute)."""
        with self._lock:
            out, self._commands = self._commands, []
        return out

    # ------------------------------------------------------------------ publication

    def _envelope(self, topic: str, payload: dict) -> bytes:
        text = json.dumps(payload, separators=(",", ":"), default=str)
        body = text.encode("utf-8")
        now = int(self._clock.now().timestamp())
        nonce = crypto.new_nonce()
        signature = self._keys.sign(crypto.canonical("MQTT", topic, now, nonce, body).encode("utf-8"))
        envelope = {
            "v": VERSION,
            "agent": self._uuid,
            "ts": now,
            "nonce": nonce,
            "payload": text,
            "sig": signature,
        }
        return json.dumps(envelope, separators=(",", ":")).encode("utf-8")

    def _publish(self, leaf: str, payload: dict) -> bool:
        if not self.connected:
            return False
        topic = self.topic(leaf)
        try:
            data = self._envelope(topic, payload)
        except Exception:  # noqa: BLE001 - TPM injoignable : le sondage signé reste, rien n'est publié non signé
            LOG.warning("message %s non signé (magasin de clés indisponible) : non publié", leaf)
            return False
        if len(data) > MAX_BYTES:
            LOG.warning(
                "message %s de %d octets au-delà de la borne : non publié",
                leaf,
                len(data),
            )
            return False
        started = time.monotonic()
        info = self._client.publish(topic, data, qos=1, retain=False)
        try:
            info.wait_for_publish(timeout=self._conf.publish_timeout_s)
        except (RuntimeError, ValueError):
            return False
        if not info.is_published():
            return False
        self.published += 1
        self.last_publish_ms = int((time.monotonic() - started) * 1000)
        return True

    def publish_live(self, payload: dict) -> bool:
        """Port ``live.LiveTransport`` : instantané du site toutes les 5 secondes (E027)."""
        return self._publish(TELEMETRY, payload)

    def publish_raw(self, site_id: int, readings: list[dict], now: dt.datetime) -> bool:
        """Relevés bruts du site (E039), tels qu'ils ont été mesurés : le cloud les stocke sans les agréger."""
        if not readings:
            return False
        return self._publish(
            TELEMETRY,
            {
                "v": VERSION,
                "type": "raw",
                "site_id": site_id,
                "sent_at": now.isoformat().replace("+00:00", "Z"),
                "readings": readings,
            },
        )

    def publish_records(self, records: list[dict]) -> bool:
        """Comptes rendus d'exécution, alertes, preuves : mêmes enregistrements que ``POST /api/agent/sync/buffer``."""
        if not records:
            return False
        return self._publish(REPORTS, {"v": VERSION, "type": "records", "records": records})

    def publish_command_result(self, command_id: str, body: dict) -> bool:
        return self._publish(
            REPORTS,
            {
                "v": VERSION,
                "type": "command_result",
                "command_id": command_id,
                "status": body.get("status"),
                "result": body.get("result"),
                "error": body.get("error"),
                "duration_ms": body.get("duration_ms"),
            },
        )

    def publish_state(
        self,
        state: str,
        version: str,
        buffer_size: int,
        clock_synced: bool,
        manual_mode: bool,
        tpm: dict | None = None,
        command_key: str | None = None,
    ) -> bool:
        payload = {
            "v": VERSION,
            "type": "state",
            "state": state,
            "agent_version": version,
            "buffer_size": buffer_size,
            "clock_synced": clock_synced,
            "manual_mode": manual_mode,
        }
        if command_key is not None:
            payload["command_public_key"] = command_key
        if tpm is not None:
            payload["tpm"] = {
                k: tpm.get(k)
                for k in (
                    "status",
                    "simulator",
                    "manufacturer",
                    "firmware",
                    "ek_certificate",
                )
            }
        return self._publish(STATE, payload)


class MqttCommandChannel:
    """Port ``commands.CommandChannel`` sur le canal poussé : les commandes arrivent, les résultats repartent."""

    def __init__(self, mqtt: CloudMqtt) -> None:
        self._mqtt = mqtt

    def fetch(self) -> list[dict]:
        return self._mqtt.take_commands()

    def report(self, command_id: str, body: dict) -> None:
        if not self._mqtt.publish_command_result(command_id, body):
            raise ConnectionError("canal MQTT indisponible")


class FallbackCommandChannel:
    """Canal poussé en premier, sondage signé en filet de sécurité (jamais de double exécution).

    ``fetch`` prend les commandes déjà reçues par le canal poussé, **puis interroge le sondage dans tous les cas**. Le
    sondage n'est pas coupé quand le canal poussé tient : il est espacé, par la cadence de la tâche appelante
    (``Task("commands", 20, …, pushed_every_s=300)``). C'est ce que cette classe annonçait déjà, et qu'elle ne faisait
    pas : elle supprimait le sondage au lieu de l'espacer.

    Le supprimer laissait les commandes mourir en silence. Toute la chaîne de publication est en échec ouvert par
    conception — l'orchestrateur qui ne joint pas la passerelle, la passerelle qui répond 204 sans agent, le courtier
    qui n'est pas connecté : aucun de ces trois maillons ne remonte d'erreur. Et cette voie ne marque jamais la
    commande « remise ». Sans sondage, un boîtier connecté au courtier mais que la publication n'atteint pas ne reçoit
    plus rien, sans qu'aucune trace ne le dise, nulle part. Constaté en clientèle : commandes PENDING puis EXPIRED,
    agent en ligne et synchronisé.

    Recevoir la même commande par les deux chemins est sans danger : ``DeviceCommands._verify`` tient l'ensemble des
    identifiants déjà vus, sous verrou, et n'en exécute aucun deux fois.

    ``report`` tente le canal poussé puis retombe sur le sondage : un résultat n'est jamais perdu parce que le courtier
    est tombé entre l'ordre et sa fin d'exécution.
    """

    def __init__(self, mqtt: CloudMqtt, http, pushed: Callable[[], bool] | None = None) -> None:
        self._mqtt = MqttCommandChannel(mqtt)
        self._http = http
        self._pushed = pushed if pushed is not None else (lambda: mqtt.connected)

    def fetch(self) -> list[dict]:
        return self._mqtt.fetch() + list(self._http.fetch())

    def report(self, command_id: str, body: dict) -> None:
        if self._pushed():
            try:
                self._mqtt.report(command_id, body)
                return
            except ConnectionError:
                LOG.warning(
                    "résultat de la commande %s non publié sur le canal poussé : repli sur le sondage",
                    command_id,
                )
        self._http.report(command_id, body)


@dataclass
class RawForwarder:
    """Relevés bruts non encore publiés (E039), lus dans ``local_measurements`` et remis au canal poussé.

    Curseur en base (``agent_kv``) et non en mémoire : un redémarrage ne republie pas une journée de relevés. Un lot est
    borné ; le reste part au tour suivant (5 secondes). Canal coupé : rien n'avance, les relevés restent sur le boîtier —
    ils ne sont pas perdus, le brut n'est pas la source de l'historique (moyennes de 5 minutes par le buffer).
    """

    db: object
    site: Callable[[], int | None]
    now: Callable[[], dt.datetime]
    transport: CloudMqtt | None = None
    batch: int = RAW_BATCH
    #: Relevé plus vieux que ce délai : jamais publié (le cloud l'écarterait), le curseur passe outre.
    max_age: dt.timedelta = dt.timedelta(hours=1)

    KEY = "telemetrie.brut.curseur"

    def cursor(self) -> int:
        with self.db.tx() as cur:
            cur.execute("SELECT value FROM agent_kv WHERE key = %s", (self.KEY,))
            row = cur.fetchone()
        return int(row["value"]) if row and str(row["value"]).isdigit() else 0

    def _save(self, value: int) -> None:
        with self.db.tx() as cur:
            cur.execute(
                "INSERT INTO agent_kv (key, value, updated_at) VALUES (%s, %s, now()) ON CONFLICT (key) DO UPDATE"
                " SET value = EXCLUDED.value, updated_at = now()",
                (self.KEY, str(value)),
            )

    def tick(self) -> int:
        """Publie un lot de relevés bruts ; renvoie le nombre publié (0 si canal coupé, site inconnu ou rien de neuf)."""
        site = self.site()
        if self.transport is None or site is None or not self.transport.connected:
            return 0
        now = self.now()
        since = self.cursor()
        with self.db.tx() as cur:
            cur.execute(
                "SELECT measurement_id, device_id, data_type, value, unit, measurement_timestamp FROM local_measurements"
                " WHERE measurement_id > %s ORDER BY measurement_id LIMIT %s",
                (since, self.batch),
            )
            rows = cur.fetchall()
        if not rows:
            return 0
        floor = now - self.max_age
        readings = [
            {
                "device": r["device_id"],
                "data_type": r["data_type"],
                "value": float(r["value"]),
                "unit": r["unit"],
                "at": r["measurement_timestamp"].isoformat().replace("+00:00", "Z"),
            }
            for r in rows
            if r["measurement_timestamp"] >= floor
        ]
        last = rows[-1]["measurement_id"]
        if not readings:
            # Rien de publiable dans ce lot (relevés trop anciens) : le curseur avance quand même, sinon il n'avancerait plus.
            self._save(last)
            return 0
        if not self.transport.publish_raw(site, readings, now):
            return 0
        self._save(last)
        return len(readings)

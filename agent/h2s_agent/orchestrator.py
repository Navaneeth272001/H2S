"""Orchestration de l'agent (F18.7, CA-18.35 à CA-18.39).

Trois fils : la boucle réseau MQTT (collecte, commandes), l'API locale, et la boucle des tâches ci-dessous. Une tâche
qui échoue passe l'agent en ERROR, lève une alerte, puis l'agent reprend en INIT et retrouve RUNNING ou DEGRADED selon le
cloud (reprise automatique, CA-18.37) ; la collecte n'est jamais interrompue.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import datetime as dt
import json
import logging
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import (
    __version__,
    backup,
    integrity,
    local_policy,
    modbus,
    parsers,
    rules,
    state,
    tic,
    tpm,
)
from .buffer import Buffer
from .clock import Clock, parse_instant, utcnow
from .cloud import CloudClient, CloudRefused, CloudServerError, CloudUnavailable
from .cloud_mqtt import CloudMqtt, FallbackCommandChannel, RawForwarder
from .collector import Collector
from .commands import COMMANDS_STALE_AFTER_S, CommandKey, DeviceCommands, Executor, HttpCommandChannel, commands_stale
from .config import BALANCE_KINDS, AgentConfig
from .decisions import DecisionManager, Identity
from .federated import MODEL_TYPE, Federated
from .live import INTERVAL_S as LIVE_INTERVAL_S
from .live import LivePublisher
from .monitoring import Alerts, Metrics
from .proofs import Proofs
from .storage import Database

LOG = logging.getLogger("h2s_agent")


@dataclass
class Task:
    name: str
    every_s: float
    run: Callable[[], None]
    next_at: float = 0.0
    failures: int = field(default=0)
    #: Cadence appliquée tant que le canal poussé (MQTT) est connecté ; sans elle, la cadence est la même dans les deux cas.
    #: Le sondage des ordres et des commandes n'est pas arrêté mais espacé : il reste le filet qui rattrape une remise perdue.
    pushed_every_s: float | None = None

    def due_in(self, pushed: bool) -> float:
        return self.pushed_every_s if pushed and self.pushed_every_s is not None else self.every_s


def load_identity(db: Database) -> Identity | None:
    with db.tx() as cur:
        cur.execute(
            "SELECT agent_uuid, site_id, decision_key_id, decision_public_key, gradient_public_key FROM agent_identity"
        )
        r = cur.fetchone()
    if r is None:
        return None
    return Identity(
        str(r["agent_uuid"]),
        int(r["site_id"]),
        r["decision_key_id"],
        r["decision_public_key"],
        r["gradient_public_key"],
    )


def save_identity(db: Database, cloud_url: str, registered: dict) -> Identity:
    keys = registered["keys"]
    with db.tx() as cur:
        cur.execute("DELETE FROM agent_identity")
        cur.execute(
            "INSERT INTO agent_identity (agent_uuid, site_id, site_name, cloud_url, decision_key_id, decision_public_key, gradient_public_key)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                registered["agent_uuid"],
                registered["site_id"],
                registered.get("site_name"),
                cloud_url,
                keys["decision_key_id"],
                keys["decision_public_key"],
                keys["gradient_public_key"],
            ),
        )
    return load_identity(db)  # type: ignore[return-value]


class Agent:
    def __init__(
        self,
        config: AgentConfig,
        db: Database,
        cloud: CloudClient,
        publisher,
        clock: Clock,
        require_signed_package: bool = True,
        integrity_root=integrity.PACKAGE,
        site_packages: list | None = None,
        keys=None,
        live_transport=None,
        cloud_mqtt: CloudMqtt | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.cloud = cloud
        self.publisher = publisher
        self.clock = clock
        self.identity = load_identity(db)
        site = str(self.identity.site_id) if self.identity else "?"
        self.buffer = Buffer(db, site, config.buffer_max, config.buffer_retention_days)
        self.alerts = Alerts(
            self.buffer,
            clock.now,
            lambda topic, payload: publisher.publish(self._local_topic(topic), payload, 0),
        )
        self.states = state.StateMachine(db, site, clock.now, self._state_changed)
        self.collector = Collector(
            db,
            self.buffer,
            config,
            clock.now,
            lambda: clock.synced,
            self._site,
            role_of=self._role_of,
        )
        # Clé d'identité qui signe les requêtes : elle signe aussi les preuves locales (E188) et les commandes de batterie (E183).
        self.keys = keys if keys is not None else getattr(cloud, "_keys", None)
        self.decisions = DecisionManager(
            db,
            self.buffer,
            publisher,
            lambda: self.identity,
            self._command_prefix,
            clock.now,
            self.alerts.raise_,
            lambda: clock.synced,
            signer=self.keys,
            devices=lambda: [(f"device:{d.device_id}", d.role) for d in config.devices if d.kind in BALANCE_KINDS],
        )
        #: Canal temps réel vers le cloud (lot 5) ; ``None`` : l'agent sonde comme avant, rien n'est présenté comme du direct.
        self.cloud_mqtt = cloud_mqtt
        live_transport = live_transport if live_transport is not None else cloud_mqtt
        #: Un ordre poussé est exécuté sur son propre fil ; ce verrou empêche qu'il croise le chien de garde de la boucle.
        self._decision_lock = threading.Lock()
        #: Instantané du site toutes les 5 s (E027) : sans transport temps réel (MQTT, lot 5), la tâche n'est pas planifiée.
        self.live = LivePublisher(
            db,
            lambda: self.identity.site_id if self.identity else None,
            lambda: [
                (f"device:{d.device_id}", self._role_of(d.device_id)) for d in config.devices if d.kind in BALANCE_KINDS
            ],
            lambda: [b.stock_id for b in config.batteries],
            clock.now,
            live_transport,
        )
        #: Prévisions embarquées (§2.18) : aucun modèle LightGBM livré à l'agent (Q-05), point d'accroche seulement.
        self.forecaster: local_policy.Forecaster = local_policy.NO_FORECAST
        self.metrics = Metrics(db, self.buffer, str(config.data_dir), clock.now, self._site)
        self.federated = Federated(db, clock.now, self._site)
        # Commandes aux équipements du site (test de connexion, recherche, Wi-Fi) : sondage signé, exécution sur un fil à part.
        self.command_key = CommandKey.load_or_create(config.data_dir / "keys" / "command-x25519.pem")
        http_commands = HttpCommandChannel(cloud)
        # Commandes par le canal poussé tant qu'il tient, par le sondage signé sinon (repli automatique) ; jamais deux fois.
        channel = (
            http_commands if cloud_mqtt is None else FallbackCommandChannel(cloud_mqtt, http_commands, self.pushed)
        )
        self.device_commands = DeviceCommands(
            channel,
            lambda: self.identity,
            self.command_key,
            Executor(),
            clock.now,
            self.alerts.raise_,
        )
        #: Relevés bruts publiés au fil de l'eau (E039) ; le cloud les stocke tels quels, avec une rétention bornée.
        self.raw = RawForwarder(
            db,
            lambda: self.identity.site_id if self.identity else None,
            clock.now,
            cloud_mqtt,
        )
        #: Ordres poussés : un fil à part, pour ne pas bloquer la boucle réseau du courtier pendant une exécution.
        self._orders = (
            None
            if cloud_mqtt is None
            else concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="h2s-ordres")
        )
        if cloud_mqtt is not None:
            cloud_mqtt.bind_decisions(self.push_received)
        self.proofs = (
            Proofs(db, self.buffer, self.keys, lambda: self.identity, clock.now) if hasattr(self.keys, "sign") else None
        )
        self.require_signed_package = require_signed_package
        self.execution_blocked = False
        self.integrity = integrity.Verdict(False, "non vérifiée")
        self._integrity_root = integrity_root
        self._site_packages = integrity.site_packages() if site_packages is None else site_packages
        self._running_config: dict | None = None
        self._stop = threading.Event()
        self._started = time.monotonic()
        self._cursor: str | None = self._kv("decisions.cursor")
        self._server_error = False
        if self.identity:
            cloud.identify(self.identity.agent_uuid)
        # Collecte directe (E172) : centrales PowerLogic en Modbus, lues par la boucle des tâches ; compteurs Linky en TIC, un fil par port.
        self.modbus = [
            modbus.device_from_config(d.device_id, d.options or {})
            for d in config.devices
            if d.kind == "powerlogic_modbus"
        ]
        self.modbus_failures: dict[int, str] = {}
        self.tic_readers = [
            tic.TicReader(
                d.device_id,
                str((d.options or {})["port"]),
                str((d.options or {})["mode"]),
                self.collector.store,
                clock.now,
                lambda: clock.synced,
                (d.options or {}).get("meter_id"),
            )
            for d in config.devices
            if d.kind == "linky_tic"
        ]
        self.tasks = [
            Task("decisions", 10, self.run_decisions),
            Task("modbus", 60, self.poll_modbus),
            Task("proofs", 300, self.build_proofs),
            # Canal poussé connecté : le sondage des ORDRES est espacé (filet de sécurité) — c'est un appel
            # réseau, et l'ordre poussé arrive par le canal.
            Task("poll", 60, self.poll_decisions, pushed_every_s=600),
            # Les COMMANDES d'équipement, elles, gardent leur cadence dans les DEUX modes, et ce n'est pas un
            # oubli de symétrie (D734). Une commande poussée n'est pas traitée à son arrivée : elle est mise
            # en file dans CloudMqtt, et c'est « poll_commands » — ce sondage-ci — qui la relève. L'espacer
            # à 300 s revenait donc à retarder la commande poussée de 300 s, alors que le cloud l'expire en
            # 120 s (DeviceCommandService.TTL). Tester la connexion d'un équipement ne pouvait PAS aboutir
            # tant que le canal poussé tenait : le filet se déclenchait toujours après la mort de la commande.
            # En mode poussé, relever la file ne coûte rien — c'est un échange de liste en mémoire, pas un
            # appel réseau (FallbackCommandChannel).
            Task("commands", 20, self.poll_commands),
            Task("aggregate", 60, self.aggregate),
            Task("metrics", 60, self.collect_metrics),
            Task("heartbeat", 300, self.heartbeat),
            # 60 s ET NON 300 s (D765). Cette cadence était le premier poste du retard observé à l'écran :
            # une fenêtre de 5 minutes agrégée à 21 h 37 pouvait attendre 21 h 42 pour partir. Mesuré sur le
            # boîtier du site d'essai le 03/10/2026 — 13 minutes entre la mesure et son apparition sur la
            # courbe, décomposées en 5 (fenêtre) + 2 (grâce Shelly) + 1 (cadence d'agrégation) + 5 (ici).
            #
            # Ce que ça coûte : rien de sensible. « sync » vide un buffer ; s'il est vide, l'appel ne part
            # même pas (voir plus bas, « due » rend une liste vide). Sur un site d'habitation, une fenêtre
            # de 5 minutes produit une poignée de lignes. Le gain est de 4 minutes sur les 13.
            #
            # Ce que ça ne change PAS : la fenêtre de 5 minutes et la grâce de 2 minutes restent. La
            # première est le pas de la donnée ; la seconde corrige un comportement réel des prises Shelly,
            # qui datent leur relevé de la minute écoulée. Les retirer ferait agréger des fenêtres
            # incomplètes — une mesure fausse vaut moins qu'un retard honnête.
            Task("sync", 60, self.sync),
            Task("batch", 300, self.poll_batch),
            Task("rules", 3600, self.refresh_rules),
            Task("housekeeping", 3600, self.housekeeping),
            Task("integrity", 3600, self.verify_integrity),
            Task("pubkey", 3600, self.announce_key),
            Task("model", 86_400, self.refresh_model),
            Task("backup", 86_400, self.backup),
            Task("gradients", 604_800, self.contribute),
        ]
        if live_transport is not None:
            self.tasks.insert(0, Task("live", LIVE_INTERVAL_S, self.live.tick))
        if cloud_mqtt is not None:
            # Relevés bruts (E039) et comptes rendus urgents au fil de l'eau : uniquement tant que le canal poussé tient.
            self.tasks.insert(0, Task("brut", LIVE_INTERVAL_S, self.forward_raw))
            self.tasks.insert(0, Task("rendus", LIVE_INTERVAL_S, self.push_reports))
        for task in self.tasks:
            task.every_s = max(1.0, task.every_s * config.interval_scale)
            if task.pushed_every_s is not None:
                task.pushed_every_s = max(1.0, task.pushed_every_s * config.interval_scale)

    # ------------------------------------------------------------------ outils

    def _site(self) -> str:
        return str(self.identity.site_id) if self.identity else "?"

    def _local_topic(self, topic: str) -> str:
        """Topics de l'agent sur le broker local, préfixés par le site comme ceux des équipements (D255, ACL de deploy/)."""
        return f"h2s/site{self._site()}/{topic}"

    def _kv(self, key: str) -> str | None:
        with self.db.tx() as cur:
            cur.execute("SELECT value FROM agent_kv WHERE key = %s", (key,))
            r = cur.fetchone()
        return r["value"] if r else None

    def _set_kv(self, key: str, value: str) -> None:
        with self.db.tx() as cur:
            cur.execute(
                "INSERT INTO agent_kv (key, value, updated_at) VALUES (%s, %s, now()) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,"
                " updated_at = now()",
                (key, value),
            )

    def _role_of(self, device_id: int) -> str:
        """Rôle énergétique d'un équipement (E181) : celui enregistré au cloud (règles reçues), sinon celui d'agent.json."""
        policy = self.decisions.policy() if hasattr(self, "decisions") else None
        role = ((policy or {}).get("roles") or {}).get(f"device:{device_id}")
        if role is not None:
            return role
        return next(
            (d.role for d in self.config.devices if d.device_id == device_id),
            "CONSUMPTION",
        )

    def _command_prefix(self, stock_id: int) -> str | None:
        return next(
            (b.command_prefix for b in self.config.batteries if b.stock_id == stock_id),
            None,
        )

    def _state_changed(self, target: str, previous: str | None, reason: str) -> None:
        now = self.clock.now()
        self.buffer.push(
            "STATE",
            {"state": target, "previous_state": previous, "reason": reason[:200]},
            now,
            now,
        )
        with contextlib.suppress(Exception):
            self.publisher.publish(
                self._local_topic("agent/status"),
                json.dumps({"state": target, "reason": reason[:200]}),
                0,
            )
        if self.pushed():
            try:
                # Changement d'état annoncé tout de suite : l'écran des agents ne l'apprend plus au battement de cœur suivant.
                self.cloud_mqtt.publish_state(
                    target,
                    __version__,
                    self.buffer.size(),
                    self.clock.synced,
                    self.decisions.manual_mode,
                )
            except Exception:  # noqa: BLE001 - le canal n'est jamais une condition d'une transition d'état
                LOG.warning("état %s non publié sur le canal temps réel", target)
        if target == state.DEGRADED:
            self.alerts.raise_("CLOUD_UNREACHABLE", "WARNING", reason)
        if target == state.RUNNING and previous == state.DEGRADED:
            self.buffer.release(now)

    def _cloud(self, fn: Callable[[], dict]) -> dict | None:
        """Appel au cloud compté pour la détection de perte réseau ; None si le cloud est injoignable ou en erreur 500."""
        self._server_error = False
        try:
            out = fn()
        except CloudServerError as exc:
            # Le cloud répond : ce n'est pas une perte de réseau, pas de mode dégradé ni de rejeu du cache (relecture B18-1).
            self._server_error = True
            self.states.cloud_success()
            LOG.warning("erreur du cloud : %s", exc)
            return None
        except CloudUnavailable as exc:
            self.states.cloud_failure(str(exc))
            return None
        except CloudRefused as exc:
            self.states.cloud_success()
            if exc.status == 401:
                self.alerts.raise_(
                    "AGENT_ERROR",
                    "CRITICAL",
                    "le cloud ne reconnaît plus l'agent (révoqué, ou horloge décalée de plus de 5 minutes)",
                )
            raise
        self.states.cloud_success()
        return out

    def stock_ids(self) -> list[int]:
        with self.db.tx() as cur:
            cur.execute(
                "SELECT DISTINCT stock_id FROM local_security_rules WHERE stock_id IS NOT NULL ORDER BY stock_id"
            )
            known = [r["stock_id"] for r in cur.fetchall()]
        return known or [b.stock_id for b in self.config.batteries]

    # ------------------------------------------------------------------ tâches

    def run_decisions(self) -> None:
        if self.identity is None:
            return
        # Même verrou que les ordres poussés : le chien de garde et un ordre qui arrive ne pilotent jamais la même batterie ensemble.
        with self._decision_lock:
            if self.execution_blocked:
                # Aucun ordre exécuté, mais le chien de garde ne s'arrête jamais : il ne fait que mettre en veille (contrôle final B18).
                for stock in self.stock_ids():
                    self.decisions.watchdog(stock, self.clock.now())
                return
            self.decisions.tick(self.stock_ids(), self.states.online)

    # ------------------------------------------------------------------ canal poussé (lot 5, E173)

    def pushed(self) -> bool:
        """Canal temps réel disponible : les ordres arrivent poussés, le sondage n'est plus que le filet de sécurité."""
        return self.cloud_mqtt is not None and self.cloud_mqtt.connected

    def push_received(self, signed: list[dict]) -> None:
        """Ordre reçu sur le canal poussé : vérifié et exécuté tout de suite, sur un fil à part (CA-18.10, < 200 ms).

        Le fil du courtier ne doit pas exécuter : une commande de batterie qui traîne couperait son battement de cœur.
        Un ordre déjà en cache (remise QoS 1 en double, ou déjà relevé par le sondage) ne produit aucune exécution.
        """
        if self._orders is None:
            self.receive_orders(signed)
            return
        try:
            self._orders.submit(self.receive_orders, signed)
        except RuntimeError:  # l'agent s'arrête : l'ordre sera relevé au prochain démarrage par le sondage
            LOG.warning("ordre poussé reçu pendant l'arrêt : non exécuté, il reste en attente au cloud")

    def receive_orders(self, signed: list[dict]) -> list[str]:
        """Vérifie, met en cache et exécute les ordres nouveaux ; renvoie les batteries touchées."""
        try:
            with self._decision_lock:
                fresh = self.decisions.receive(signed)
                if not fresh or self.execution_blocked:
                    return []
                return self.decisions.tick(sorted({o.stock_id for o in fresh}), True)
        except Exception as exc:
            LOG.exception("ordre poussé non exécuté")
            self.recover(f"ordre poussé : {type(exc).__name__}")
            return []

    def forward_raw(self) -> int:
        """Relevés bruts publiés au fil de l'eau (E039) ; sans canal poussé, rien n'est publié et rien n'est perdu."""
        return self.raw.tick()

    def push_reports(self) -> int:
        """Comptes rendus, alertes et changements d'état publiés dès qu'ils existent, sans attendre la synchronisation de 300 s.

        Le courtier accuse chaque message (QoS 1) : c'est cet accusé qui libère l'enregistrement du buffer. Les mesures,
        métriques, preuves locales et contributions gardent le chemin HTTP et son accusé applicatif, enregistrement par
        enregistrement (elles sont probantes ou comptées par quota).
        """
        if not self.pushed() or self.identity is None:
            return 0
        now = self.clock.now()
        items = self.buffer.due(now, 50, ("DECISION_REPORT", "ALERT", "STATE"))
        if not items:
            return 0
        records = [
            {
                "id": i.message_id,
                "kind": i.kind,
                "at": i.at.isoformat().replace("+00:00", "Z"),
                "payload": i.payload,
            }
            for i in items
        ]
        if not self.cloud_mqtt.publish_records(records):
            # Canal tombé entre-temps : rien n'est marqué en échec, la synchronisation HTTP les reprendra telles quelles.
            return 0
        self.buffer.acknowledged([i.buffer_id for i in items])
        return len(records)

    def poll_decisions(self) -> None:
        if self.identity is None:
            return
        body = self._cloud(self.cloud.decisions_current)
        if body is not None:
            fresh = self.decisions.receive(body.get("decisions", []))
            if fresh and not self.execution_blocked:
                self.decisions.tick(sorted({o.stock_id for o in fresh}), True)

    def poll_commands(self) -> int:
        """Commandes d'équipement en attente (E010, E011, E020) : vérifiées puis exécutées sur le fil des commandes."""
        if self.identity is None:
            return 0
        out = self._cloud(lambda: {"started": self.device_commands.poll()})
        return 0 if out is None else out["started"]

    def poll_batch(self) -> None:
        if self.identity is None:
            return
        body = self._cloud(lambda: self.cloud.decisions_batch(self._cursor))
        if body is not None:
            self.decisions.receive(body.get("decisions", []))
            cursor = body.get("cursor")
            if isinstance(cursor, str) and cursor.isdigit() and cursor != self._cursor:
                self._cursor = cursor
                self._set_kv("decisions.cursor", cursor)

    def tpm_probe(self) -> dict:
        """Sonde TPM (CA-18.27) : celle du magasin TPM, sinon du TCTI configuré. Un simulateur reste marqué simulateur."""
        keys = getattr(self.cloud, "_keys", None)
        if keys is not None and hasattr(keys, "probe"):
            return keys.probe()
        return tpm.probe(self.config.tpm_tcti)

    def tpm_view(self) -> dict:
        """``/api/agent/tpm`` : magasin de la clé, algorithme, sonde, intégrité du paquet."""
        keys = getattr(self.cloud, "_keys", None)
        return {
            "key_store": getattr(keys, "kind", None),
            "key_algorithm": getattr(keys, "algorithm", None),
            "tpm": self.tpm_probe(),
            "integrity": {"ok": self.integrity.ok, "reason": self.integrity.reason},
        }

    def heartbeat(self) -> None:
        if self.identity is None:
            return
        probe = self.tpm_probe()
        self._cloud(
            lambda: self.cloud.heartbeat(
                self.states.state,
                __version__,
                self.buffer.size(),
                self.decisions.manual_mode,
                probe,
                self.command_key.public_key(),
            )
        )

    def sync(self, limit: int = 500) -> int:
        """Vide le buffer par lots tant que le cloud accuse réception ; les contributions passent par leur route."""
        if self.identity is None:
            return 0
        sent = 0
        now = self.clock.now()
        for item in self.buffer.due(now, 20, ("GRADIENT",)):
            try:
                out = self._cloud(lambda item=item: self.cloud.gradients(item.payload))
            except CloudRefused as exc:
                self.buffer.failed([item.buffer_id], str(exc), now)
                continue
            if out is None:
                self.buffer.failed([item.buffer_id], "cloud injoignable", now)
                return sent
            self.buffer.acknowledged([item.buffer_id])
            sent += 1
        isolated_failures = 0
        while True:
            items = self.buffer.due(
                self.clock.now(),
                limit,
                (
                    "MEASUREMENT",
                    "METRIC",
                    "STATE",
                    "DECISION_REPORT",
                    "ALERT",
                    "LOCAL_PROOF",
                ),
            )
            if not items:
                return sent
            records = [
                {
                    "id": i.message_id,
                    "kind": i.kind,
                    "at": i.at.isoformat().replace("+00:00", "Z"),
                    "payload": i.payload,
                }
                for i in items
            ]
            try:
                out = self._cloud(lambda records=records: self.cloud.sync(records))
            except CloudRefused as exc:
                self.buffer.failed([i.buffer_id for i in items], str(exc), self.clock.now())
                return sent
            if out is None and self._server_error and limit > 1:
                # Erreur 500 sur le lot : lots plus petits pour isoler l'enregistrement fautif, les autres partent (relecture B18-1).
                limit = max(1, limit // 10)
                continue
            if out is None:
                self.buffer.failed(
                    [i.buffer_id for i in items],
                    "erreur du cloud" if self._server_error else "cloud injoignable",
                    self.clock.now(),
                )
                isolated_failures += 1
                if self._server_error and isolated_failures < 5:
                    continue
                return sent
            # Différé (quota du cloud) : ni acquitté ni perdu, renvoyé avec la reprise espacée du buffer.
            answered = {r.get("id") for r in out.get("results", []) if r.get("outcome") != "DEFERRED"}
            done = [i.buffer_id for i in items if i.message_id in answered]
            self.buffer.acknowledged(done)
            rejected = [r for r in out.get("results", []) if r.get("outcome") == "REJECTED"]
            if rejected:
                LOG.warning(
                    "%d enregistrement(s) refusé(s) par le cloud, ex. : %s",
                    len(rejected),
                    rejected[0].get("reason"),
                )
                # Un enregistrement refusé est ACQUITTÉ : il quitte le tampon et n'y reviendra pas. Jusqu'ici, seule
                # cette ligne de journal en gardait la trace — sur un boîtier, c'est-à-dire nulle part où quelqu'un
                # regarde. Le cloud, lui, le note dans son journal des messages, mais aucun opérateur n'y lit une
                # alerte. Une mesure stockée localement puis refusée là-haut ne déclenchera jamais « équipement hors
                # ligne » (qui lit la base locale) : sans cette alerte, elle disparaît sans témoin. Une par heure au
                # plus (dédoublonnage par code) — y compris si c'est l'alerte elle-même qui est refusée.
                self.alerts.raise_(
                    "SYNC_REJECTED",
                    "WARNING",
                    f"{len(rejected)} enregistrement(s) refusé(s) par le cloud, ex. : {rejected[0].get('reason')}",
                )
            missing = [i.buffer_id for i in items if i.message_id not in answered]
            if missing:
                self.buffer.failed(missing, "sans accusé du cloud", self.clock.now())
            sent += len(done)
            if len(items) < limit:
                return sent

    def poll_modbus(self) -> int:
        """Lecture des centrales Modbus (60 s) ; une centrale muette n'arrête pas les autres, le silence est signalé à 10 minutes."""
        written = 0
        for device in self.modbus:
            now = self.clock.now()
            try:
                written += self.collector.store(device.poll(now, self.clock.synced), now)
                self.modbus_failures.pop(device.device_id, None)
            except (OSError, parsers.ParseError) as exc:
                if self.modbus_failures.get(device.device_id) != str(exc):
                    LOG.warning("centrale Modbus %s illisible : %s", device.device_id, exc)
                self.modbus_failures[device.device_id] = str(exc)
        return written

    def build_proofs(self) -> int:
        """Preuves locales des périodes closes (E188) : établies en réseau comme en coupure, remises par le buffer."""
        if self.proofs is None:
            return 0
        return self.proofs.build()

    def collect_metrics(self) -> None:
        with self.db.tx() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM local_decisions WHERE created_at > %s",
                (self.clock.now() - dt.timedelta(hours=1),),
            )
            decisions = int(cur.fetchone()["n"])
        size = self.buffer.size()
        errors, self.alerts.errors = self.alerts.errors, 0
        values = self.metrics.collect(self.cloud.last_latency_ms, size, decisions, errors)
        if size > 0.8 * self.config.buffer_max:
            self.alerts.raise_("BUFFER_NEAR_FULL", "WARNING", f"{size} enregistrements en attente")
        if values.get("disk_pct") is not None and values["disk_pct"] > 90:
            self.alerts.raise_("DISK_HIGH", "CRITICAL", f"{values['disk_pct']} %")
        if values.get("cpu_pct") is not None and values["cpu_pct"] > 90:
            self.alerts.raise_("CPU_HIGH", "WARNING", f"{values['cpu_pct']} %")
        self.check_commands_stale()

    def check_commands_stale(self) -> bool:
        """L'agent répond au cloud mais ne relève plus ses commandes : l'alerte que le battement de cœur ne donne pas.

        Ne juge qu'un agent que le cloud VOIT (RUNNING ou SYNC) : en mode dégradé, c'est CLOUD_UNREACHABLE qui parle,
        et doubler l'alerte n'apprendrait rien. Le délai de grâce au démarrage suit ``interval_scale`` comme les autres.
        """
        if self.identity is None or self.states.state not in (state.RUNNING, state.SYNC):
            return False
        since_start = (time.monotonic() - self._started) / self.config.interval_scale
        if not commands_stale(self.device_commands.last_poll_at, self.clock.now(), since_start):
            return False
        last = self.device_commands.last_poll_at
        self.alerts.raise_(
            "COMMANDS_STALE",
            "CRITICAL",
            "aucun relevé de commandes réussi depuis "
            + (last.isoformat().replace("+00:00", "Z") if last else "le démarrage")
            + f" (seuil {COMMANDS_STALE_AFTER_S} s) : les boutons d'équipement du tableau de bord n'aboutissent pas",
            details={"last_poll_at": last.isoformat().replace("+00:00", "Z") if last else None},
        )
        return True

    def aggregate(self) -> None:
        self.collector.aggregate()
        self.silent_devices()

    def silent_devices(self) -> list[str]:
        """Équipement hors ligne (§4.4.1, E033) : aucune mesure brute depuis ``device_offline_after_s`` (5 minutes) ; une alerte CRITICAL
        par équipement (clé ``device``), une par heure au plus pour chacun. Le cloud en fait l'alerte « Équipement hors ligne » du site."""
        now = self.clock.now()
        keys = [f"device:{d.device_id}" for d in self.config.devices] + [
            f"stock:{b.stock_id}" for b in self.config.batteries
        ]
        if not keys:
            return []
        limit = dt.timedelta(seconds=self.config.device_offline_after_s)
        # Même délai de grâce après le démarrage : un équipement n'a pas encore eu le temps de publier.
        if time.monotonic() - self._started <= self.config.device_offline_after_s * self.config.interval_scale:
            return []
        with self.db.tx() as cur:
            cur.execute(
                "SELECT device_id, max(measurement_timestamp) AS last FROM local_measurements WHERE device_id = ANY(%s) GROUP BY device_id",
                (keys,),
            )
            last = {r["device_id"]: r["last"] for r in cur.fetchall()}
        silent = [k for k in keys if last.get(k) is None or now - last[k] > limit]
        minutes = max(1, round(limit.total_seconds() / 60))
        for key in silent:
            seen = last.get(key)
            detail = {
                "device": key,
                "last_seen": seen.isoformat().replace("+00:00", "Z") if seen else None,
            }
            self.alerts.raise_(
                "DEVICE_SILENT",
                "CRITICAL",
                f"{key} : Aucune mesure reçue depuis {minutes} minutes",
                key=key,
                details=detail,
            )
        return silent

    def refresh_rules(self) -> None:
        if self.identity is None:
            return
        body = self._cloud(self.cloud.rules)
        if body is None:
            return
        now = self.clock.now()
        with self.db.tx() as cur:
            for s in body.get("stocks", []):
                t = rules.Thresholds.bounded(
                    float(s["soc_min"]),
                    float(s["soc_max"]),
                    float(s["p_charge_max_w"]),
                    float(s["p_discharge_max_w"]),
                    int(s["min_transition_interval_s"]),
                )
                for code, name, value, unit in (
                    ("SEC1", "Pas de conflit CHARGE/DISCHARGE", None, None),
                    ("SEC2", "SoC minimum", t.soc_min, "%"),
                    ("SEC3", "SoC maximum", t.soc_max, "%"),
                    ("SEC4", "Puissance charge max", t.p_charge_max_w, "W"),
                    ("SEC5", "Puissance décharge max", t.p_discharge_max_w, "W"),
                    (
                        "SEC6",
                        "Temps min entre transitions",
                        t.min_transition_interval_s,
                        "s",
                    ),
                ):
                    cur.execute(
                        "INSERT INTO local_security_rules (site_id, stock_id, rule_code, rule_name, threshold_value, unit, is_active, synced_from_cloud,"
                        " last_sync_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, TRUE, TRUE, %s, %s) ON CONFLICT (stock_id, rule_code) DO UPDATE"
                        " SET threshold_value = EXCLUDED.threshold_value, synced_from_cloud = TRUE, last_sync_at = EXCLUDED.last_sync_at,"
                        " updated_at = EXCLUDED.updated_at",
                        (
                            self._site(),
                            s["stock_id"],
                            code,
                            name,
                            value,
                            unit,
                            now,
                            now,
                        ),
                    )
                cur.execute(
                    "INSERT INTO local_battery_profiles (site_id, stock_id, profile_name, capacity_wh, soc_min, soc_max, p_charge_max_w, p_discharge_max_w,"
                    " synced_from_cloud, last_sync_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s, %s) ON CONFLICT (stock_id) DO UPDATE"
                    " SET capacity_wh = EXCLUDED.capacity_wh, soc_min = EXCLUDED.soc_min, soc_max = EXCLUDED.soc_max,"
                    " p_charge_max_w = EXCLUDED.p_charge_max_w, p_discharge_max_w = EXCLUDED.p_discharge_max_w, synced_from_cloud = TRUE,"
                    " last_sync_at = EXCLUDED.last_sync_at, updated_at = EXCLUDED.updated_at",
                    (
                        self._site(),
                        s["stock_id"],
                        (s.get("name") or f"batterie {s['stock_id']}")[:100],
                        s.get("capacity_wh"),
                        t.soc_min,
                        t.soc_max,
                        int(t.p_charge_max_w),
                        int(t.p_discharge_max_w),
                        now,
                        now,
                    ),
                )
        # Seuils §5.1 du site, rôles des équipements et prix day-ahead : ce qu'il faut pour décider seul hors connexion (R5-20).
        self.decisions.store_policy(body.get("decision"), body.get("prices"), now)

    def refresh_model(self) -> None:
        if self.identity is None:
            return
        body = self._cloud(lambda: self.cloud.model(MODEL_TYPE))
        if body is None:
            return
        try:
            if self.federated.install(body, self.identity.decision_public_key):
                LOG.info("modèle fédéré installé")
        except ValueError as exc:
            self.alerts.raise_("MODEL_REJECTED", "CRITICAL", str(exc))

    def contribute(self) -> None:
        if self.identity is None:
            return
        body = self.federated.contribution(self.identity.gradient_public_key)
        if body is not None:
            now = self.clock.now()
            self.buffer.push("GRADIENT", body, now, now)
            self.sync()

    def housekeeping(self) -> None:
        now = self.clock.now()
        self.buffer.evict(now)
        self.decisions.purge_cache(now)
        self.collector.purge(now, self.config.measurement_retention_days)
        self.metrics.purge(now)
        if not self.clock.synced:
            self.alerts.raise_(
                "CLOCK_UNSYNCED",
                "WARNING",
                "aucune réponse horodatée du cloud depuis le démarrage",
            )

    def backup(self) -> None:
        try:
            backup.run(
                self.config.db_url,
                self.config.backup_dir,
                self.clock.now(),
                self.config.backup_keep,
            )
        except (RuntimeError, OSError) as exc:
            self.alerts.raise_("BACKUP_FAILED", "WARNING", str(exc))

    # ------------------------------------------------------------------ cycle de vie

    def verify_integrity(self, at_boot: bool = False) -> integrity.Verdict:
        """Paquet, dépendances et champs de sécurité d'agent.json (E176) : au démarrage puis toutes les heures.

        Un écart bloque l'exécution jusqu'au redémarrage du service sur un paquet conforme (jamais de déblocage à chaud) et,
        en cours d'exécution, met en veille les batteries sous un ordre : le code qui les pilote n'est plus digne de foi.
        """
        try:
            current = integrity.config_snapshot(self.config.config_path)
        except (OSError, ValueError) as exc:
            current = {"illisible": str(exc)[:100]}
        if at_boot:
            self._running_config = current
        # Clé de publication du paquet, distincte de la clé des ordres (audit B18-3) : lue dans agent.json, en lecture seule pour le service.
        verdict = integrity.verify(
            self.config.package_public_key,
            self._integrity_root,
            self._site_packages,
            current,
            self._running_config,
        )
        self.integrity = verdict
        if verdict.ok:
            return verdict
        was_blocked = self.execution_blocked
        self.execution_blocked = self.execution_blocked or self.require_signed_package
        self.alerts.raise_(
            "INTEGRITY_FAILED",
            "CRITICAL" if self.require_signed_package else "WARNING",
            verdict.reason,
        )
        if self.require_signed_package and not was_blocked and not at_boot:
            for stock_id in [s for s in self.stock_ids() if self._command_prefix(s)]:
                last = self.decisions.last_execution(stock_id)
                if last is not None and last["action"] != rules.STANDBY:
                    self.decisions.safe_standby(stock_id, "intégrité du code non vérifiée")
        return verdict

    def announce_key(self) -> None:
        """Clé publique retenue sur ``h2s/site<N>/agent/pubkey`` : la passerelle de la batterie authentifie les commandes (E183)."""
        if self.identity is not None:
            self.decisions.publish_public_key(self._local_topic("agent/pubkey"))

    def boot(self) -> None:
        self.states.start("démarrage de l'agent")
        if self.cloud_mqtt is not None and self.identity is not None:
            try:
                self.cloud_mqtt.start()
            except Exception:  # noqa: BLE001 - courtier injoignable au démarrage : l'agent sonde, le client réessaie seul
                LOG.warning("canal temps réel non ouvert au démarrage : sondage signé jusqu'au rétablissement")
        self.verify_integrity(at_boot=True)
        try:
            self.announce_key()
        except Exception:  # noqa: BLE001 - broker local absent au démarrage : annoncée à la tâche horaire suivante
            LOG.warning("clé publique non annoncée au démarrage")
        if self.identity:
            try:
                self.refresh_rules()
                self.heartbeat()
            except CloudRefused:
                pass
            interrupted = json.loads(self._kv("resume.stocks") or "[]")
            if interrupted and not self.execution_blocked:
                self.decisions.resume([int(s) for s in interrupted], self.states.online)
            self._set_kv("resume.stocks", "[]")

    def run_once(self, monotonic: float | None = None) -> None:
        """Exécute les tâches échues ; une exception fait passer en ERROR puis reprendre (CA-18.37)."""
        t = time.monotonic() if monotonic is None else monotonic
        pushed = self.pushed()
        for task in self.tasks:
            if t < task.next_at:
                continue
            task.next_at = t + task.due_in(pushed)
            try:
                task.run()
                task.failures = 0
            except CloudRefused as exc:
                LOG.warning("tâche %s refusée par le cloud : %s", task.name, exc)
            except Exception as exc:
                task.failures += 1
                LOG.exception("tâche %s en échec", task.name)
                self.recover(f"tâche {task.name} : {type(exc).__name__}")

    def recover(self, reason: str) -> None:
        try:
            self.alerts.raise_("AGENT_ERROR", "CRITICAL", reason)
        except Exception:
            LOG.exception("alerte de reprise impossible")
        try:
            self.states.to(state.ERROR, reason)
            self.states.to(state.INIT, "reprise automatique")
            self.states.cloud_success() if self._probe() else self.states.to(state.DEGRADED, "reprise sans cloud")
        except Exception:
            LOG.exception("transition de reprise impossible")

    def _probe(self) -> bool:
        if self.identity is None:
            return False
        try:
            self.cloud.heartbeat(self.states.state, __version__, self.buffer.size())
            return True
        except (CloudUnavailable, CloudRefused):
            return False

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self._stop.set())
        signal.signal(signal.SIGINT, lambda *_: self._stop.set())
        for reader in self.tic_readers:
            reader.start()
        try:
            self.boot()
            while not self._stop.is_set():
                self.run_once()
                self._stop.wait(1.0)
        finally:
            # Arrêt demandé ou erreur fatale : les batteries repassent en veille dans tous les cas (contrôle final B18).
            for reader in self.tic_readers:
                reader.stop()
            self.device_commands.stop()
            self.shutdown()
            if self._orders is not None:
                self._orders.shutdown(wait=False, cancel_futures=True)
            if self.cloud_mqtt is not None:
                self.cloud_mqtt.stop()

    def shutdown(self) -> None:
        with contextlib.suppress(Exception):  # transition illégale ou base indisponible
            self.states.to(state.SHUTDOWN, "arrêt demandé")
        # Plus personne ne surveille les règles de sécurité : chaque batterie repasse en veille avant la sortie (D247). Seules les
        # batteries arrêtées par cette sortie reprendront leur ordre au démarrage — pas celles que le chien de garde a mises en veille.
        try:
            # Seules les batteries raccordées à cet agent (préfixe de commande configuré) : les autres stockages du site ne
            # reçoivent rien de lui, une veille « en échec » à chaque arrêt ne serait qu'une fausse alerte au cloud (D290).
            stocks = [s for s in self.stock_ids() if self._command_prefix(s)]
            running = [
                s
                for s in stocks
                if (last := self.decisions.last_execution(s)) is not None and last["action"] != rules.STANDBY
            ]
            self._set_kv("resume.stocks", json.dumps(running))
        except Exception:  # noqa: BLE001 - base locale indisponible : veille de toutes les batteries configurées
            LOG.warning("base locale indisponible à l'arrêt : veille directe de chaque batterie configurée")
            stocks = [b.stock_id for b in self.config.batteries]
        for stock_id in stocks:
            try:
                self.decisions.safe_standby(stock_id, "arrêt de l'agent")
            except Exception:  # noqa: BLE001 - l'arrêt ne doit pas échouer
                self._emergency_standby(stock_id)
        try:
            self.sync()
        except Exception:  # noqa: BLE001 - l'arrêt ne doit pas échouer
            LOG.warning("dernière synchronisation impossible à l'arrêt")

    def _emergency_standby(self, stock_id: int) -> None:
        """Veille publiée sans passer par la base (disque plein, base arrêtée) : rien n'est tracé, mais la batterie s'arrête."""
        prefix = self._command_prefix(stock_id)
        if not prefix:
            return
        now = utcnow()
        command = {
            "command_id": f"CMD-{now.strftime('%Y%m%d-%H%M%S')}-urgence",
            "action": rules.STANDBY,
            "power_w": 0.0,
            "duration_s": 0,
            "priority": 1,
            "timestamp": now.isoformat().replace("+00:00", "Z"),
            "source": "SAFE_STANDBY",
        }
        try:
            payload = json.dumps(self.decisions.sign(command))
        except Exception:  # noqa: BLE001 - TPM injoignable : la veille part quand même, la passerelle arrête toujours sur STANDBY
            payload = json.dumps(command)
        try:
            ok = self.publisher.publish(f"{prefix}/standby", payload, 1)
            LOG.critical(
                "veille d'urgence de la batterie %s %s",
                stock_id,
                "publiée" if ok else "NON publiée",
            )
        except Exception:  # noqa: BLE001 - dernier recours
            LOG.critical("veille d'urgence de la batterie %s impossible", stock_id)

    def health(self) -> tuple[int, dict]:
        """Santé locale (D295) : base locale et broker MQTT du site. 503 si l'un manque : sans broker, ni mesure ni ordre ne passe.

        Un publieur sans état de connexion (tests, banc) est réputé connecté ; le cloud n'est pas une condition (mode dégradé prévu).
        """
        try:
            db_ok = bool(self.db.ping())
        except Exception:  # noqa: BLE001 - une sonde ne lève pas
            db_ok = False
        # MqttBridge.connected est une propriété ; un publieur sans cet attribut (tests, banc) est réputé connecté.
        connected = getattr(self.publisher, "connected", True)
        mqtt_ok = bool(connected() if callable(connected) else connected)
        checks = {
            "database": "UP" if db_ok else "DOWN",
            "mqtt_broker": "UP" if mqtt_ok else "DOWN",
        }
        up = db_ok and mqtt_ok
        return (200 if up else 503), {
            "status": "UP" if up else "DOWN",
            "state": self.states.state,
            "checks": checks,
        }

    def status(self) -> dict:
        return {
            "state": self.states.state,
            "version": __version__,
            "agent_uuid": self.identity.agent_uuid if self.identity else None,
            "site_id": self.identity.site_id if self.identity else None,
            "buffer": {
                "size": self.buffer.size(),
                "by_priority": self.buffer.by_priority(),
            },
            "clock_synced": self.clock.synced,
            "latency_ms": {
                "cloud": self.cloud.last_latency_ms,
                "decision_receive": self.decisions.last_receive_ms,
                "decision_execute": self.decisions.last_execute_ms,
                "measurement_parse": self.collector.last_parse_ms,
            },
            "cloud_channel": {
                "transport": "MQTT" if self.pushed() else "HTTP",
                "mqtt_configured": self.cloud_mqtt is not None,
                "orders_received": None if self.cloud_mqtt is None else self.cloud_mqtt.received,
                "published": None if self.cloud_mqtt is None else self.cloud_mqtt.published,
                "last_publish_ms": None if self.cloud_mqtt is None else self.cloud_mqtt.last_publish_ms,
            },
            "integrity": {"ok": self.integrity.ok, "reason": self.integrity.reason},
            "execution_blocked": self.execution_blocked,
            "manual_mode": self.decisions.manual_mode,
            "key_store": self.cloud._keys.kind,
            "inference": self.forecaster.status(),
            "key_algorithm": getattr(self.cloud._keys, "algorithm", None),
        }


def parse_cursor(value: str | None) -> dt.datetime | None:
    return parse_instant(value) if value else None

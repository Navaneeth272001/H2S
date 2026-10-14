"""Réception, cache et exécution des décisions du cloud (F18.3, CA-18.10 à CA-18.15, D247, D250) ; décisions locales en mode
dégradé (CDC §2.18, §5.1 ; R5-20, C-9, E182, amende D247 et D258).

En ligne, le cloud décide : l'agent vérifie la signature de chaque ordre, le met en cache, le revalide par SEC1-6 sur son
propre état (SoC mesuré, dernière transition), l'exécute et en rend compte. Hors réseau, l'ordre du cloud encore dans sa durée
va à son terme ; **un ordre échu n'est jamais rejoué** : l'agent applique alors lui-même la règle §5.1 (``local_policy``) sur
ses mesures de l'instant, le dernier prix day-ahead connu de l'heure (sans prix, la condition de prix n'est pas remplie) et
les seuils du site reçus du cloud, puis SEC1-6 locales ; source ``LOCAL_AUTO``, rendue au cloud à la reconnexion. Sans
pilotage automatique activé pour le site, sans bilan complet ou sans SoC, rien n'est décidé : une action en cours s'arrête
(STANDBY sûr) à la fin de sa durée.

Correctifs de la double relecture (D258) :

* **chien de garde** : pendant une charge ou une décharge, SEC2 et SEC3 et la fraîcheur du SoC sont revus à chaque passage
  (10 s) ; un seuil atteint (borne exacte comprise, R5-07) ou un SoC muet met la batterie en veille — la validation au départ ne
  suffit pas ;
* seul l'ordre **le plus récent** d'une batterie peut être en vigueur : échu, il ne rend pas la main à un ordre plus ancien ;
* hors réseau, le cache n'est rejoué que sur une horloge digne de foi (synchronisée, jamais antérieure au dernier ordre
  reçu) : un boîtier redémarré sans horloge matérielle ne ressuscite pas des ordres échus ;
* un rejet SEC6 est temporaire : l'ordre est retenté une fois l'intervalle écoulé ; une publication en échec est retentée
  toutes les 5 minutes, pas à chaque passage ;
* mode manuel du technicien borné à une heure, signalé ; le chien de garde reste actif.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from . import crypto, local_policy, rules
from .buffer import Buffer
from .clock import parse_instant
from .crypto import verify_signed_payload
from .storage import Database

MAX_VALIDITY = dt.timedelta(days=7, minutes=1)
SOC_MAX_AGE = dt.timedelta(minutes=10)
FAILED_RETRY = dt.timedelta(minutes=5)
CLOCK_TOLERANCE = dt.timedelta(minutes=5)
MANUAL_MODE_MAX = dt.timedelta(hours=1)
#: Décision locale : durée d'une commande (renouvelée tant que la règle la maintient) et fraîcheur exigée des mesures du bilan.
LOCAL_DURATION_S = 300
LOCAL_RENEW_BEFORE = dt.timedelta(seconds=60)
LOCAL_DATA_MAX_AGE = dt.timedelta(minutes=3)
#: Écart de puissance en deçà duquel l'action locale en cours n'est pas renvoyée (comme AutoPilot.POWER_DEADBAND_*).
LOCAL_DEADBAND_W = 50.0
LOCAL_DEADBAND_RATIO = 0.2
#: Énergie d'une décharge close retenue si les relevés de la batterie couvrent 80 % de sa durée (comme DecisionCloser).
MIN_MEASURED_COVERAGE = 0.8
PRICES_KEPT = dt.timedelta(days=8)
#: Cache des 100 dernières décisions (§2.1, E184).
CACHE_MAX = 100
#: Nombre maximal de fenêtres de capacité réservée gardées (C4) : l'horizon transmis est de 48 h.
RESERVED_MAX = 200


def _reserved_windows(raw) -> list:
    """Fenêtres de capacité réservée reçues du cloud, normalisées (``from``, ``to``, ``reason``) ; entrées illisibles ignorées."""
    out = []
    for w in raw or []:
        if not isinstance(w, dict):
            continue
        start, end = w.get("from"), w.get("to")
        if not isinstance(start, str) or not isinstance(end, str):
            continue
        try:
            parse_instant(start)
            parse_instant(end)
        except (ValueError, TypeError):
            continue
        reason = w.get("reason")
        out.append(
            {
                "from": start,
                "to": end,
                "reason": reason if isinstance(reason, str) else "RESERVED",
            }
        )
        if len(out) >= RESERVED_MAX:
            break
    return out


def capacity_reserved(policy: dict | None, now: dt.datetime, clock_trusted: bool = True) -> bool:
    """La capacité de la batterie est-elle réservée à cet instant (C4) ?

    Le cloud transmet les fenêtres réservées (offre de flexibilité acceptée, engagement de portefeuille, ordre d'ajustement
    RTE) et l'horizon ``reserved_until`` sur lequel cette liste fait foi. Hors de cet horizon — politique trop ancienne,
    coupure longue — ou sur une horloge non fiable, l'agent ne sait plus : il se comporte alors **comme si** la capacité
    était réservée et ne décharge pas sur un pic. Vider une capacité vendue coûte une pénalité au client ; ne pas la vider
    ne coûte qu'une économie d'autoconsommation. Un cloud antérieur n'envoie ni liste ni horizon : rien n'est réservé et le
    comportement d'avant le lot 3 est conservé (compatibilité ascendante).
    """
    if not isinstance(policy, dict) or not policy.get("reserved_until"):
        return False
    if not clock_trusted:
        return True
    try:
        until = parse_instant(str(policy["reserved_until"]))
    except (ValueError, TypeError):
        return True
    if now >= until:
        return True
    for w in policy.get("reserved") or []:
        try:
            if parse_instant(str(w["from"])) <= now < parse_instant(str(w["to"])):
                return True
        except (KeyError, TypeError, ValueError):
            continue
    return False


class Publisher(Protocol):
    def publish(self, topic: str, payload: str, qos: int = 1) -> bool: ...


@dataclass(frozen=True)
class Order:
    decision_id: str
    site_id: int
    stock_id: int
    action: str
    power_w: float
    duration_s: int
    priority: int
    origin: str
    valid_from: dt.datetime
    expires_at: dt.datetime


@dataclass(frozen=True)
class Identity:
    agent_uuid: str
    site_id: int
    decision_key_id: str
    decision_public_key: str
    gradient_public_key: str


class DecisionManager:
    def __init__(
        self,
        db: Database,
        buffer: Buffer,
        publisher: Publisher,
        identity: Callable[[], Identity | None],
        command_prefix: Callable[[int], str | None],
        now: Callable[[], dt.datetime],
        alert: Callable[[str, str, str], None] | None = None,
        clock_synced: Callable[[], bool] | None = None,
        signer: object | None = None,
        devices: Callable[[], list[tuple[str, str]]] | None = None,
    ) -> None:
        self._db = db
        self._buffer = buffer
        self._publisher = publisher
        self._identity = identity
        self._prefix = command_prefix
        self._now = now
        self._alert = alert or (lambda code, severity, message: None)
        self._synced = clock_synced or (lambda: True)
        #: Clé d'identité (``KeyStore``) qui signe les commandes publiées vers la batterie (E183) ; l'agent en service en a toujours une.
        self._signer = signer if hasattr(signer, "sign") else None
        #: Équipements de mesure du bilan local : (clé ``device:N``, rôle ``CONSUMPTION`` ou ``PRODUCTION`` d'agent.json).
        self._devices = devices or (list)
        self.manual_until: dt.datetime | None = None
        self.last_receive_ms: int | None = None
        self.last_execute_ms: int | None = None

    @property
    def manual_mode(self) -> bool:
        return self.manual_until is not None and self._now() < self.manual_until

    def set_manual(self, enabled: bool) -> dt.datetime | None:
        """Mode manuel du technicien (API locale) : suspend les ordres du cloud une heure au plus, jamais le chien de garde."""
        now = self._now()
        self.manual_until = now + MANUAL_MODE_MAX if enabled else None
        self._alert(
            "MANUAL_MODE",
            "WARNING",
            "mode manuel activé jusqu'à " + self.manual_until.isoformat() if enabled else "mode manuel désactivé",
        )
        return self.manual_until

    # ------------------------------------------------------------------ réception (CA-18.10, CA-18.13)

    def receive(self, signed: list[dict]) -> list[Order]:
        """Vérifie et met en cache ; renvoie les ordres nouveaux. Un ordre faux est écarté et signalé, jamais exécuté."""
        ident = self._identity()
        if ident is None:
            return []
        started = time.monotonic()
        fresh: list[Order] = []
        now = self._now()
        for item in signed or []:
            try:
                order = self._verify(ident, item, now)
            except ValueError as exc:
                self._alert(
                    "DECISION_SIGNATURE_INVALID",
                    "CRITICAL",
                    f"ordre du cloud écarté : {exc}",
                )
                continue
            with self._db.tx() as cur:
                cur.execute(
                    "INSERT INTO decision_cache (site_id, decision_id, stock_id, action, power_w, duration_s, priority, origin, valid_from,"
                    " received_at, expires_at, payload, signature, key_id, target_devices) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                    " %s, %s, %s, %s) ON CONFLICT (decision_id) DO NOTHING",
                    (
                        str(ident.site_id),
                        order.decision_id,
                        order.stock_id,
                        order.action,
                        order.power_w,
                        order.duration_s,
                        order.priority,
                        order.origin,
                        order.valid_from,
                        now,
                        order.expires_at,
                        item["payload"],
                        item["signature"],
                        item.get("key_id"),
                        json.dumps({"stock_id": order.stock_id}),
                    ),
                )
                if cur.rowcount == 1:
                    fresh.append(order)
        self.last_receive_ms = int((time.monotonic() - started) * 1000)
        return fresh

    def _verify(self, ident: Identity, item: dict, now: dt.datetime) -> Order:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("payload"), str)
            or not isinstance(item.get("signature"), str)
        ):
            raise ValueError("ordre mal formé")  # noqa: TRY004 - contenu mal formé, pas un type d'argument
        if item.get("key_id") and item["key_id"] != ident.decision_key_id:
            raise ValueError("clé de signature inconnue (réenrôler l'agent après rotation)")
        p = verify_signed_payload(ident.decision_public_key, item["payload"], item["signature"])
        if p.get("type") != "decision" or p.get("v") != 1:
            raise ValueError("charge utile qui n'est pas un ordre")
        if int(p["site_id"]) != ident.site_id:
            raise ValueError("ordre destiné à un autre site")
        action = p["action"]
        if action not in (rules.CHARGE, rules.DISCHARGE, rules.STANDBY):
            raise ValueError("action inconnue")
        valid_from, expires = (
            parse_instant(p["valid_from"]),
            parse_instant(p["expires_at"]),
        )
        if expires <= valid_from or expires - valid_from > MAX_VALIDITY:
            raise ValueError("validité incohérente")
        if valid_from > now + dt.timedelta(minutes=5):
            raise ValueError("ordre daté dans le futur")
        return Order(
            str(uuid.UUID(p["decision_id"])),
            ident.site_id,
            int(p["stock_id"]),
            action,
            float(p["power_w"]),
            int(p["duration_s"]),
            int(p.get("priority", 2)),
            str(p.get("origin", "AUTO")),
            valid_from,
            expires,
        )

    # ------------------------------------------------------------------ état local d'une batterie

    def soc(self, stock_id: int, now: dt.datetime) -> float | None:
        # Un relevé daté dans le futur (horloge de passerelle en avance) ne fige pas le SoC (audit B18-2).
        with self._db.tx() as cur:
            cur.execute(
                "SELECT value, measurement_timestamp FROM local_measurements WHERE device_id = %s AND data_type = 'soc_pct'"
                " AND measurement_timestamp <= %s ORDER BY measurement_timestamp DESC LIMIT 1",
                (f"stock:{stock_id}", now + CLOCK_TOLERANCE),
            )
            row = cur.fetchone()
        if row is None or now - row["measurement_timestamp"] > SOC_MAX_AGE:
            return None
        return float(row["value"])

    def last_execution(self, stock_id: int) -> dict | None:
        with self._db.tx() as cur:
            cur.execute(
                "SELECT action, source, decision_ref, executed_at, duration_s, power_w FROM local_decisions WHERE stock_id = %s"
                " AND execution_status = 'EXECUTED'"
                " ORDER BY executed_at DESC, decision_id DESC LIMIT 1",
                (stock_id,),
            )
            return cur.fetchone()

    def last_transition(self, stock_id: int) -> dt.datetime | None:
        """Dernier changement d'état effectif (SEC6) : la plus récente exécution dont l'action diffère de la précédente."""
        with self._db.tx() as cur:
            cur.execute(
                "SELECT action, executed_at FROM local_decisions WHERE stock_id = %s AND execution_status = 'EXECUTED'"
                " ORDER BY executed_at DESC, decision_id DESC LIMIT 50",
                (stock_id,),
            )
            rows = cur.fetchall()
        for newer, older in itertools.pairwise(rows):
            if newer["action"] != older["action"]:
                return newer["executed_at"]
        return rows[-1]["executed_at"] if rows else None

    def rules_for(self, stock_id: int) -> rules.Thresholds:
        with self._db.tx() as cur:
            cur.execute(
                "SELECT rule_code, threshold_value FROM local_security_rules WHERE stock_id = %s AND is_active",
                (stock_id,),
            )
            found = {r["rule_code"]: r["threshold_value"] for r in cur.fetchall()}
        defaults = rules.Thresholds()
        # Bornes du §5.2 appliquées aussi à la lecture : une règle enregistrée avant le lot 3 ou saisie localement ne les élargit pas.
        return rules.Thresholds.bounded(
            float(found.get("SEC2", defaults.soc_min)),
            float(found.get("SEC3", defaults.soc_max)),
            float(found.get("SEC4", defaults.p_charge_max_w)),
            float(found.get("SEC5", defaults.p_discharge_max_w)),
            int(found.get("SEC6", defaults.min_transition_interval_s)),
        )

    def latest_valid(self, stock_id: int, now: dt.datetime) -> Order | None:
        """L'ordre le plus récent de la batterie s'il court encore ; échu, aucun : un ordre plus ancien ne revient pas (audit B18-10)."""
        with self._db.tx() as cur:
            cur.execute(
                "SELECT decision_id, site_id, stock_id, action, power_w, duration_s, priority, origin, valid_from, expires_at FROM decision_cache"
                " WHERE stock_id = %s AND coalesce(valid_from, received_at) <= %s"
                " ORDER BY coalesce(valid_from, received_at) DESC, cache_id DESC LIMIT 1",
                (stock_id, now),
            )
            r = cur.fetchone()
        if r is None or r["expires_at"] <= now:
            return None
        return Order(
            r["decision_id"],
            int(r["site_id"]),
            r["stock_id"],
            r["action"],
            float(r["power_w"] or 0),
            int(r["duration_s"] or 0),
            int(r["priority"] or 2),
            r["origin"] or "AUTO",
            r["valid_from"],
            r["expires_at"],
        )

    def cache_trusted(self, now: dt.datetime) -> bool:
        """Horloge digne de foi pour rejouer le cache : synchronisée, et pas en retard sur le plus récent ordre signé reçu."""
        if not self._synced():
            return False
        with self._db.tx() as cur:
            cur.execute("SELECT max(valid_from) AS latest FROM decision_cache")
            latest = cur.fetchone()["latest"]
        return latest is None or now >= latest - CLOCK_TOLERANCE

    # ------------------------------------------------------------------ exécution (CA-18.11, CA-18.12, CA-18.14)

    def watchdog(self, stock_id: int, now: dt.datetime) -> str | None:
        """Batterie en charge ou décharge : SEC2, SEC3 et SoC frais revus ; en veille au premier écart (audit B18-1)."""
        last = self.last_execution(stock_id)
        if last is None or last["action"] == rules.STANDBY:
            return None
        soc = self.soc(stock_id, now)
        t = self.rules_for(stock_id)
        if soc is None:
            reason = "SoC inconnu depuis plus de 10 minutes pendant l'ordre"
        elif last["action"] == rules.CHARGE and soc >= t.soc_max:
            reason = f"SEC3 : SoC {soc:g} % à {t.soc_max:g} % ou au-dessus pendant la charge"
        elif last["action"] == rules.DISCHARGE and soc <= t.soc_min:
            reason = f"SEC2 : SoC {soc:g} % à {t.soc_min:g} % ou en dessous pendant la décharge"
        else:
            return None
        self._alert("BATTERY_WATCHDOG", "CRITICAL", f"batterie {stock_id} : {reason}")
        return self.safe_standby(stock_id, reason)

    def tick(self, stock_ids: list[int], online: bool) -> list[str]:
        """Pour chaque batterie : chien de garde ; exécuter l'ordre en vigueur jamais tenté (ou refusé SEC6 dont l'intervalle est
        écoulé, ou dont la publication a échoué il y a 5 minutes) ; hors réseau et sur horloge fiable, rejouer l'ordre en vigueur
        après sa durée (5 minutes au moins entre deux tentatives) ; une action terminée sans ordre à suivre revient à STANDBY sûr.
        Renvoie les statuts d'exécution."""
        out: list[str] = []
        now = self._now()
        for stock in stock_ids:
            guard = self.watchdog(stock, now)
            if guard is not None:
                out.append(guard)
                continue
            if self.manual_mode:
                continue
            if not online:
                status = self._offline(stock, now)
                if status is not None:
                    out.append(status)
                continue
            order = self.latest_valid(stock, now)
            if order is not None:
                attempt = self._last_try(order.decision_id)
                if attempt is None or self._retry_due(stock, attempt, now):
                    out.append(self.execute(order, "CLOUD_LIVE"))
                    continue
            last = self.last_execution(stock)
            if last is None or last["action"] == rules.STANDBY:
                continue
            if last["executed_at"] + dt.timedelta(seconds=self._duration(last)) <= now:
                out.append(
                    self.safe_standby(
                        stock,
                        "ordre terminé, aucun ordre valide à suivre" if order is None else "ordre terminé",
                    )
                )
        return out

    # ------------------------------------------------------------------ mode dégradé autonome (§2.18, R5-20)

    def _offline(self, stock: int, now: dt.datetime) -> str | None:
        """Hors réseau : l'ordre du cloud encore dans sa durée va à son terme (exécuté s'il ne l'a pas été), sur une horloge digne
        de foi ; sinon décision locale §5.1. Un ordre échu n'est jamais rejoué (E182)."""
        trusted = self.cache_trusted(now)
        if not trusted:
            last = self.last_execution(stock)
            if last is not None and last["action"] != rules.STANDBY and last["source"] != "LOCAL_AUTO":
                # Horloge douteuse (boîtier redémarré sans heure) : un ordre du cloud n'est plus daté de façon sûre (D258).
                return self.safe_standby(
                    stock,
                    "hors réseau sur horloge non fiable : ordre en cache non rejoué",
                )
        order = self.latest_valid(stock, now) if trusted else None
        if order is not None and now < order.valid_from + dt.timedelta(seconds=order.duration_s):
            attempt = self._last_try(order.decision_id)
            if attempt is None or self._retry_due(stock, attempt, now):
                return self.execute(order, "CLOUD_CACHE")
            return None
        # Sans horloge digne de foi, l'heure du prix est incertaine : décision locale sans condition de prix.
        return self.local(stock, now, trusted)

    def local(self, stock: int, now: dt.datetime, clock_trusted: bool = True) -> str | None:
        """Décision locale §5.1 d'une batterie, puis SEC1-6 ; renvoie le statut d'une commande publiée, ``None`` sinon."""
        last = self.last_execution(stock)
        active = last is not None and last["action"] != rules.STANDBY
        expired = active and last["executed_at"] + dt.timedelta(seconds=self._duration(last)) <= now
        policy = self.policy()
        soc = self.soc(stock, now)
        balance = self.balance(policy, now)
        reason = None
        if policy is None:
            reason = "aucun seuil du site reçu du cloud"
        elif not policy.get("auto_pilot"):
            reason = "pilotage automatique non activé pour ce site"
        elif stock in local_policy.manual_override(policy):
            # V2 : mode manuel posé depuis la plateforme (§2.5). Il est opposé aux pilotages du cloud ; il doit l'être
            # aussi à l'autonomie locale, sinon une coupure du lien rendait la batterie à la règle §5.1 pendant qu'un
            # technicien intervenait sur l'installation.
            reason = "mode manuel en cours sur cette batterie (plateforme)"
        elif balance is None:
            reason = "mesures du site incomplètes ou anciennes"
        elif soc is None:
            reason = "SoC inconnu"
        if reason is not None:
            # Rien n'est décidé sans les données : l'action locale s'arrête tout de suite, un ordre du cloud à la fin de sa durée.
            if active and (expired or last["source"] == "LOCAL_AUTO"):
                return self.safe_standby(stock, f"mode dégradé sans décision locale : {reason}")
            return None
        production, consumption = balance
        t = self.rules_for(stock)
        price = self.price(now) if clock_trusted else None
        if price is None:
            self._alert(
                "SPOT_PRICE_UNAVAILABLE",
                "WARNING",
                "mode dégradé : aucun prix day-ahead connu pour l'heure, charge locale suspendue",
            )
        th = local_policy.thresholds(policy, t.soc_min, t.soc_max)
        reserved = capacity_reserved(policy, now, clock_trusted)
        action, power = local_policy.decide(
            soc,
            production - consumption,
            consumption,
            th,
            t.p_charge_max_w,
            t.p_discharge_max_w,
            price,
            reserved,
        )
        if action == rules.STANDBY:
            if active:
                return self._command(stock, None, "LOCAL_AUTO", rules.STANDBY, 0.0, 0, 2, soc, now)
            return None
        if (
            active
            and last["action"] == action
            and last["source"] == "LOCAL_AUTO"
            and not expired
            and last["executed_at"] + dt.timedelta(seconds=self._duration(last)) - LOCAL_RENEW_BEFORE > now
            and abs(power - float(last["power_w"] or 0)) < max(LOCAL_DEADBAND_W, LOCAL_DEADBAND_RATIO * power)
        ):
            return None
        verdict = rules.validate(
            t,
            action,
            power,
            soc,
            now,
            last["action"] if last else None,
            self.last_transition(stock),
        )
        if not verdict.valid:
            if verdict.rule == "SEC1":
                # Charge pendant une décharge (ou l'inverse) : arrêt d'abord ; la nouvelle action suit après le délai SEC6.
                return self._command(stock, None, "LOCAL_AUTO", rules.STANDBY, 0.0, 0, 2, soc, now)
            if verdict.rule != "SEC6":
                self._record(
                    stock,
                    None,
                    "LOCAL_AUTO",
                    action,
                    power,
                    soc,
                    "REJECTED",
                    verdict.rule,
                    verdict.message,
                    None,
                    now,
                )
            return None
        return self._command(
            stock,
            None,
            "LOCAL_AUTO",
            action,
            verdict.power_w,
            LOCAL_DURATION_S,
            2,
            soc,
            now,
        )

    def store_policy(self, policy: dict | None, prices: list | None, now: dt.datetime) -> None:
        """Seuils §5.1 du site, rôle des équipements et prix day-ahead reçus du cloud (``/api/agent/rules``), gardés pour le mode dégradé."""
        with self._db.tx() as cur:
            if isinstance(policy, dict):
                kept = {
                    k: policy.get(k)
                    for k in (
                        "auto_pilot",
                        "surplus_min",
                        "peak_threshold",
                        "soc_reserve",
                        "price_low",
                        "price_high",
                        "soc_min",
                        "soc_max",
                    )
                }
                kept["auto_pilot"] = policy.get("auto_pilot") is True
                roles = {}
                for d in policy.get("devices") or []:
                    if (
                        isinstance(d, dict)
                        and str(d.get("role")) in ("CONSUMPTION", "PRODUCTION")
                        and str(d.get("device_id", "")).isdigit()
                    ):
                        roles[f"device:{int(d['device_id'])}"] = d["role"]
                kept["roles"] = roles
                # C4 : capacité réservée du site (offre de flexibilité acceptée, engagement de portefeuille, ordre RTE) et
                # horizon sur lequel la liste fait foi. Un cloud antérieur ne les envoie pas : liste vide, horizon inconnu.
                kept["reserved"] = _reserved_windows(policy.get("reserved"))
                kept["reserved_until"] = (
                    policy.get("reserved_until") if isinstance(policy.get("reserved_until"), str) else None
                )
                # V2 : batteries sous mode manuel §2.5 côté plateforme. Gardées avec les seuils, donc opposables hors réseau.
                kept["manual_override"] = sorted(local_policy.manual_override(policy))
                kept["received_at"] = now.isoformat()
                cur.execute(
                    "INSERT INTO agent_kv (key, value, updated_at) VALUES ('policy.site', %s, now()) ON CONFLICT (key) DO UPDATE"
                    " SET value = EXCLUDED.value, updated_at = now()",
                    (json.dumps(kept),),
                )
            for p in prices or []:
                try:
                    hour = parse_instant(p["hour_start"])
                    value = float(p["eur_per_kwh"])
                except (KeyError, TypeError, ValueError):
                    continue
                if hour.minute or hour.second or hour.microsecond or not -10 <= value <= 10:
                    continue
                cur.execute(
                    "INSERT INTO local_spot_prices (hour_start, eur_per_kwh, received_at) VALUES (%s, %s, %s) ON CONFLICT (hour_start)"
                    " DO UPDATE SET eur_per_kwh = EXCLUDED.eur_per_kwh, received_at = EXCLUDED.received_at",
                    (hour, value, now),
                )
            cur.execute(
                "DELETE FROM local_spot_prices WHERE hour_start < %s",
                (now - PRICES_KEPT,),
            )

    def policy(self) -> dict | None:
        with self._db.tx() as cur:
            cur.execute("SELECT value FROM agent_kv WHERE key = 'policy.site'")
            r = cur.fetchone()
        return json.loads(r["value"]) if r else None

    def price(self, now: dt.datetime) -> float | None:
        """Dernier prix day-ahead connu pour l'heure (€/kWh), ``None`` sans prix."""
        hour = now.astimezone(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
        with self._db.tx() as cur:
            cur.execute(
                "SELECT eur_per_kwh FROM local_spot_prices WHERE hour_start = %s",
                (hour,),
            )
            r = cur.fetchone()
        return float(r["eur_per_kwh"]) if r else None

    def balance(self, policy: dict | None, now: dt.datetime) -> tuple[float, float] | None:
        """(production, consommation) en W : dernier relevé de puissance de chaque équipement du bilan, frais de moins de 3 minutes.
        Un équipement muet rend le bilan incomplet (``None``) : aucune décision sur une partie du site."""
        devices = self._devices()
        if not devices:
            return None
        roles = (policy or {}).get("roles") or {}
        keys = [k for k, _ in devices]
        with self._db.tx() as cur:
            cur.execute(
                "SELECT DISTINCT ON (device_id) device_id, value, measurement_timestamp FROM local_measurements WHERE device_id = ANY(%s)"
                " AND data_type = 'power_w' AND measurement_timestamp <= %s ORDER BY device_id, measurement_timestamp DESC",
                (keys, now + CLOCK_TOLERANCE),
            )
            last = {r["device_id"]: r for r in cur.fetchall()}
        production = consumption = 0.0
        for key, role in devices:
            r = last.get(key)
            if r is None or now - r["measurement_timestamp"] > LOCAL_DATA_MAX_AGE:
                return None
            value = max(0.0, float(r["value"]))
            if roles.get(key, role) == "PRODUCTION":
                production += value
            else:
                consumption += value
        return production, consumption

    def resume(self, stock_ids: list[int], online: bool) -> list[str]:
        """Au démarrage : l'ordre en vigueur des batteries arrêtées par la sortie du service reprend, revalidé (relecture B18-9).
        L'orchestrateur ne passe que ces batteries : une veille du chien de garde ou de fin d'ordre n'est jamais annulée."""
        out: list[str] = []
        now = self._now()
        if not (online or self.cache_trusted(now)):
            return out
        for stock in stock_ids:
            last = self.last_execution(stock)
            order = self.latest_valid(stock, now)
            if last is None or last["source"] != "SAFE_STANDBY" or order is None or order.action == rules.STANDBY:
                continue
            with self._db.tx() as cur:
                cur.execute(
                    "SELECT executed_at FROM local_decisions WHERE decision_ref = %s AND execution_status = 'EXECUTED'"
                    " ORDER BY executed_at DESC LIMIT 1",
                    (order.decision_id,),
                )
                prior = cur.fetchone()
            if (
                prior is not None
                and prior["executed_at"] <= last["executed_at"]
                and prior["executed_at"] + dt.timedelta(seconds=order.duration_s) > now
            ):
                out.append(self.execute(order, "CLOUD_LIVE" if online else "CLOUD_CACHE"))
        return out

    def _last_try(self, decision_id: str) -> dict | None:
        with self._db.tx() as cur:
            cur.execute(
                "SELECT execution_status, rejection_rule, created_at FROM local_decisions WHERE decision_ref = %s"
                " ORDER BY created_at DESC, decision_id DESC LIMIT 1",
                (decision_id,),
            )
            return cur.fetchone()

    def _retry_due(self, stock_id: int, attempt: dict, now: dt.datetime) -> bool:
        if attempt["execution_status"] == "FAILED":
            return attempt["created_at"] + FAILED_RETRY <= now
        if attempt["execution_status"] == "REJECTED" and attempt["rejection_rule"] == "SEC6":
            since = self.last_transition(stock_id)
            return (
                since is None or since + dt.timedelta(seconds=self.rules_for(stock_id).min_transition_interval_s) <= now
            )
        return False

    def _last_attempt_at(self, stock_id: int) -> dt.datetime | None:
        with self._db.tx() as cur:
            cur.execute(
                "SELECT max(created_at) AS at FROM local_decisions WHERE stock_id = %s",
                (stock_id,),
            )
            r = cur.fetchone()
        return r["at"] if r else None

    def _duration(self, last: dict) -> int:
        if last.get("duration_s") is not None:
            return int(last["duration_s"])
        if last["decision_ref"] is None:
            return 3600
        with self._db.tx() as cur:
            cur.execute(
                "SELECT duration_s FROM decision_cache WHERE decision_id = %s",
                (last["decision_ref"],),
            )
            r = cur.fetchone()
        return int(r["duration_s"]) if r and r["duration_s"] else 3600

    def execute(self, order: Order, source: str) -> str:
        started = time.monotonic()
        now = self._now()
        soc = self.soc(order.stock_id, now)
        last = self.last_execution(order.stock_id)
        verdict = rules.validate(
            self.rules_for(order.stock_id),
            order.action,
            order.power_w,
            soc,
            now,
            last["action"] if last else None,
            self.last_transition(order.stock_id),
        )
        if not verdict.valid:
            self._record(
                order.stock_id,
                order.decision_id,
                source,
                order.action,
                order.power_w,
                soc,
                "REJECTED",
                verdict.rule,
                verdict.message,
                None,
                now,
            )
            self._alert(
                "DECISION_REJECTED_LOCALLY",
                "WARNING",
                f"{order.action} refusé localement ({verdict.rule}) : {verdict.message}",
            )
            self.safe_standby(order.stock_id, f"rejet {verdict.rule} de l'ordre {order.decision_id}")
            return "REJECTED"
        status = self._command(
            order.stock_id,
            order.decision_id,
            source,
            order.action,
            verdict.power_w,
            order.duration_s,
            order.priority,
            soc,
            now,
        )
        self.last_execute_ms = int((time.monotonic() - started) * 1000)
        return status

    def safe_standby(self, stock_id: int, reason: str) -> str:
        now = self._now()
        last = self.last_execution(stock_id)
        if last is not None and last["action"] == rules.STANDBY and last["source"] == "SAFE_STANDBY":
            return "EXECUTED"
        with self._db.tx() as cur:
            cur.execute(
                "SELECT execution_status, created_at FROM local_decisions WHERE stock_id = %s AND source = 'SAFE_STANDBY'"
                " ORDER BY created_at DESC, decision_id DESC LIMIT 1",
                (stock_id,),
            )
            previous = cur.fetchone()
        if (
            previous is not None
            and previous["execution_status"] == "FAILED"
            and previous["created_at"] + FAILED_RETRY > now
        ):
            # Broker local en panne : nouvel essai dans 5 minutes, pas une ligne et un compte rendu toutes les 10 secondes (relecture B18-5).
            return "FAILED"
        status = self._command(
            stock_id,
            None,
            "SAFE_STANDBY",
            rules.STANDBY,
            0.0,
            0,
            1,
            self.soc(stock_id, now),
            now,
        )
        if status == "EXECUTED":
            # Une veille que le broker n'a pas prise n'est pas annoncée faite : son compte rendu « en échec » part au cloud (D290).
            self._alert(
                "SAFE_STANDBY",
                "WARNING",
                f"batterie {stock_id} mise en STANDBY : {reason}",
            )
        return status

    def manual(self, stock_id: int, action: str, power_w: float, duration_s: int) -> dict:
        """Ordre manuel depuis l'API locale (technicien) : mêmes règles SEC1-6, source MANUAL, rendu au cloud."""
        now = self._now()
        soc = self.soc(stock_id, now)
        last = self.last_execution(stock_id)
        verdict = rules.validate(
            self.rules_for(stock_id),
            action,
            power_w,
            soc,
            now,
            last["action"] if last else None,
            self.last_transition(stock_id),
        )
        if not verdict.valid:
            self._record(
                stock_id,
                None,
                "MANUAL",
                action,
                power_w,
                soc,
                "REJECTED",
                verdict.rule,
                verdict.message,
                None,
                now,
            )
            return {"valid": False, "rule": verdict.rule, "message": verdict.message}
        status = self._command(stock_id, None, "MANUAL", action, verdict.power_w, duration_s, 1, soc, now)
        return {
            "valid": True,
            "status": status,
            "power_w": verdict.power_w,
            "warnings": verdict.warnings,
        }

    def _command(
        self,
        stock_id: int,
        decision_ref: str | None,
        source: str,
        action: str,
        power_w: float,
        duration_s: int,
        priority: int,
        soc: float | None,
        now: dt.datetime,
    ) -> str:
        prefix = self._prefix(stock_id)
        command_id = f"CMD-{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        payload = json.dumps(
            self.sign(
                {
                    "command_id": command_id,
                    "action": action,
                    "power_w": round(power_w, 2),
                    "duration_s": duration_s,
                    "priority": priority,
                    "timestamp": now.isoformat().replace("+00:00", "Z"),
                    "source": source,
                }
            )
        )
        topic = f"{prefix}/{action.lower()}" if prefix else None
        ok = bool(topic) and self._publisher.publish(topic, payload, 1)
        status = "EXECUTED" if ok else "FAILED"
        error = (
            None
            if ok
            else (
                "aucun préfixe de commande configuré pour cette batterie"
                if not topic
                else "publication MQTT impossible"
            )
        )
        closes = self._closing(stock_id, now) if ok else None
        decision_row = self._record(
            stock_id,
            decision_ref,
            source,
            action,
            power_w,
            soc,
            status,
            None,
            error,
            command_id,
            now,
            duration_s,
            closes,
        )
        with self._db.tx() as cur:
            cur.execute(
                "INSERT INTO local_commands (decision_id, site_id, target_device, action, power_w, duration_s, priority, status, mqtt_topic,"
                " mqtt_payload, sent_at, executed_at, error_message) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    decision_row,
                    self._site(),
                    f"stock:{stock_id}",
                    action,
                    power_w,
                    duration_s,
                    priority,
                    "EXECUTED" if ok else "FAILED",
                    topic,
                    payload,
                    now,
                    now if ok else None,
                    error,
                ),
            )
        return status

    def sign(self, command: dict) -> dict:
        """Commande signée pour la passerelle (§4.3.5, E183) ; sans clé (bancs de test), la commande part telle quelle."""
        return crypto.sign_command(self._signer, command) if self._signer is not None else command

    def publish_public_key(self, topic: str) -> bool:
        """Clé publique de l'agent retenue sur le broker local : la passerelle de la batterie vérifie les commandes avec elle."""
        if self._signer is None:
            return False
        public = self._signer.public_key()
        body = {
            "algorithm": getattr(self._signer, "algorithm", "Ed25519"),
            "public_key": public,
            "key_id": crypto.key_id(public),
            "scheme": crypto.COMMAND_SCHEME,
        }
        try:
            return bool(self._publisher.publish(topic, json.dumps(body), 1, retain=True))
        except TypeError:  # publieur sans message retenu
            return bool(self._publisher.publish(topic, json.dumps(body), 1))

    def _site(self) -> str:
        ident = self._identity()
        return str(ident.site_id) if ident else "?"

    def _closing(self, stock_id: int, now: dt.datetime) -> dict | None:
        """Décharge en cours remplacée par la commande publiée à ``now`` : fin (au plus tard la fin de sa durée) et énergie déchargée
        mesurée (relevés ``power_discharge_w`` de la batterie, chacun tenu jusqu'au suivant, bornés à la puissance de l'ordre), si
        les relevés couvrent 80 % de la durée écoulée — sinon ``energy_wh`` nul et le cloud retient la durée écoulée (WP3b-4)."""
        last = self.last_execution(stock_id)
        if last is None or last["action"] != rules.DISCHARGE:
            return None
        start = last["executed_at"]
        end = min(now, start + dt.timedelta(seconds=self._duration(last)))
        elapsed = (end - start).total_seconds()
        order_power = float(last["power_w"] or 0)
        with self._db.tx() as cur:
            cur.execute(
                "SELECT value, measurement_timestamp AS at FROM local_measurements WHERE device_id = %s AND data_type = 'power_discharge_w'"
                " AND measurement_timestamp >= %s AND measurement_timestamp < %s ORDER BY measurement_timestamp",
                (f"stock:{stock_id}", start, end),
            )
            samples = cur.fetchall()
        energy = None
        coverage = 0.0
        if samples and elapsed > 0:
            wh = 0.0
            for i, s in enumerate(samples):
                until = samples[i + 1]["at"] if i + 1 < len(samples) else end
                wh += min(max(0.0, float(s["value"])), order_power) * (until - s["at"]).total_seconds() / 3600.0
            coverage = (end - samples[0]["at"]).total_seconds() / elapsed
            energy = round(wh, 3) if coverage >= MIN_MEASURED_COVERAGE else None
        return {
            "decision_id": last["decision_ref"],
            "source": last["source"],
            "action": rules.DISCHARGE,
            "started_at": start.isoformat().replace("+00:00", "Z"),
            "ended_at": end.isoformat().replace("+00:00", "Z"),
            "energy_wh": energy,
            "coverage": round(min(1.0, coverage), 3),
        }

    def _record(
        self,
        stock_id: int,
        decision_ref: str | None,
        source: str,
        action: str,
        power_w: float,
        soc: float | None,
        status: str,
        rule: str | None,
        reason: str | None,
        command_id: str | None,
        now: dt.datetime,
        duration_s: int | None = None,
        closes: dict | None = None,
    ) -> int:
        with self._db.tx() as cur:
            cur.execute(
                "INSERT INTO local_decisions (site_id, action, power_w, soc_before, source, execution_status, rejection_reason, command_id,"
                " executed_at, decision_ref, stock_id, rejection_rule, created_at, duration_s)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING decision_id",
                (
                    self._site(),
                    action,
                    power_w,
                    soc,
                    source,
                    status,
                    reason,
                    command_id,
                    now,
                    decision_ref,
                    stock_id,
                    rule,
                    now,
                    duration_s,
                ),
            )
            row_id = int(cur.fetchone()["decision_id"])
        report = {
            "decision_id": decision_ref,
            "stock_id": stock_id,
            "action": action,
            "power_w": round(power_w, 2),
            "source": source,
            "execution_status": status,
            "rejection_rule": rule,
            "rejection_reason": reason,
            "soc_before": soc,
            # C5 : sans la durée appliquée, le cloud ne peut pas valoriser une décision prise en local (§5.4).
            "duration_s": duration_s,
        }
        if closes is not None:
            report["closes"] = closes
        self._buffer.push("DECISION_REPORT", report, now, now)
        return row_id

    def purge_cache(self, now: dt.datetime) -> int:
        """Ordres de plus de 7 jours supprimés du cache (CA-18.15), puis cache plafonné aux 100 dernières décisions reçues par site
        (§2.1, E184) — jamais l'ordre en vigueur d'une batterie (le plus récent par date d'effet), même reçu avant les 100 autres."""
        with self._db.tx() as cur:
            cur.execute(
                "DELETE FROM decision_cache WHERE expires_at < %s OR received_at < %s",
                (now - dt.timedelta(days=7), now - dt.timedelta(days=7)),
            )
            removed = cur.rowcount
            cur.execute(
                "DELETE FROM decision_cache WHERE cache_id IN (SELECT cache_id FROM (SELECT cache_id, row_number() OVER (PARTITION BY site_id"
                " ORDER BY received_at DESC, cache_id DESC) AS rang FROM decision_cache) r WHERE r.rang > %s)"
                " AND cache_id NOT IN (SELECT DISTINCT ON (stock_id) cache_id FROM decision_cache WHERE coalesce(valid_from, received_at) <= %s"
                " ORDER BY stock_id, coalesce(valid_from, received_at) DESC, cache_id DESC)",
                (CACHE_MAX, now),
            )
            return removed + cur.rowcount

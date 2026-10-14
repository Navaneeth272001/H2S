"""Mesures instantanées du site vers le cloud (spéc. 3 CA-3.10 « mise à jour < 10 s » ; CDC §3.2 « l'Edge Box collecte et publie sur
MQTT » ; E027) — point d'accroche du canal temps réel du lot 5.

La synchronisation du buffer remonte des moyennes de 5 minutes (historique, preuves) : un site réel paraît vieux de 7 à 13 minutes
au tableau de bord. Toutes les 5 secondes, ``LivePublisher`` compose l'instantané du site depuis les relevés bruts les plus récents
(moins de 10 secondes) : puissance par équipement et par rôle (production, consommation), SoC des batteries, date de la plus ancienne
valeur retenue. Il ne l'enregistre nulle part : il le confie à un ``LiveTransport``.

Le transport est celui du lot 5 (MQTT EMQX, QoS 1, TLS 1.3, D314) ; aucun n'est livré ici, donc la tâche n'est pas planifiée
(``Agent.live`` sans transport) — rien n'est présenté comme du temps réel tant que le canal n'existe pas. Côté cloud, l'instantané
reçu est poussé sur le flux du site sans écriture en base (``LiveUpdates.live``).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Protocol

from .storage import Database

INTERVAL_S = 5
MAX_AGE = dt.timedelta(seconds=10)


class LiveTransport(Protocol):
    def publish_live(self, payload: dict) -> bool: ...


class LivePublisher:
    def __init__(
        self,
        db: Database,
        site: Callable[[], int | None],
        devices: Callable[[], list[tuple[str, str]]],
        stocks: Callable[[], list[int]],
        now: Callable[[], dt.datetime],
        transport: LiveTransport | None = None,
    ) -> None:
        self._db = db
        self._site = site
        self._devices = devices
        self._stocks = stocks
        self._now = now
        self.transport = transport

    def snapshot(self, now: dt.datetime | None = None) -> dict | None:
        """Instantané du site, ou ``None`` si aucun relevé de moins de 10 secondes (rien de périmé n'est présenté comme instantané)."""
        now = now or self._now()
        site = self._site()
        devices = self._devices()
        keys = [k for k, _ in devices] + [f"stock:{s}" for s in self._stocks()]
        if site is None or not keys:
            return None
        with self._db.tx() as cur:
            cur.execute(
                "SELECT DISTINCT ON (device_id, data_type) device_id, data_type, value, measurement_timestamp AS at FROM local_measurements"
                " WHERE device_id = ANY(%s) AND data_type IN ('power_w', 'soc_pct') AND measurement_timestamp > %s AND measurement_timestamp <= %s"
                " ORDER BY device_id, data_type, measurement_timestamp DESC",
                (keys, now - MAX_AGE, now + dt.timedelta(seconds=5)),
            )
            rows = cur.fetchall()
        if not rows:
            return None
        roles = dict(devices)
        production = consumption = None
        per_device, socs = [], {}
        # Équipements dont la puissance a été retenue dans la fenêtre, par rôle : c'est la COMPLÉTUDE de
        # la somme qui décide si on l'envoie, pas le fait d'avoir trouvé au moins une valeur.
        vus = {"PRODUCTION": set(), "CONSUMPTION": set()}
        for r in rows:
            if r["data_type"] == "soc_pct":
                socs[r["device_id"].split(":", 1)[1]] = round(float(r["value"]), 2)
                continue
            value = max(0.0, float(r["value"]))
            role = roles.get(r["device_id"], "CONSUMPTION")
            per_device.append(
                {
                    "device": r["device_id"],
                    "role": role,
                    "power_w": round(value, 1),
                }
            )
            vus.setdefault(role, set()).add(r["device_id"])
            if role == "PRODUCTION":
                production = (production or 0.0) + value
            else:
                consumption = (consumption or 0.0) + value

        # UNE SOMME PARTIELLE N'EST PAS UN TOTAL — défaut corrigé le 05/10/2026.
        #
        # Jusqu'ici, la somme portait sur les seuls équipements ayant publié dans les dix dernières
        # secondes, et elle était envoyée comme « consumption_w » du SITE. Avec huit prises qui ne
        # publient pas en même temps, le sous-ensemble frais est le plus souvent minoritaire : le cloud
        # recevait 0 W de consommation pendant que son propre calcul, fondé sur les moyennes de cinq
        # minutes de TOUS les équipements, en voyait 4,3 W. Mesuré côté cloud : le flux poussait
        # « consumption = 0.0 » et le REST rendait « 4.34 » au même instant, douze fois de suite.
        #
        # À l'écran, l'effet est exactement celui que le client a décrit : les tuiles affichent la bonne
        # valeur au chargement, puis le canal temps réel les écrase avec un zéro cinq secondes plus tard.
        # Recharger la page les ramène — puisque le rechargement passe par le REST.
        #
        # La règle est donc : on n'envoie le total d'un rôle que si TOUS ses équipements ont une valeur
        # fraîche. Sinon on envoie « None », c'est-à-dire « je ne sais pas » — et le cloud conserve alors
        # sa propre valeur calculée (LiveUpdates.override ignore les nuls). Mieux vaut pas de chiffre
        # qu'un chiffre faux : un zéro se lit comme une mesure, une absence se lit comme une absence.
        attendus = {"PRODUCTION": set(), "CONSUMPTION": set()}
        for cle, role in devices:
            attendus.setdefault(role, set()).add(cle)
        if attendus["PRODUCTION"] and vus["PRODUCTION"] != attendus["PRODUCTION"]:
            production = None
        if attendus["CONSUMPTION"] and vus["CONSUMPTION"] != attendus["CONSUMPTION"]:
            consumption = None
        # Plus aucune puissance exploitable et aucun SoC : il n'y a rien à présenter comme instantané.
        if production is None and consumption is None and not socs:
            return None
        measured_at = min(r["at"] for r in rows)
        return {
            "v": 1,
            "type": "live",
            "site_id": site,
            "measured_at": measured_at.isoformat().replace("+00:00", "Z"),
            "production_w": None if production is None else round(production, 1),
            "consumption_w": None if consumption is None else round(consumption, 1),
            "soc": socs,
            "devices": per_device,
        }

    def tick(self) -> bool:
        if self.transport is None:
            return False
        payload = self.snapshot()
        return payload is not None and bool(self.transport.publish_live(payload))

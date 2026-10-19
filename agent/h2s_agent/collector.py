"""Collecte (F18.1, F18.2, CA-18.1 à CA-18.5, CA-18.8, D251) : messages MQTT → relevés validés → base locale → buffer.

* Batterie (mesure critique) : chaque relevé part tel quel dans le buffer, priorité 2.
* Équipements et météo (mesures standard) : agrégés par fenêtres closes de 5 minutes (moyennes ; énergie de la fenêtre
  tirée du compteur cumulé), priorité 3 — 7 jours de coupure tiennent dans le plafond du buffer (D251).
* Agrégats horaires conservés localement pour l'apprentissage fédéré (``local_aggregated``, période ``1h``).
* Équipement en production (E181) : l'énergie de la fenêtre d'un compteur qui a un index d'injection (Linky, EAIT) est tirée de
  cet index, pas de l'énergie soutirée ; le cloud écrit ces relevés en production de la source de l'équipement.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections.abc import Callable

from . import energy_calculator, parsers
from .buffer import Buffer
from .config import AgentConfig
from .storage import Database

LOG = logging.getLogger("h2s_agent.collector")
WINDOW = dt.timedelta(minutes=5)
GRACE = dt.timedelta(minutes=2)
COUNTER_LOOKBACK = dt.timedelta(hours=1)

# Contrat de ``POST /api/agent/sync/buffer`` pour ``source: battery``.  Les
# noms internes/anciens (``soc``, ``charge_power_w``…) sont normalisés par
# ``parsers.h2s_battery`` avant d'arriver ici ; ne jamais les laisser sortir
# dans le paquet signé envoyé au backend.
BATTERY_VALUE_KEYS = (
    "soc_pct",
    "power_charge_w",
    "power_discharge_w",
    "voltage_v",
    "current_a",
    "temperature_c",
)


def floor(at: dt.datetime, step: dt.timedelta) -> dt.datetime:
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    return epoch + ((at - epoch) // step) * step


class Collector:
    def __init__(
        self,
        db: Database,
        buffer: Buffer,
        config: AgentConfig,
        now: Callable[[], dt.datetime],
        clock_synced: Callable[[], bool],
        site: Callable[[], str],
        role_of: Callable[[int], str] | None = None,
    ) -> None:
        self._db = db
        # Rôle énergétique d'un équipement (E181) : celui du cloud s'il est connu, sinon celui d'agent.json.
        defaults = {d.device_id: d.role for d in config.devices}
        self._role_of = role_of or (lambda device_id: defaults.get(device_id, "CONSUMPTION"))
        self._buffer = buffer
        self._config = config
        # Sources de production déclarées du site : elles alimentent le calcul théorique de §4.1.4. La
        # liste peut être vide — un site sans source déclarée envoie alors trois zéros, ce qui est la
        # bonne réponse et non une absence de réponse.
        self._sources = tuple(config.sources)
        self._now = now
        self._synced = clock_synced
        self._site = site
        self.received = 0
        self.rejected = 0
        self.last_parse_ms: int | None = None
        self._lock = threading.Lock()
        self._routes: dict[str, Callable[[bytes, dt.datetime], list[parsers.Reading]]] = {}
        for d in config.devices:
            for topic in d.topics():
                # Le parseur dépend du SUJET, pas seulement du type d'équipement : une prise Shelly publie
                # son statut sur « /status/<composant> » et ses notifications sur « /events/rpc », et les
                # deux charges n'ont pas la même forme — la seconde emboîte le composant sous « params ».
                if d.kind != "shelly_gen2":
                    parse = parsers.h2s_device
                elif topic.endswith("/events/rpc"):
                    parse = parsers.shelly_gen2_rpc
                else:
                    parse = parsers.shelly_gen2
                self._routes[topic] = self._bind(parse, d.device_id)
        for b in config.batteries:
            self._routes[b.status_topic] = self._bind(parsers.h2s_battery, b.stock_id)
        if config.weather_topic:
            self._routes[config.weather_topic] = self._bind(parsers.h2s_weather)

    def _bind(self, fn, *lead):
        """Gel des arguments de tête (device_id, stock_id) : chaque route appelle fn(lead…, payload, at, synced)."""
        return lambda payload, at: fn(*lead, payload, at, self._synced())

    def topics(self) -> list[str]:
        return sorted(self._routes)

    def on_message(self, topic: str, payload: bytes) -> int:
        """Traite un message ; renvoie le nombre de relevés écrits. Ne lève jamais : un message faux est compté et écarté."""
        started = time.monotonic()
        route = self._routes.get(topic)
        if route is None:
            return 0
        received = self._now()
        try:
            readings = route(payload, received)
        except parsers.ParseError as exc:
            with self._lock:
                self.rejected += 1
            LOG.warning("message écarté sur %s : %s", topic, exc)
            return 0
        written = self.store(readings, received)
        with self._lock:
            self.received += 1
            self.last_parse_ms = int((time.monotonic() - started) * 1000)
        return written

    def store(self, readings: list[parsers.Reading], received: dt.datetime) -> int:
        written = 0
        with self._db.tx() as cur:
            for r in readings:
                cur.execute(
                    "INSERT INTO local_measurements (site_id, device_id, device_type, data_type, value, unit, measurement_timestamp,"
                    " received_timestamp, data_quality, source) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'MQTT')"
                    " ON CONFLICT ON CONSTRAINT unique_local_measurement DO NOTHING",
                    (
                        self._site(),
                        r.device_key,
                        r.source,
                        r.data_type,
                        r.value,
                        r.unit,
                        r.at,
                        received,
                        r.quality,
                    ),
                )
                written += cur.rowcount
        battery = [r for r in readings if r.source == "battery"]
        if battery and written:
            # Le backend batterie est volontairement strict : seulement ces
            # six grandeurs sont admises dans ``values``.  Ne pas envoyer la
            # qualité ou l'agrégation, qui sont des métadonnées locales et ne
            # font pas partie de son DTO MeasurementPayload.
            by_type = {r.data_type: r.value for r in battery}
            values = {key: by_type[key] for key in BATTERY_VALUE_KEYS if key in by_type}
            self._buffer.push(
                "MEASUREMENT",
                {
                    "source": "battery",
                    "stock_id": battery[0].ref,
                    "values": values,
                },
                battery[0].at,
                received,
                priority=2,
            )
        return written

    def aggregate(self, now: dt.datetime | None = None) -> int:
        """Fenêtres de 5 minutes closes depuis 2 minutes et non encore agrégées → buffer, puis ``local_aggregated`` ; agrégats horaires.

        * Grâce de 2 minutes : un Shelly date ses relevés de la minute écoulée (``minute_ts``), ils arrivent après la fin de la
          fenêtre qu'ils concernent (relecture B18-2).
        * Énergie de la fenêtre = somme des accroissements du compteur cumulé depuis le dernier relevé **antérieur** à la
          fenêtre ; un compteur remis à zéro repart de sa nouvelle valeur. Rien ne se perd entre deux fenêtres.
        * Buffer d'abord, marque « agrégée » ensuite : un arrêt entre les deux renvoie la fenêtre, que le cloud n'écrit
          qu'une fois (une mesure par équipement et par instant, D258) ; l'inverse la perdrait.
        """
        now = now or self._now()
        end = floor(now - GRACE, WINDOW)
        pushed = 0
        with self._db.tx() as cur:
            cur.execute("SELECT max(period_end) AS last FROM local_aggregated WHERE aggregation_period = '5min'")
            last = cur.fetchone()["last"]
            start = last if last is not None else end - dt.timedelta(hours=1)
            if start >= end:
                return 0
            cur.execute(
                "SELECT device_id, device_type, data_type, date_bin('5 minutes', measurement_timestamp, TIMESTAMPTZ '1970-01-01') AS w,"
                " avg(value) AS avg, min(value) AS min, max(value) AS max, count(*) AS n, bool_or(data_quality <> 'OK') AS estimated"
                " FROM local_measurements WHERE device_type IN ('device', 'weather') AND measurement_timestamp >= %s AND measurement_timestamp < %s"
                " GROUP BY 1, 2, 3, 4 ORDER BY 4, 1, 3",
                (start, end),
            )
            rows = cur.fetchall()
            cur.execute(
                "SELECT device_id, data_type, measurement_timestamp AS at, value FROM local_measurements WHERE device_type = 'device'"
                " AND data_type IN ('energy_total_wh', 'energy_injected_total_wh') AND measurement_timestamp >= %s AND measurement_timestamp < %s"
                " ORDER BY device_id, measurement_timestamp",
                (start - COUNTER_LOOKBACK, end),
            )
            all_counters = cur.fetchall()
        injected = {r["device_id"] for r in all_counters if r["data_type"] == "energy_injected_total_wh"}

        def counter_of(device_key: str) -> str:
            production = device_key in injected and self._role_of(int(device_key.split(":", 1)[1])) == "PRODUCTION"
            return "energy_injected_total_wh" if production else "energy_total_wh"

        counters = [r for r in all_counters if r["data_type"] == counter_of(r["device_id"])]
        energy: dict[tuple[str, dt.datetime], float] = {}
        previous: dict[str, float] = {}
        for r in counters:
            value = float(r["value"])
            before = previous.get(r["device_id"])
            if before is not None and r["at"] >= start:
                key = (r["device_id"], floor(r["at"], WINDOW))
                energy[key] = energy.get(key, 0.0) + (value - before if value >= before else value)
            previous[r["device_id"]] = value
        windows: dict[tuple, dict] = {}
        for r in rows:
            key = (r["device_id"], r["device_type"], r["w"])
            entry = windows.setdefault(key, {"values": {}, "estimated": False})
            entry["estimated"] = entry["estimated"] or r["estimated"]
            if r["data_type"] in ("energy_total_wh", "energy_injected_total_wh"):
                if r["data_type"] == counter_of(r["device_id"]) and (r["device_id"], r["w"]) in energy:
                    entry["values"]["energy_wh"] = round(energy[(r["device_id"], r["w"])], 3)
            elif r["data_type"] in (
                "power_w",
                "current_a",
                "voltage_v",
                "energy_wh",
                "power_factor",
                "temperature_c",
                "irradiance_w_m2",
                "wind_speed_m_s",
                "humidity_pct",
                "rain_mm",
            ):
                entry["values"][r["data_type"]] = round(float(r["avg"]), 3)
        for (device_key, device_type, w), entry in windows.items():
            values = entry["values"]
            if not values:
                continue
            if device_type == "weather":
                # CALCUL DE LA PRODUCTION ENR THÉORIQUE (brique 18, §4.1.4 — spécification du client du
                # 04/10/2026). C'est l'étape « prétraitement pour l'envoi au cloud » : on joint au relevé
                # météo ce que les sources déclarées du site produiraient dans ces conditions, plutôt que
                # d'envoyer l'irradiance et le vent bruts et de laisser le cloud refaire le calcul.
                #
                # Trois raisons, et elles sont dans la spécification : moins de bande passante, moins de
                # charge au cloud, et un boîtier qui sait encore estimer sa production réseau coupé.
                #
                # CE N'EST PAS UNE MESURE. Ces trois nombres voyagent avec le relevé météo, jamais à la
                # place d'une mesure de compteur : c'est au modèle de confronter le théorique au constaté.
                values.update(
                    energy_calculator.production_totale(
                        [
                            energy_calculator.SourceTheorique(
                                type=src.type,
                                surface_m2=src.surface_m2,
                                rendement_pv=src.rendement_pv,
                                diametre_rotor_m=src.diametre_rotor_m,
                                rendement_eolien=src.rendement_eolien,
                                vent_demarrage_m_s=src.vent_demarrage_m_s,
                                vent_arret_m_s=src.vent_arret_m_s,
                                puissance_nominale_w=src.puissance_nominale_w,
                            )
                            for src in self._sources
                        ],
                        irradiance_w_m2=values.get("irradiance_w_m2"),
                        vitesse_vent_m_s=values.get("wind_speed_m_s"),
                    )
                )
            payload = {
                "source": "weather" if device_type == "weather" else "device",
                "values": values,
                "quality": "ESTIMATED" if entry["estimated"] else "OK",
                "aggregation": "AVG_5MIN",
            }
            if device_type == "device":
                if "power_w" not in values and "energy_wh" not in values:
                    continue
                payload["device_id"] = int(device_key.split(":", 1)[1])
            self._buffer.push("MEASUREMENT", payload, w, now, priority=3)
            pushed += 1
        with self._db.tx() as cur:
            for r in rows:
                cur.execute(
                    "INSERT INTO local_aggregated (site_id, device_id, data_type, avg_value, min_value, max_value, count, aggregation_period,"
                    " period_start, period_end) VALUES (%s, %s, %s, %s, %s, %s, %s, '5min', %s, %s)",
                    (
                        self._site(),
                        r["device_id"],
                        r["data_type"],
                        r["avg"],
                        r["min"],
                        r["max"],
                        r["n"],
                        r["w"],
                        r["w"] + WINDOW,
                    ),
                )
            # Marque de fin : la plage entière est traitée, même sans mesure, pour ne pas la relire à chaque passage.
            cur.execute(
                "INSERT INTO local_aggregated (site_id, device_id, data_type, count, aggregation_period, period_start, period_end)"
                " VALUES (%s, '-', '-', 0, '5min', %s, %s)",
                (self._site(), start, end),
            )
        self.hourly(now)
        return pushed

    def hourly(self, now: dt.datetime) -> None:
        end = floor(now, dt.timedelta(hours=1))
        with self._db.tx() as cur:
            cur.execute("SELECT max(period_end) AS last FROM local_aggregated WHERE aggregation_period = '1h'")
            last = cur.fetchone()["last"]
            start = last if last is not None else end - dt.timedelta(days=8)
            if start >= end:
                return
            cur.execute(
                "INSERT INTO local_aggregated (site_id, device_id, data_type, avg_value, min_value, max_value, count, aggregation_period,"
                " period_start, period_end) SELECT %s, device_id, data_type, avg(value), min(value), max(value), count(*), '1h',"
                " date_trunc('hour', measurement_timestamp), date_trunc('hour', measurement_timestamp) + interval '1 hour'"
                " FROM local_measurements WHERE data_type = 'power_w' AND device_type = 'device' AND measurement_timestamp >= %s"
                " AND measurement_timestamp < %s GROUP BY 2, 3, 9, 10",
                (self._site(), start, end),
            )
            cur.execute(
                "INSERT INTO local_aggregated (site_id, device_id, data_type, count, aggregation_period, period_start, period_end)"
                " VALUES (%s, '-', '-', 0, '1h', %s, %s)",
                (self._site(), end - dt.timedelta(hours=1), end),
            )

    def purge(self, now: dt.datetime, retention_days: int) -> int:
        with self._db.tx() as cur:
            cur.execute(
                "DELETE FROM local_measurements WHERE measurement_timestamp < %s",
                (now - dt.timedelta(days=retention_days),),
            )
            n = cur.rowcount
            cur.execute(
                "DELETE FROM local_aggregated WHERE period_end < %s",
                (now - dt.timedelta(days=retention_days),),
            )
            return n + cur.rowcount

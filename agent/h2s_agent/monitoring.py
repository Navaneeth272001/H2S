"""Surveillance locale et alertes (§4.7.4, CA-18.38, CA-18.39).

Métriques système lues dans /proc (sans dépendance), stockées dans ``local_metrics`` et remontées au cloud (priorité 4).
Alertes : liste fermée de codes, sévérité, une par code et par heure au plus ; publiées sur ``<préfixe>/agent/errors`` en
local et remontées au cloud (priorité 2).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import shutil
import threading
from collections.abc import Callable

from .buffer import Buffer
from .storage import Database

LOG = logging.getLogger("h2s_agent.monitoring")

ALERTS = {
    "CLOUD_UNREACHABLE": "Cloud injoignable : mode dégradé, décisions locales selon la règle du site",
    "SAFE_STANDBY": "Batterie mise en STANDBY de sécurité",
    "DECISION_REJECTED_LOCALLY": "Ordre du cloud refusé par les règles SEC1-6 locales",
    "DECISION_SIGNATURE_INVALID": "Ordre du cloud à la signature invalide écarté",
    "BUFFER_NEAR_FULL": "Buffer au-delà de 80 % de son plafond",
    "DISK_HIGH": "Carte SD au-delà de 90 % d'occupation",
    "CPU_HIGH": "Processeur au-delà de 90 % sur la dernière minute",
    "DEVICE_SILENT": "Équipement hors ligne",
    "CLOCK_UNSYNCED": "Horloge non synchronisée : mesures marquées estimées",
    "INTEGRITY_FAILED": "Intégrité du code de l'agent non vérifiée",
    "BACKUP_FAILED": "Sauvegarde locale quotidienne en échec",
    "AGENT_ERROR": "Erreur interne de l'agent, reprise automatique",
    "MODEL_REJECTED": "Modèle global à la signature ou à l'empreinte invalide écarté",
    "MANUAL_MODE": "Mode manuel du technicien : ordres du cloud suspendus (une heure au plus)",
    "BATTERY_WATCHDOG": "Seuil de sécurité franchi pendant un ordre : batterie mise en veille",
    "SPOT_PRICE_UNAVAILABLE": "Prix spot indisponible : charge locale suspendue en mode dégradé",
    "COMMANDS_STALE": "Commandes d'équipement non relevées : l'agent répond mais ne traite plus les commandes",
    "SYNC_REJECTED": "Enregistrements refusés par le cloud à la synchronisation",
}


class Alerts:
    def __init__(
        self,
        buffer: Buffer,
        now: Callable[[], dt.datetime],
        publish: Callable[[str, str], None] | None = None,
    ) -> None:
        self._buffer = buffer
        self._now = now
        self._publish = publish or (lambda topic, payload: None)
        self._last: dict[str, dt.datetime] = {}
        self._lock = threading.Lock()
        self.errors = 0

    def raise_(
        self,
        code: str,
        severity: str,
        message: str,
        key: str | None = None,
        details: dict | None = None,
    ) -> bool:
        """Alerte une fois par heure par code, ou par code et par objet quand ``key`` est donné (un équipement muet n'en masque
        pas un autre, E033) ; ``details`` (identifiants, jamais de secret) accompagne l'alerte jusqu'au cloud."""
        if code not in ALERTS:
            code = "AGENT_ERROR"
        now = self._now()
        slot = code if key is None else f"{code}:{key}"
        with self._lock:
            if severity in ("WARNING", "CRITICAL"):
                self.errors += 1
            last = self._last.get(slot)
            if last is not None and now - last < dt.timedelta(hours=1):
                return False
            self._last[slot] = now
        text = f"{ALERTS[code]} — {message}"[:300]
        LOG.warning("alerte %s (%s) : %s", code, severity, text)
        payload = {
            **(details or {}),
            "code": code,
            "severity": severity,
            "message": text,
        }
        self._buffer.push("ALERT", payload, now, now)
        try:
            self._publish("agent/errors", f'{{"code":"{code}","severity":"{severity}"}}')
        except Exception:  # noqa: BLE001 - le broker local absent ne doit pas perdre l'alerte, déjà dans le buffer
            LOG.debug("alerte non publiée en local")
        return True


class Metrics:
    def __init__(
        self,
        db: Database,
        buffer: Buffer,
        data_dir: str,
        now: Callable[[], dt.datetime],
        site: Callable[[], str],
    ) -> None:
        self._db = db
        self._buffer = buffer
        self._dir = data_dir
        self._now = now
        self._site = site
        self._cpu_prev: tuple[int, int] | None = None

    def cpu_pct(self) -> float | None:
        try:
            with open("/proc/stat", encoding="ascii") as f:
                parts = [int(x) for x in f.readline().split()[1:]]
        except (OSError, ValueError):
            return None
        idle, total = parts[3] + (parts[4] if len(parts) > 4 else 0), sum(parts)
        prev, self._cpu_prev = self._cpu_prev, (idle, total)
        if prev is None or total == prev[1]:
            return None
        return round(100.0 * (1 - (idle - prev[0]) / (total - prev[1])), 2)

    @staticmethod
    def memory_pct() -> float | None:
        try:
            info = {}
            with open("/proc/meminfo", encoding="ascii") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    info[k] = int(v.strip().split()[0])
            return round(100.0 * (1 - info["MemAvailable"] / info["MemTotal"]), 2)
        except (OSError, KeyError, ValueError, ZeroDivisionError):
            return None

    def disk_pct(self) -> float | None:
        try:
            os.makedirs(self._dir, exist_ok=True)
            u = shutil.disk_usage(self._dir)
            return round(100.0 * u.used / u.total, 2)
        except OSError:
            return None

    def collect(
        self,
        latency_ms: int | None,
        buffer_size: int,
        decisions_last_hour: int,
        errors: int,
    ) -> dict:
        now = self._now()
        values = {
            "cpu_pct": self.cpu_pct(),
            "memory_pct": self.memory_pct(),
            "disk_pct": self.disk_pct(),
            "latency_ms": latency_ms,
            "buffer_size": buffer_size,
            "decision_count": decisions_last_hour,
            "error_count": errors,
        }
        units = {
            "cpu_pct": "%",
            "memory_pct": "%",
            "disk_pct": "%",
            "latency_ms": "ms",
            "buffer_size": "",
            "decision_count": "",
            "error_count": "",
        }
        with self._db.tx() as cur:
            for k, v in values.items():
                if v is not None:
                    cur.execute(
                        "INSERT INTO local_metrics (site_id, metric_type, value, unit, collected_at) VALUES (%s, %s, %s, %s, %s)",
                        (self._site(), k, float(v), units[k], now),
                    )
        self._buffer.push("METRIC", {k: v for k, v in values.items() if v is not None}, now, now)
        return values

    def purge(self, now: dt.datetime, days: int = 30) -> int:
        with self._db.tx() as cur:
            cur.execute(
                "DELETE FROM local_metrics WHERE collected_at < %s",
                (now - dt.timedelta(days=days),),
            )
            return cur.rowcount

"""Horloge injectable : l'agent ne lit jamais l'heure système directement (tests, décalage mesuré avec le cloud)."""

from __future__ import annotations

import datetime as dt
import threading


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Clock:
    """Heure UTC de l'agent, corrigée d'un décalage estimé sur les réponses authentifiées du cloud (D256)."""

    def __init__(self) -> None:
        self._offset = dt.timedelta(0)
        self._synced = False
        self._lock = threading.Lock()

    def now(self) -> dt.datetime:
        with self._lock:
            return utcnow() + self._offset

    @property
    def synced(self) -> bool:
        return self._synced

    def observe_server_time(self, server: dt.datetime, sent_at: dt.datetime, received_at: dt.datetime) -> None:
        """Décalage = heure du serveur - milieu de l'aller-retour. Sous 2 secondes, l'heure système est juste : le décalage
        retombe à zéro (une correction NTP ultérieure n'est pas doublée par un décalage périmé, relecture B18-7)."""
        middle = sent_at + (received_at - sent_at) / 2
        # L'aller-retour mesure l'heure système ; le décalage appliqué jusqu'ici n'y entre pas.
        offset = server - middle
        with self._lock:
            self._offset = offset if abs(offset.total_seconds()) > 2 else dt.timedelta(0)
            self._synced = True


class FixedClock(Clock):
    """Horloge de test, avancée à la main."""

    def __init__(self, start: dt.datetime) -> None:
        super().__init__()
        self._now = start

    def now(self) -> dt.datetime:
        return self._now

    def advance(self, **kwargs: float) -> dt.datetime:
        self._now = self._now + dt.timedelta(**kwargs)
        return self._now

    def set(self, value: dt.datetime) -> None:
        self._now = value


def parse_instant(text: str) -> dt.datetime:
    """Instant ISO-8601 du cloud (Java ``Instant`` : jusqu'à 9 décimales, suffixe Z) en datetime UTC."""
    import re

    t = text.strip().replace("Z", "+00:00")
    m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})(\.\d+)?([+-]\d{2}:\d{2})$", t)
    if not m:
        raise ValueError(f"instant illisible : {text!r}")
    frac = (m.group(2) or "")[:7]
    return dt.datetime.fromisoformat(m.group(1) + frac + m.group(3)).astimezone(dt.timezone.utc)

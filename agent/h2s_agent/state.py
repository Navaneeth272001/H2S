"""Machine à états de l'agent (§4.7.3, CA-18.36, CA-18.37, D252).

INIT → RUNNING | DEGRADED ; RUNNING ⇄ DEGRADED ; RUNNING → SYNC → RUNNING | DEGRADED ; * → ERROR → INIT ; * → SHUTDOWN.
Chaque transition ferme l'état précédent dans ``agent_status_history`` et en ouvre un nouveau. La perte de réseau est
constatée après 3 échecs consécutifs d'appel au cloud, le retour après 2 succès (hystérésis contre l'oscillation).
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from collections.abc import Callable

from .storage import Database

INIT, RUNNING, DEGRADED, SYNC, ERROR, SHUTDOWN = (
    "INIT",
    "RUNNING",
    "DEGRADED",
    "SYNC",
    "ERROR",
    "SHUTDOWN",
)

TRANSITIONS: dict[str, set[str]] = {
    INIT: {RUNNING, DEGRADED, ERROR, SHUTDOWN},
    RUNNING: {DEGRADED, SYNC, ERROR, SHUTDOWN},
    DEGRADED: {RUNNING, ERROR, SHUTDOWN},
    SYNC: {RUNNING, DEGRADED, ERROR, SHUTDOWN},
    ERROR: {INIT, SHUTDOWN},
    SHUTDOWN: set(),
}

FAILURES_TO_DEGRADE = 3
SUCCESSES_TO_RECOVER = 2


class IllegalTransition(RuntimeError):
    pass


class StateMachine:
    def __init__(
        self,
        db: Database,
        site: str,
        now: Callable[[], dt.datetime],
        on_change: Callable[[str, str | None, str], None] | None = None,
    ) -> None:
        self._db = db
        self._site = site
        self._now = now
        self._on_change = on_change
        self._lock = threading.RLock()
        self.state = INIT
        self._failures = 0
        self._successes = 0

    def site(self, site: str) -> None:
        self._site = site

    def start(self, reason: str = "démarrage") -> None:
        with self._lock:
            now = self._now()
            with self._db.tx() as cur:
                cur.execute(
                    "UPDATE agent_status_history SET ended_at = %s WHERE ended_at IS NULL",
                    (now,),
                )
                cur.execute(
                    "INSERT INTO agent_status_history (site_id, status, previous_status, reason, started_at) VALUES (%s, %s, NULL, %s, %s)",
                    (self._site, INIT, reason, now),
                )
            self.state = INIT
            if self._on_change:
                self._on_change(INIT, None, reason)

    def to(self, target: str, reason: str, details: dict | None = None) -> bool:
        with self._lock:
            if target == self.state:
                return False
            if target not in TRANSITIONS[self.state]:
                raise IllegalTransition(f"{self.state} → {target} interdit")
            previous = self.state
            now = self._now()
            with self._db.tx() as cur:
                cur.execute(
                    "UPDATE agent_status_history SET ended_at = %s WHERE ended_at IS NULL",
                    (now,),
                )
                cur.execute(
                    "INSERT INTO agent_status_history (site_id, status, previous_status, reason, details, started_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        self._site,
                        target,
                        previous,
                        reason[:500],
                        json.dumps(details) if details else None,
                        now,
                    ),
                )
            self.state = target
            if target == RUNNING:
                self._failures = 0
            if self._on_change:
                self._on_change(target, previous, reason)
            return True

    @property
    def online(self) -> bool:
        return self.state in (RUNNING, SYNC, INIT)

    def cloud_success(self) -> None:
        with self._lock:
            self._failures = 0
            if self.state == DEGRADED:
                self._successes += 1
                if self._successes >= SUCCESSES_TO_RECOVER:
                    self._successes = 0
                    self.to(RUNNING, "réseau rétabli")
            elif self.state == INIT:
                self.to(RUNNING, "cloud joignable")

    def cloud_failure(self, why: str) -> None:
        with self._lock:
            self._successes = 0
            self._failures += 1
            if self.state in (RUNNING, SYNC, INIT) and self._failures >= FAILURES_TO_DEGRADE:
                self.to(
                    DEGRADED,
                    f"perte du cloud après {self._failures} échecs : {why}"[:200],
                )

    def time_in_state(self) -> dt.timedelta:
        with self._db.tx() as cur:
            cur.execute(
                "SELECT started_at FROM agent_status_history WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
            )
            row = cur.fetchone()
        return self._now() - row["started_at"] if row else dt.timedelta(0)

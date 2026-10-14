"""File de sortie persistante vers le cloud (F18.5, CA-18.21 à CA-18.26, D251, D252).

Tout ce qui remonte passe par ici, réseau présent ou non : écrit d'abord en base, envoyé ensuite, supprimé seulement
quand le cloud en a accusé réception (accepté, déjà reçu ou refusé avec motif).

* Ordre d'envoi : priorité croissante, puis ancienneté (FIFO) dans chaque priorité.
  1 = contributions fédérées et comptes rendus d'exécution ; 2 = mesures critiques (batterie), états et alertes ;
  3 = mesures standard (agrégats 5 min) ; 4 = métriques.
* Reprise : 5 s, 30 s, 2 min, 10 min, 30 min, puis toutes les 30 minutes, sans jamais abandonner une donnée non échue.
* Éviction : au-delà du plafond, la priorité la plus basse puis la plus ancienne ; rétention 30 jours sauf contributions.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass

from .storage import Database

BACKOFF_S = (5, 30, 120, 600, 1800)
PRIORITY = {
    "GRADIENT": 1,
    "DECISION_REPORT": 1,
    "LOCAL_PROOF": 1,
    "STATE": 2,
    "ALERT": 2,
    "METRIC": 4,
}
#: Jamais supprimés par la rétention ni par le plafond : contributions fédérées et preuves locales (E188).
KEPT = ("GRADIENT", "LOCAL_PROOF")


@dataclass(frozen=True)
class Item:
    buffer_id: int
    message_id: str
    kind: str
    at: dt.datetime
    payload: dict
    priority: int
    retry_count: int


def backoff(retry_count: int) -> dt.timedelta:
    return dt.timedelta(seconds=BACKOFF_S[min(max(retry_count, 1), len(BACKOFF_S)) - 1])


class Buffer:
    def __init__(self, db: Database, site: str, max_items: int = 50_000, retention_days: int = 30) -> None:
        self._db = db
        self._site = site
        self._max = max_items
        self._retention = dt.timedelta(days=retention_days)

    def site(self, site: str) -> None:
        self._site = site

    def push(
        self,
        kind: str,
        payload: dict,
        at: dt.datetime,
        now: dt.datetime,
        priority: int | None = None,
    ) -> str:
        with self._db.tx() as cur:
            return self.push_in(cur, kind, payload, at, now, priority)

    def push_in(
        self,
        cur,
        kind: str,
        payload: dict,
        at: dt.datetime,
        now: dt.datetime,
        priority: int | None = None,
    ) -> str:
        """Dans la transaction de l'appelant : l'enregistrement et ce qui le produit sont écrits ensemble ou pas du tout."""
        p = priority if priority is not None else PRIORITY.get(kind, 3)
        message_id = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO local_buffer (site_id, entity_type, payload, priority, message_id, event_at, next_attempt_at, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (self._site, kind, json.dumps(payload), p, message_id, at, now, now, now),
        )
        return message_id

    def size(self) -> int:
        with self._db.tx() as cur:
            cur.execute("SELECT count(*) AS n FROM local_buffer WHERE NOT synced")
            return int(cur.fetchone()["n"])

    def by_priority(self) -> dict[int, int]:
        with self._db.tx() as cur:
            cur.execute(
                "SELECT priority, count(*) AS n FROM local_buffer WHERE NOT synced GROUP BY priority ORDER BY priority"
            )
            return {int(r["priority"]): int(r["n"]) for r in cur.fetchall()}

    def due(self, now: dt.datetime, limit: int = 500, kinds: tuple[str, ...] | None = None) -> list[Item]:
        sql = (
            "SELECT buffer_id, message_id, entity_type, event_at, created_at, payload, priority, retry_count FROM local_buffer"
            " WHERE NOT synced AND next_attempt_at <= %s"
        )
        params: list = [now]
        if kinds:
            sql += " AND entity_type = ANY(%s)"
            params.append(list(kinds))
        sql += " ORDER BY priority, created_at, buffer_id LIMIT %s"
        params.append(limit)
        with self._db.tx() as cur:
            cur.execute(sql, params)
            return [
                Item(
                    r["buffer_id"],
                    str(r["message_id"]),
                    r["entity_type"],
                    r["event_at"] or r["created_at"],
                    r["payload"],
                    r["priority"],
                    r["retry_count"],
                )
                for r in cur.fetchall()
            ]

    def acknowledged(self, buffer_ids: list[int]) -> None:
        """Accusé du cloud (accepté, déjà reçu, refusé) : l'enregistrement quitte le buffer (§4.5.3 « succès → suppression »)."""
        if not buffer_ids:
            return
        with self._db.tx() as cur:
            cur.execute("DELETE FROM local_buffer WHERE buffer_id = ANY(%s)", (buffer_ids,))

    def failed(self, buffer_ids: list[int], error: str, now: dt.datetime) -> None:
        """Échec d'envoi : tentative comptée, prochaine date selon le barème ; jamais d'abandon d'une donnée non échue."""
        if not buffer_ids:
            return
        with self._db.tx() as cur:
            cur.execute(
                "UPDATE local_buffer SET retry_count = retry_count + 1, last_attempt = %s, error_message = %s, updated_at = %s,"
                " next_attempt_at = %s + (ARRAY[5, 30, 120, 600, 1800])[LEAST(retry_count + 1, 5)] * interval '1 second'"
                " WHERE buffer_id = ANY(%s)",
                (now, error[:500], now, now, buffer_ids),
            )

    def release(self, now: dt.datetime) -> int:
        """Retour du réseau : les enregistrements en attente de reprise repartent tout de suite (CA-18.22)."""
        with self._db.tx() as cur:
            cur.execute(
                "UPDATE local_buffer SET next_attempt_at = %s WHERE NOT synced AND next_attempt_at > %s",
                (now, now),
            )
            return cur.rowcount

    def evict(self, now: dt.datetime) -> int:
        """Rétention (sauf contributions) puis plafond : basse priorité et ancienneté d'abord (CA-18.25)."""
        removed = 0
        with self._db.tx() as cur:
            cur.execute(
                "DELETE FROM local_buffer WHERE entity_type <> ALL(%s) AND created_at < %s",
                (list(KEPT), now - self._retention),
            )
            removed += cur.rowcount
            cur.execute("SELECT count(*) AS n FROM local_buffer")
            excess = int(cur.fetchone()["n"]) - self._max
            if excess > 0:
                cur.execute(
                    "DELETE FROM local_buffer WHERE buffer_id IN (SELECT buffer_id FROM local_buffer WHERE entity_type <> ALL(%s)"
                    " ORDER BY priority DESC, created_at, buffer_id LIMIT %s)",
                    (list(KEPT), excess),
                )
                removed += cur.rowcount
        return removed

"""Base locale PostgreSQL de l'agent (D12, CA-18.6, CA-18.7) : connexion, migrations versionnées, transactions courtes."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.resources
import threading
from collections.abc import Iterator

import psycopg
from psycopg.rows import dict_row

#: Par ``importlib.resources`` : lu sur le disque, ou dans l'archive en mémoire d'un paquet chiffré (E177).
MIGRATIONS = importlib.resources.files(__package__) / "migrations"


class Database:
    """Une connexion par fil (psycopg n'est pas partageable entre fils sans verrou) ; chaque unité de travail est une transaction."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._local = threading.local()

    def _conn(self) -> psycopg.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None or conn.closed:
            conn = psycopg.connect(self._url, row_factory=dict_row, autocommit=False)
            self._local.conn = conn
        return conn

    @contextlib.contextmanager
    def tx(self) -> Iterator[psycopg.Cursor]:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                yield cur
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None and not conn.closed:
            conn.close()

    def ping(self) -> bool:
        try:
            with self.tx() as cur:
                cur.execute("SELECT 1")
                return True
        except psycopg.Error:
            return False


def migrate(db: Database) -> list[str]:
    """Applique les migrations absentes dans l'ordre ; refuse une migration appliquée dont le contenu a changé."""
    applied: list[str] = []
    with db.tx() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('h2s-agent-migrations'))")
        cur.execute(
            "CREATE TABLE IF NOT EXISTS agent_schema_history (version VARCHAR(50) PRIMARY KEY, checksum VARCHAR(64) NOT NULL,"
            " applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        )
        cur.execute("SELECT version, checksum FROM agent_schema_history")
        known = {r["version"]: r["checksum"] for r in cur.fetchall()}
        for file in sorted(
            (f for f in MIGRATIONS.iterdir() if f.name.startswith("E") and f.name.endswith(".sql")),
            key=lambda f: f.name,
        ):
            version = file.name.split("__")[0]
            sql = file.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            if version in known:
                if known[version] != checksum:
                    raise RuntimeError(
                        f"migration {file.name} modifiée après application : écrire une nouvelle migration"
                    )
                continue
            cur.execute(sql)
            cur.execute(
                "INSERT INTO agent_schema_history (version, checksum) VALUES (%s, %s)",
                (version, checksum),
            )
            applied.append(file.name)
    return applied

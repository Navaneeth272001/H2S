"""Sauvegarde locale quotidienne (CA-18.9) : ``pg_dump`` au format personnalisé, 7 copies gardées.

La copie reste sur la carte : elle protège d'une corruption de la base, pas de la perte du boîtier (les mesures remontent
au cloud par le buffer). Le répertoire doit être sur la partition chiffrée (guide de déploiement, D254).
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict, make_conninfo


def run(db_url: str, target_dir: Path, now: dt.datetime, keep: int = 7) -> Path:
    if shutil.which("pg_dump") is None:
        raise RuntimeError("pg_dump introuvable : installer le client PostgreSQL")
    target_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(target_dir, 0o700)
    target = target_dir / f"h2s-agent-{now.strftime('%Y%m%d-%H%M%S')}.dump"
    tmp = target.with_suffix(".tmp")
    # Le mot de passe éventuel passe par l'environnement du processus fils, jamais par sa ligne de commande (visible de ps).
    params = conninfo_to_dict(db_url)
    env = dict(os.environ)
    if params.get("password"):
        env["PGPASSWORD"] = str(params.pop("password"))
    result = subprocess.run(
        [
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--file",
            str(tmp),
            make_conninfo(**params),
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump en échec : {result.stderr.strip()[:200]}")
    tmp.rename(target)
    os.chmod(target, 0o600)
    dumps = sorted(target_dir.glob("h2s-agent-*.dump"))
    for old in dumps[:-keep]:
        old.unlink(missing_ok=True)
    return target

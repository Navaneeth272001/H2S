"""Publication du paquet chiffré de l'agent (E177) et de l'installation sans .py (E178), sur le poste de publication.

    # 1. chiffrer une version (source : h2s_agent en clair, ou le paquet compilé par tools/build_native.py)
    python tools/publish_package.py build --seed-file cle-publication.seed --source h2s_agent --out dist/1.1.0
    #    → dist/1.1.0/h2s_agent.enc (signé) et dist/1.1.0/h2s_agent.cek (clé AES, 0600, à garder hors ligne)

    # 2. pour chaque boîtier : sa clé de code (python -I -m h2s_boot --config … code-key, lue par le technicien)
    python tools/publish_package.py wrap --cek dist/1.1.0/h2s_agent.cek --code-public-key MIIBIjAN… --out package.key
    #    → package.key à déposer dans <data_dir>/keys/ du boîtier (inutile ailleurs)

    # 3. installation : répertoire app/ = h2s_boot/ + h2s_agent.enc, manifeste signé dessus
    python tools/sign_manifest.py --seed-file cle-publication.seed --root dist/1.1.0/app

La clé de publication signe l'en-tête du paquet (même clé que le manifeste, ``package_public_key``) ; la clé AES n'est jamais
sur un boîtier en clair : seule sa version chiffrée pour la clé de code de ce boîtier y est.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from h2s_agent import __version__
from h2s_boot import package

AGENT = Path(__file__).resolve().parents[1]


def _secret(path: Path) -> bytes:
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SystemExit(f"{path} doit être en 0600 (lisible du seul propriétaire)")
    return path.read_bytes()


def build(seed_file: Path, source: Path, out: Path, version: str = __version__) -> Path:
    seed = base64.b64decode(_secret(seed_file).strip(), validate=True)
    envelope, cek = package.encrypt(source, "h2s_agent", version, seed)
    app = out / "app"
    app.mkdir(parents=True, exist_ok=True)
    (app / "h2s_agent.enc").write_text(json.dumps(envelope), encoding="utf-8")
    shutil.copytree(
        AGENT / "h2s_boot",
        app / "h2s_boot",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    cek_path = out / "h2s_agent.cek"
    if cek_path.exists():
        cek_path.unlink()
    fd = os.open(cek_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(base64.b64encode(cek).decode("ascii"))
    return app


def wrap(cek_file: Path, code_public_key: str, out: Path) -> None:
    cek = base64.b64decode(_secret(cek_file).strip(), validate=True)
    out.write_text(
        base64.b64encode(package.wrap_key(cek, code_public_key)).decode("ascii") + "\n",
        encoding="ascii",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Paquet chiffré de l'agent H2S")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="chiffrer et signer une version")
    b.add_argument("--seed-file", required=True, type=Path)
    b.add_argument("--source", default=str(AGENT / "h2s_agent"), type=Path)
    b.add_argument("--out", required=True, type=Path)
    b.add_argument("--version", default=__version__)
    w = sub.add_parser("wrap", help="chiffrer la clé du paquet pour un boîtier")
    w.add_argument("--cek", required=True, type=Path)
    w.add_argument("--code-public-key", required=True)
    w.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "build":
        app = build(args.seed_file, args.source, args.out, args.version)
        print(f"{app}/h2s_agent.enc publié ; clé AES dans {args.out}/h2s_agent.cek (hors ligne)")
    else:
        wrap(args.cek, args.code_public_key, args.out)
        print(f"{args.out} écrit")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Signe le paquet de l'agent (D254, D258) : écrit ``h2s_agent/MANIFEST.json`` avec la **clé de publication** de H2S.

Clé dédiée, distincte de la graine des ordres (``H2S_EDGE_SIGNING_SEED``) qui ne quitte jamais le cloud : un poste de
publication compromis ne permet pas de forger des ordres de charge. À lancer sur ce poste (idéalement hors ligne), jamais
sur un boîtier. La graine (32 octets en base64) est lue dans un fichier 0600 ; sa clé publique va dans ``package_public_key``
de chaque ``agent.json``.

    python tools/sign_manifest.py --new-key cle-publication.seed     # une fois : crée la graine, affiche la clé publique
    python tools/sign_manifest.py --seed-file cle-publication.seed   # à chaque version

Le manifeste couvre aussi les ``site-packages`` de l'interpréteur qui lance l'outil (E176) : le lancer avec le venv de
référence de la version, installé par ``pip install -r requirements.txt`` sur la même architecture et le même Python que les
boîtiers (image de référence ou conteneur arm64). ``--site-packages`` désigne un autre environnement ; ``--config-policy``
impose les champs de sécurité d'agent.json (``cloud_url``, ``key_store``, ``tpm_tcti``, ``require_tpm``) ; ``--root`` signe un
autre répertoire de paquet (paquet compilé sans .py, E178).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from h2s_agent import __version__
from h2s_agent.crypto import b64url
from h2s_agent.integrity import (
    MANIFEST,
    PACKAGE,
    manifest_payload,
    site_packages,
)


def sign(
    seed_b64: str,
    root: Path = PACKAGE,
    version: str = __version__,
    site: list[Path] | None = None,
    policy: dict | None = None,
) -> dict[str, str]:
    seed = base64.b64decode(seed_b64.strip(), validate=True)
    if len(seed) != 32:
        raise ValueError(f"graine Ed25519 : 32 octets attendus, {len(seed)} reçus")
    key = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
    payload = manifest_payload(root, version, site, policy)
    return {"payload": payload, "signature": b64url(key.sign(payload.encode("utf-8")))}


def public_key(seed_b64: str) -> str:
    key = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(seed_b64.strip(), validate=True))
    return b64url(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Signe le manifeste d'intégrité de l'agent H2S")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--seed-file", help="graine base64 de la clé de publication (fichier 0600)")
    group.add_argument(
        "--new-key",
        help="crée une graine de publication dans ce fichier (0600) et affiche sa clé publique",
    )
    parser.add_argument(
        "--root",
        default=str(PACKAGE),
        help="répertoire du paquet à signer (défaut : h2s_agent)",
    )
    parser.add_argument(
        "--site-packages",
        action="append",
        help="site-packages de l'environnement de référence (défaut : ceux de cet interpréteur)",
    )
    parser.add_argument(
        "--no-dependencies",
        action="store_true",
        help="développement seulement : ne pas couvrir les dépendances",
    )
    parser.add_argument("--config-policy", help="JSON des champs de sécurité imposés à agent.json")
    args = parser.parse_args(argv)
    if args.new_key:
        fd = os.open(args.new_key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(base64.b64encode(os.urandom(32)).decode("ascii"))
        print(
            "package_public_key :",
            public_key(Path(args.new_key).read_text(encoding="ascii")),
        )
        return 0
    path = Path(args.seed_file)
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        parser.error(f"{path} doit être en 0600 (lisible du seul propriétaire)")
    seed = path.read_text(encoding="ascii")
    root = Path(args.root)
    manifest = root / MANIFEST.name
    site = (
        None
        if args.no_dependencies
        else [Path(p) for p in args.site_packages]
        if args.site_packages
        else site_packages()
    )
    policy = json.loads(Path(args.config_policy).read_text(encoding="utf-8")) if args.config_policy else None
    if manifest.exists():
        manifest.unlink()
    manifest.write_text(
        json.dumps(sign(seed, root, __version__, site, policy), indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"{manifest} signé ({len(json.loads(manifest.read_text())['payload'])} octets de charge utile)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

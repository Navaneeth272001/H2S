"""Amorce : ``python -I -m h2s_boot --config /etc/h2s-agent/agent.json --app /opt/h2s-agent/app {run|migrate|enroll|version|code-key}``.

* ``code-key`` : affiche la clé publique de code du boîtier (la crée au besoin), à remettre au poste de publication.
* autre commande : charge ``<app>/h2s_agent.enc`` avec ``<data_dir>/keys/package.key`` puis passe la commande à l'agent.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

from . import keys, package


def _settings(config: str) -> dict:
    raw = json.loads(Path(config).read_text(encoding="utf-8"))
    return {
        "key_store": str(raw.get("key_store", "software")).lower(),
        "tpm_tcti": str(raw.get("tpm_tcti", "device:/dev/tpmrm0")),
        "data_dir": Path(os.environ.get("H2S_AGENT_DATA_DIR", raw.get("data_dir", "/var/lib/h2s-agent"))),
        "package_public_key": raw.get("package_public_key"),
    }


def code_public_key(s: dict) -> str:
    if s["key_store"] == "tpm2":
        ctx = keys.tpm_context(s["tpm_tcti"])
        try:
            return keys.TpmCodeKey(ctx).public()
        finally:
            ctx.close()
    return keys.software_public(s["data_dir"] / "keys" / "code.pem")


def load(s: dict, app: Path) -> dict:
    """Vérifie, déchiffre et installe le paquet en mémoire ; renvoie l'en-tête signé."""
    if not s["package_public_key"]:
        raise ValueError("package_public_key absente d'agent.json : aucun paquet chiffré ne peut être vérifié")
    envelope = json.loads((app / "h2s_agent.enc").read_text(encoding="utf-8"))
    wrapped = base64.b64decode((s["data_dir"] / "keys" / "package.key").read_text(encoding="ascii").strip())
    if s["key_store"] == "tpm2":
        ctx = keys.tpm_context(s["tpm_tcti"])
        try:
            header, plain = package.decrypt(envelope, s["package_public_key"], keys.TpmCodeKey(ctx).unwrap, wrapped)
        finally:
            ctx.close()  # l'agent ouvre ensuite son propre contexte TPM
    else:
        header, plain = package.decrypt(
            envelope,
            s["package_public_key"],
            lambda w: keys.software_unwrap(s["data_dir"] / "keys" / "code.pem", w),
            wrapped,
        )
    package.install(plain, header["package"])
    package.APP_DIR = app
    return header


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="h2s_boot", description="Amorce de l'agent H2S à paquet chiffré")
    parser.add_argument("--config", default="/etc/h2s-agent/agent.json")
    parser.add_argument(
        "--app",
        default=str(Path(__file__).resolve().parents[1]),
        help="répertoire de h2s_agent.enc et de MANIFEST.json",
    )
    parser.add_argument("command", choices=["run", "migrate", "enroll", "version", "code-key"])
    args, rest = parser.parse_known_args(argv)
    s = _settings(args.config)
    if args.command == "code-key":
        print(code_public_key(s))
        return 0
    try:
        load(s, Path(args.app))
    except (OSError, ValueError, KeyError) as exc:
        print(f"paquet chiffré non chargé : {exc}", file=sys.stderr)
        return 4
    from h2s_agent.__main__ import main as agent_main  # importé depuis la mémoire

    return agent_main([args.command, "--config", args.config, *rest])


if __name__ == "__main__":
    sys.exit(main())

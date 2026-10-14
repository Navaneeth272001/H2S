"""Intégrité du code de l'agent (CA-18.28, D254 ; étendue par WP8, E176).

Un manifeste ``MANIFEST.json`` porte la signature Ed25519 de la **clé de publication** de H2S (``package_public_key``
d'agent.json ; jamais la clé des ordres, dont la graine ne quitte pas le cloud, D258). Il couvre :

* ``files`` : l'empreinte SHA-256 de chaque fichier du paquet (sources, ou modules compilés d'un paquet sans .py, E178) ;
* ``dependencies`` : chaque fichier des répertoires ``site-packages`` de l'environnement de l'agent (bibliothèques, fichiers
  ``.pth`` compris : un module ajouté à côté des dépendances est un écart comme un fichier modifié) ;
* ``config`` (facultatif) : les valeurs imposées aux champs de sécurité d'agent.json (``cloud_url``, ``key_store``,
  ``require_tpm``, ``tpm_tcti``) ; une configuration du site qui s'en écarte est refusée.

Vérification au démarrage **et toutes les heures** (orchestrateur) : tout écart — fichier modifié, ajouté ou manquant,
signature fausse, dépendance altérée, champ de sécurité d'agent.json modifié depuis le démarrage ou contraire au manifeste —
bloque l'exécution des ordres, met les batteries en veille et lève ``INTEGRITY_FAILED`` ; la collecte continue.

Ne sont pas couverts, car propres à chaque installation : ``__pycache__`` et ``*.pyc`` (le service impose
``PYTHONPYCACHEPREFIX`` hors du paquet et ``PYTHONDONTWRITEBYTECODE``), et les métadonnées d'installation de pip
(``RECORD``, ``INSTALLER``, ``REQUESTED``, ``direct_url.json``). Ce contrôle logiciel ne remplace ni Secure Boot ni les mesures
PCR du TPM (attestation matérielle à distance : non vérifiable sans module) : il détecte une modification, pas un attaquant
maître de la machine (analyse 18 §7.6).
"""

from __future__ import annotations

import hashlib
import json
import sysconfig
from dataclasses import dataclass
from pathlib import Path

from .crypto import verify_signed_payload

PACKAGE = Path(__file__).parent
MANIFEST = PACKAGE / "MANIFEST.json"
#: Champs de sécurité d'agent.json : figés au démarrage, comparés au fichier à chaque vérification, imposables par le manifeste.
SECURITY_FIELDS = (
    "cloud_url",
    "package_public_key",
    "key_store",
    "tpm_tcti",
    "require_tpm",
)
POLICY_FIELDS = ("cloud_url", "key_store", "tpm_tcti", "require_tpm")
VOLATILE_METADATA = {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json"}


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def files(root: Path = PACKAGE) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != MANIFEST.name and "__pycache__" not in p.parts:
            out[str(p.relative_to(root))] = _hash(p)
    return out


def site_packages() -> list[Path]:
    """Répertoires des bibliothèques de l'interpréteur qui exécute l'agent (venv du service)."""
    paths = []
    for key in ("purelib", "platlib"):
        p = Path(sysconfig.get_paths()[key]).resolve()
        if p not in paths and p.is_dir():
            paths.append(p)
    return paths


def dependencies(roots: list[Path]) -> dict[str, str]:
    """Empreinte de chaque fichier des ``site-packages`` ; clé : ``<rang du répertoire>/<chemin relatif>``."""
    out = {}
    for i, root in enumerate(roots):
        for p in sorted(Path(root).rglob("*")):
            if not p.is_file() or "__pycache__" in p.parts or p.suffix == ".pyc":
                continue
            if p.name in VOLATILE_METADATA and p.parent.name.endswith(".dist-info"):
                continue
            out[f"{i}/{p.relative_to(root)}"] = _hash(p)
    return out


def config_fields(raw: dict) -> dict:
    """Champs de sécurité lus dans le JSON d'agent.json (valeurs par défaut explicites : un champ ajouté compte)."""
    defaults = {
        "key_store": "software",
        "tpm_tcti": "device:/dev/tpmrm0",
        "require_tpm": False,
        "package_public_key": None,
        "cloud_url": "",
    }
    return {k: raw.get(k, defaults[k]) for k in SECURITY_FIELDS}


def config_snapshot(path: Path | None) -> dict | None:
    if path is None:
        return None
    return config_fields(json.loads(Path(path).read_text(encoding="utf-8")))


def manifest_payload(
    root: Path = PACKAGE,
    version: str = "",
    site: list[Path] | None = None,
    policy: dict | None = None,
) -> str:
    content: dict = {"type": "manifest", "version": version, "files": files(root)}
    if site is not None:
        content["dependencies"] = dependencies(site)
    if policy:
        unknown = set(policy) - set(POLICY_FIELDS)
        if unknown:
            raise ValueError(f"politique de configuration : champs non pris en charge {sorted(unknown)}")
        content["config"] = policy
    return json.dumps(content, sort_keys=True, separators=(",", ":"))


def _changed(expected: dict, actual: dict) -> list[str]:
    return sorted(k for k in set(expected) | set(actual) if expected.get(k) != actual.get(k))


def verify(
    public_key: str | None,
    root: Path = PACKAGE,
    site: list[Path] | None = None,
    config: dict | None = None,
    running_config: dict | None = None,
) -> Verdict:
    """Vérifie paquet, dépendances (si ``site`` est donné) et champs de sécurité (``config`` : fichier relu, ``running_config`` : au démarrage)."""
    manifest = root / MANIFEST.name
    if not manifest.exists():
        return Verdict(False, "paquet non signé (aucun MANIFEST.json) : développement seulement")
    if public_key is None:
        return Verdict(
            False,
            "clé de publication du paquet absente (package_public_key dans agent.json)",
        )
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        content = verify_signed_payload(public_key, data["payload"], data["signature"])
    except (ValueError, KeyError, json.JSONDecodeError, OSError) as exc:
        return Verdict(False, f"manifeste illisible ou signature fausse ({exc})")
    if content.get("type") != "manifest":
        return Verdict(False, "charge signée qui n'est pas un manifeste")
    changed = _changed(content.get("files", {}), files(root))
    if changed:
        return Verdict(False, "fichiers modifiés, ajoutés ou manquants : " + ", ".join(changed[:5]))
    counted = f"{len(content.get('files', {}))} fichiers"
    if site is not None:
        if "dependencies" not in content:
            return Verdict(
                False,
                "manifeste sans empreinte des dépendances : re-signer le paquet avec son environnement (E176)",
            )
        changed = _changed(content["dependencies"], dependencies(site))
        if changed:
            return Verdict(
                False,
                "dépendances modifiées, ajoutées ou manquantes : " + ", ".join(changed[:5]),
            )
        counted += f" et {len(content['dependencies'])} fichiers de dépendances"
    if config is not None and running_config is not None and config != running_config:
        return Verdict(
            False,
            "champs de sécurité d'agent.json modifiés depuis le démarrage : "
            + ", ".join(_changed(running_config, config)),
        )
    policy = content.get("config") or {}
    effective = running_config if running_config is not None else config
    if policy and effective is not None:
        wrong = sorted(k for k, v in policy.items() if effective.get(k) != v)
        if wrong:
            return Verdict(
                False,
                "agent.json contraire à la politique signée : " + ", ".join(wrong),
            )
    return Verdict(True, counted + " conformes au manifeste signé")

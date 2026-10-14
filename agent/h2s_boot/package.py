"""Paquet chiffré de l'agent (E177, spécification 18 §4.6.3 « déchiffrement à l'exécution ») : format, publication, chargement.

Format ``H2S-PKG-V1`` (JSON) :

* ``payload`` : en-tête **signé** par la clé de publication de H2S (Ed25519, ``package_public_key``) — paquet, version,
  empreintes SHA-256 de l'archive claire et du chiffré, vecteur d'initialisation ;
* ``signature`` ; ``ciphertext`` : archive ZIP du paquet chiffrée en AES-256-GCM, l'en-tête exact en données associées.

La clé AES (32 octets, une par version) est chiffrée **pour chaque boîtier** par sa clé de code (RSA-OAEP / SHA-256, dans son
TPM) : fichier ``package.key`` de ce boîtier, inutile sur un autre.

Au chargement : signature, empreinte du chiffré, ouverture de la clé par le TPM, déchiffrement, empreinte de l'archive, puis
import **depuis la mémoire** (``MemoryFinder``) ; les fichiers de données (migrations SQL) se lisent par ``importlib.resources``.
Rien n'est écrit sur le disque : ni source, ni bytecode (``sys.dont_write_bytecode``), et le chargeur ne rend jamais le source
(``get_source`` absent : pas de source dans les traces). Limite : un attaquant maître du boîtier en fonctionnement lit la
mémoire du processus ; le chiffrement protège la carte SD retirée ou copiée, pas un système compromis (analyse 18 §7.6).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
try:
    from importlib.resources.abc import TraversableResources
except ImportError:
    from importlib.abc import TraversableResources
import io
import json
import secrets
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .keys import OAEP

FORMAT = "H2S-PKG-V1"
ORIGIN_PREFIX = "/h2s-enc/"
#: Répertoire de l'installation chiffrée (paquet et manifeste), fixé par l'amorce : l'agent y vérifie son intégrité.
APP_DIR: Path | None = None


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def archive(root: Path, package: str) -> bytes:
    """Archive ZIP déterministe du paquet (hors ``__pycache__`` et bytecode), chemins ``<paquet>/…``."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(Path(root).rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
                info = zipfile.ZipInfo(
                    f"{package}/{p.relative_to(root).as_posix()}",
                    date_time=(2026, 1, 1, 0, 0, 0),
                )
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, p.read_bytes())
    return buf.getvalue()


def encrypt(root: Path, package: str, version: str, publication_seed: bytes) -> tuple[dict, bytes]:
    """Poste de publication : paquet chiffré signé et sa clé AES (à garder hors ligne pour la chiffrer pour chaque boîtier)."""
    if len(publication_seed) != 32:
        raise ValueError("graine de publication : 32 octets attendus")
    plain = archive(root, package)
    cek = AESGCM.generate_key(bit_length=256)
    iv = secrets.token_bytes(12)
    header = {
        "format": FORMAT,
        "package": package,
        "version": version,
        "plain_sha256": hashlib.sha256(plain).hexdigest(),
        "iv": base64.b64encode(iv).decode("ascii"),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    # L'empreinte du chiffré ne peut pas être dans les données associées du chiffrement lui-même : en-tête sans elle d'abord,
    # puis en-tête signé complet.
    aad = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(cek).encrypt(iv, plain, aad)
    header["cipher_sha256"] = hashlib.sha256(ciphertext).hexdigest()
    payload = json.dumps(header, sort_keys=True, separators=(",", ":"))
    key = ed25519.Ed25519PrivateKey.from_private_bytes(publication_seed)
    signature = base64.urlsafe_b64encode(key.sign(payload.encode("utf-8"))).decode("ascii").rstrip("=")
    return {
        "payload": payload,
        "signature": signature,
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }, cek


def wrap_key(cek: bytes, code_public_key_b64: str) -> bytes:
    """Clé AES du paquet chiffrée pour la clé de code d'un boîtier (``package.key``)."""
    public = serialization.load_der_public_key(base64.b64decode(code_public_key_b64))
    return public.encrypt(cek, OAEP)


def decrypt(
    envelope: dict,
    publication_public_key: str,
    unwrap: Callable[[bytes], bytes],
    wrapped_key: bytes,
) -> tuple[dict, bytes]:
    """Vérifie et déchiffre en mémoire ; lève ``ValueError`` au premier défaut (jamais d'exécution d'un paquet non conforme)."""
    try:
        payload = envelope["payload"]
        ed25519.Ed25519PublicKey.from_public_bytes(_b64url_decode(publication_public_key)).verify(
            _b64url_decode(envelope["signature"]), payload.encode("utf-8")
        )
        header = json.loads(payload)
        ciphertext = base64.b64decode(envelope["ciphertext"])
    except (KeyError, TypeError, ValueError, InvalidSignature) as exc:
        raise ValueError("paquet chiffré : signature de publication fausse ou enveloppe illisible") from exc
    if header.get("format") != FORMAT:
        raise ValueError("paquet chiffré : format inconnu")
    if hashlib.sha256(ciphertext).hexdigest() != header.get("cipher_sha256"):
        raise ValueError("paquet chiffré : chiffré modifié")
    cek = unwrap(wrapped_key)
    aad_header = {k: v for k, v in header.items() if k != "cipher_sha256"}
    try:
        plain = AESGCM(cek).decrypt(
            base64.b64decode(header["iv"]),
            ciphertext,
            json.dumps(aad_header, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
    except (InvalidTag, ValueError) as exc:
        raise ValueError("paquet chiffré : déchiffrement refusé (clé d'un autre paquet ou contenu altéré)") from exc
    if hashlib.sha256(plain).hexdigest() != header.get("plain_sha256"):
        raise ValueError("paquet chiffré : archive différente de l'archive publiée")
    return header, plain


class MemoryFinder(importlib.abc.MetaPathFinder):
    """Importe un paquet depuis une archive en mémoire. Sources compilées en mémoire, jamais écrites ni rendues."""

    def __init__(self, plain: bytes, package: str) -> None:
        self.package = package
        self._zip = zipfile.ZipFile(io.BytesIO(plain))
        self._names = set(self._zip.namelist())

    def _entry(self, fullname: str) -> tuple[str, bool] | None:
        if fullname != self.package and not fullname.startswith(self.package + "."):
            return None
        base = fullname.replace(".", "/")
        if f"{base}/__init__.py" in self._names:
            return f"{base}/__init__.py", True
        if f"{base}.py" in self._names:
            return f"{base}.py", False
        return None

    def find_spec(self, fullname, path=None, target=None):
        entry = self._entry(fullname)
        if entry is None:
            return None
        name, is_package = entry
        loader = MemoryLoader(self, name)
        spec = importlib.util.spec_from_loader(fullname, loader, origin=ORIGIN_PREFIX + name, is_package=is_package)
        spec.has_location = True
        if is_package:
            spec.submodule_search_locations = []
        return spec

    def read(self, name: str) -> bytes:
        return self._zip.read(name)

    def files(self, package: str):
        return zipfile.Path(self._zip, at=package.replace(".", "/") + "/")


class MemoryLoader(importlib.abc.Loader):
    def __init__(self, finder: MemoryFinder, name: str) -> None:
        self._finder = finder
        self._name = name

    def create_module(self, spec):
        return None

    def exec_module(self, module) -> None:
        code = compile(
            self._finder.read(self._name),
            ORIGIN_PREFIX + self._name,
            "exec",
            dont_inherit=True,
        )
        exec(code, module.__dict__)  # noqa: S102 - code du paquet vérifié (signature de publication) puis déchiffré

    def get_resource_reader(self, fullname):
        return _Reader(self._finder, fullname)


class _Reader(TraversableResources):
    def __init__(self, finder: MemoryFinder, package: str) -> None:
        self._finder = finder
        self._package = package

    def files(self):
        return self._finder.files(self._package)


def install(plain: bytes, package: str) -> MemoryFinder:
    """Installe le chercheur en tête : le paquet chiffré l'emporte sur toute copie en clair qui traînerait sur le disque."""
    if package in sys.modules:
        raise RuntimeError(f"{package} déjà importé depuis le disque : l'amorce doit le charger en premier")
    sys.dont_write_bytecode = True
    finder = MemoryFinder(plain, package)
    sys.meta_path.insert(0, finder)
    return finder

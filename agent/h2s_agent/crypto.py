"""Cryptographie de l'agent (D249, D250, D253, D254).

* Identité : clé de l'agent derrière le port ``KeyStore`` : Ed25519 logicielle (``SoftwareKeyStore``) ou ECDSA P-256 non
  exportable dans le TPM 2.0 (``tpm.Tpm2KeyStore``, WP8) ; le magasin se choisit dans agent.json (``key_store``).
* Requêtes : signature de la chaîne canonique ``H2S-AGENT-V1`` (identique à ``com.h2s.edge.model.AgentRequest``).
* Ordres et modèles : vérification Ed25519 de la charge utile exacte reçue, avec la clé publique remise à l'enrôlement.
* Contributions fédérées : AES-256-GCM, clé chiffrée par RSA-OAEP (SHA-256) pour la clé publique du cloud.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, utils
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SCHEME = "H2S-AGENT-V1"
HYBRID_ALG = "RSA-OAEP-256+A256GCM"
#: Chaîne canonique des commandes MQTT signées pour la passerelle de la batterie (§4.3.5, E183).
COMMAND_SCHEME = "H2S-COMMAND-V1"
COMMAND_FIELDS = (
    "command_id",
    "action",
    "power_w",
    "duration_s",
    "priority",
    "timestamp",
    "source",
)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class KeyStore(Protocol):
    """Port de la clé d'identité : la clé privée ne sort jamais du magasin.

    ``kind`` : ``SOFTWARE`` ou ``TPM2`` ; ``algorithm`` : ``Ed25519`` ou ``ECDSA-P256`` (signature r ‖ s, 64 octets).
    ``code_public_key`` et ``unwrap_key`` ouvrent la clé du paquet chiffré (E177) sans la laisser sur le disque.
    """

    kind: str
    algorithm: str

    def public_key(self) -> str: ...

    def sign(self, message: bytes) -> str: ...

    def code_public_key(self) -> str: ...

    def unwrap_key(self, wrapped: bytes) -> bytes: ...


def _private_file(path: Path, pem: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)


def _load_private(path: Path):
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PermissionError(f"clé d'agent lisible par d'autres comptes ({oct(mode)}) : chmod 600 {path}")
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


class SoftwareKeyStore:
    """Clé Ed25519 dans un fichier 0600 hors de l'arborescence du code (D254). Sans TPM : une copie du fichier vaut l'identité."""

    kind = "SOFTWARE"
    algorithm = "Ed25519"

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        if self._path.exists():
            self._key = _load_private(self._path)
            if not isinstance(self._key, ed25519.Ed25519PrivateKey):
                raise ValueError("clé d'agent : Ed25519 attendue")
        else:
            self._key = ed25519.Ed25519PrivateKey.generate()
            _private_file(
                self._path,
                self._key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ),
            )

    def public_key(self) -> str:
        raw = self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return b64url(raw)

    def sign(self, message: bytes) -> str:
        return b64url(self._key.sign(message))

    def code_public_key(self) -> str:
        """Clé de code du boîtier (E177), même implémentation que l'amorce ``h2s_boot`` qui déchiffre le paquet."""
        from h2s_boot import keys

        return keys.software_public(self._path.with_name("code.pem"))

    def unwrap_key(self, wrapped: bytes) -> bytes:
        from h2s_boot import keys

        return keys.software_unwrap(self._path.with_name("code.pem"), wrapped)


def canonical(method: str, path_and_query: str, epoch_seconds: int, nonce: str, body: bytes) -> str:
    return "\n".join(
        [
            SCHEME,
            method.upper(),
            path_and_query,
            str(epoch_seconds),
            nonce,
            sha256_hex(body or b""),
        ]
    )


def key_id(public_key_b64url: str) -> str:
    """Identifiant court d'une clé publique d'agent : 16 premiers caractères hexadécimaux de l'empreinte SHA-256 de la clé brute."""
    return sha256_hex(b64url_decode(public_key_b64url))[:16]


def command_canonical(command: dict) -> str:
    """Charge canonique d'une commande de batterie (E183) : schéma puis les sept champs, un par ligne, puissance à deux décimales.

    La passerelle recompose la même chaîne depuis le JSON reçu et vérifie ``signature`` avec la clé publique retenue sur
    ``h2s/site<N>/agent/pubkey`` (``verify_agent_signature``) ; un champ ajouté au JSON hors de cette liste n'est pas couvert.
    """
    values = []
    for name in COMMAND_FIELDS:
        v = command[name]
        values.append(f"{float(v):.2f}" if name == "power_w" else str(v))
    return "\n".join([COMMAND_SCHEME, *values])


def sign_command(keys: KeyStore, command: dict) -> dict:
    """Commande complétée de ``alg``, ``key_id`` et ``signature`` (clé d'identité de l'agent : Ed25519 ou ECDSA P-256 du TPM)."""
    signed = dict(command)
    signed["alg"] = getattr(keys, "algorithm", "Ed25519")
    signed["key_id"] = key_id(keys.public_key())
    signed["signature"] = keys.sign(command_canonical(command).encode("utf-8"))
    return signed


def new_nonce() -> str:
    return secrets.token_hex(16)


def signed_headers(
    keys: KeyStore,
    agent_uuid: str,
    method: str,
    path_and_query: str,
    epoch_seconds: int,
    body: bytes,
    nonce: str | None = None,
) -> dict[str, str]:
    n = nonce or new_nonce()
    return {
        "X-H2S-Agent": agent_uuid,
        "X-H2S-Timestamp": str(epoch_seconds),
        "X-H2S-Nonce": n,
        "X-H2S-Signature": keys.sign(canonical(method, path_and_query, epoch_seconds, n, body).encode("utf-8")),
    }


def verify_agent_signature(algorithm: str, public_key_b64url: str, message: bytes, signature: str) -> bool:
    """Vérifie une signature d'agent (``Ed25519`` ou ``ECDSA-P256`` r ‖ s), comme ``com.h2s.edge.model.EdgeCrypto``. Ne lève pas."""
    try:
        raw = b64url_decode(signature)
        if algorithm == "Ed25519":
            ed25519.Ed25519PublicKey.from_public_bytes(b64url_decode(public_key_b64url)).verify(raw, message)
            return True
        if algorithm == "ECDSA-P256" and len(raw) == 64:
            key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64url_decode(public_key_b64url))
            der = utils.encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
            key.verify(der, message, ec.ECDSA(hashes.SHA256()))
            return True
    except (InvalidSignature, ValueError, TypeError):
        return False
    return False


def verify_signed_payload(public_key_b64url: str, payload: str, signature: str) -> dict:
    """Vérifie la signature de la charge utile exacte puis la lit. Lève ``ValueError`` si la signature est fausse."""
    try:
        key = ed25519.Ed25519PublicKey.from_public_bytes(b64url_decode(public_key_b64url))
        key.verify(b64url_decode(signature), payload.encode("utf-8"))
    except (InvalidSignature, ValueError) as exc:
        raise ValueError("signature du cloud invalide") from exc
    return json.loads(payload)


def encrypt_for_cloud(gradient_public_key_b64: str, clear: bytes) -> dict[str, str]:
    public = serialization.load_der_public_key(base64.b64decode(gradient_public_key_b64))
    key = AESGCM.generate_key(bit_length=256)
    iv = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(iv, clear, None)
    ek = public.encrypt(
        key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return {
        "alg": HYBRID_ALG,
        "encrypted_key": base64.b64encode(ek).decode("ascii"),
        "iv": base64.b64encode(iv).decode("ascii"),
        "ciphertext": base64.b64encode(ct).decode("ascii"),
    }

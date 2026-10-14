"""Clé de code du boîtier (E177) : RSA 2048, déchiffrement OAEP / SHA-256 de la clé AES du paquet.

* TPM 2.0 : clé créée dans le TPM sous la clé racine de stockage, persistante (0x8100001A), ``fixedTPM`` et ``fixedParent`` —
  la clé privée ne quitte jamais le module, le déchiffrement est calculé par lui.
* Logiciel : fichier PEM 0600 (``<data_dir>/keys/code.pem``) ; sans TPM, qui lit ce fichier lit le paquet (limite assumée).

La clé publique (SubjectPublicKeyInfo DER en base64) est remise au poste de publication, qui y chiffre la clé du paquet de ce
boîtier (``tools/publish_package.py wrap``).
"""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

CODE_KEY_HANDLE = 0x8100001A
OAEP = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)


def _spki_b64(key: rsa.RSAPublicKey) -> str:
    return base64.b64encode(
        key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    ).decode("ascii")


# ---------------------------------------------------------------------- logiciel


def software_key(path: Path) -> rsa.RSAPrivateKey:
    path = Path(path)
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(f"clé de code lisible par d'autres comptes ({oct(mode)}) : chmod 600 {path}")
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError("clé de code : RSA attendue")
        return key
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    return key


def software_public(path: Path) -> str:
    return _spki_b64(software_key(path).public_key())


def software_unwrap(path: Path, wrapped: bytes) -> bytes:
    try:
        return software_key(path).decrypt(wrapped, OAEP)
    except ValueError as exc:
        raise ValueError("clé du paquet indéchiffrable par ce boîtier") from exc


# ---------------------------------------------------------------------- TPM 2.0


class TpmCodeKey:
    """Clé de code dans le TPM, sur un contexte ESAPI ouvert (swtpm ne sert qu'une connexion : le contexte est partagé)."""

    def __init__(self, ctx, handle: int = CODE_KEY_HANDLE) -> None:
        import tpm2_pytss as lib

        self._lib = lib
        self._ctx = ctx
        self._handle = handle

    def _key(self):
        lib = self._lib
        try:
            return self._ctx.tr_from_tpmpublic(self._handle)
        except lib.TSS2_Exception:
            pass
        srk, *_ = self._ctx.create_primary(
            lib.TPM2B_SENSITIVE_CREATE(),
            lib.TPM2B_PUBLIC.parse(
                "ecc256:aes128cfb",
                objectAttributes=lib.TPMA_OBJECT.DEFAULT_TPM2_TOOLS_CREATEPRIMARY_ATTRS,
            ),
            lib.ESYS_TR.OWNER,
        )
        try:
            attrs = (
                lib.TPMA_OBJECT.FIXEDTPM
                | lib.TPMA_OBJECT.FIXEDPARENT
                | lib.TPMA_OBJECT.SENSITIVEDATAORIGIN
                | lib.TPMA_OBJECT.USERWITHAUTH
                | lib.TPMA_OBJECT.DECRYPT
            )
            template = lib.TPM2B_PUBLIC.parse(
                "rsa2048:oaep-sha256:null",
                nameAlg=lib.TPM2_ALG.SHA256,
                objectAttributes=attrs,
            )
            private, public, *_ = self._ctx.create(srk, lib.TPM2B_SENSITIVE_CREATE(), template)
            loaded = self._ctx.load(srk, private, public)
            try:
                return self._ctx.evict_control(lib.ESYS_TR.OWNER, loaded, self._handle)
            finally:
                self._ctx.flush_context(loaded)
        finally:
            self._ctx.flush_context(srk)

    def public(self) -> str:
        public, _, _ = self._ctx.read_public(self._key())
        n = int.from_bytes(bytes(public.publicArea.unique.rsa), "big")
        e = int(public.publicArea.parameters.rsaDetail.exponent) or 65537
        return _spki_b64(rsa.RSAPublicNumbers(e, n).public_key())

    def unwrap(self, wrapped: bytes) -> bytes:
        lib = self._lib
        scheme = lib.TPMT_RSA_DECRYPT(scheme=lib.TPM2_ALG.OAEP)
        scheme.details.oaep.hashAlg = lib.TPM2_ALG.SHA256
        try:
            return bytes(
                self._ctx.rsa_decrypt(
                    self._key(),
                    lib.TPM2B_PUBLIC_KEY_RSA(wrapped),
                    scheme,
                    lib.TPM2B_DATA(),
                )
            )
        except lib.TSS2_Exception as exc:
            raise ValueError("clé du paquet indéchiffrable par ce TPM") from exc


def tpm_context(tcti: str):
    os.environ.setdefault("TSS2_LOG", "esys+none")
    import tpm2_pytss

    return tpm2_pytss.ESAPI(tcti)

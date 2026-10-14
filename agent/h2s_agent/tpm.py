"""TPM 2.0 de l'agent (§2.18, §3.1, §4.2 du cahier des charges ; CA-18.18, CA-18.27, CA-18.33 ; décisions WP8).

Tout passe par ``tpm2-pytss`` (ESAPI), importé seulement quand le magasin ``tpm2`` est configuré : l'agent en magasin
logiciel n'en dépend pas.

* **Sonde** (``probe``) : périphérique présent, propriétés lues dans le TPM (fabricant, version du microcode), certificat EK
  présent. Un simulateur (TCTI ``swtpm`` ou ``mssim``, ou TPM logiciel d'IBM) est toujours signalé comme tel : jamais présenté
  comme un module matériel au cloud.
* **Clé d'identité** (``Tpm2KeyStore``) : ECDSA P-256 créée dans le TPM sous la clé racine de stockage, persistante,
  ``fixedTPM`` et ``fixedParent`` (non exportable). Un TPM 2.0 ne connaît pas Ed25519 : le cloud vérifie les deux (D249 inchangée
  pour les agents déjà enrôlés).
* **Preuve d'enrôlement** (``attestation``) : certificat EK du fabricant, clé d'attestation (AK, signature restreinte) et
  ``TPM2_Certify`` de la clé d'identité par l'AK ; le lien AK ↔ EK est prouvé par ``TPM2_ActivateCredential`` sur le défi du
  cloud (``activate_credential``).
* **Clé de code** : RSA 2048 de déchiffrement persistante et non exportable ; elle ouvre la clé AES du paquet chiffré (E177).

Poignées persistantes dans la plage du propriétaire (registre TCG) : identité 0x81000018, clé de code 0x8100001A.
"""

from __future__ import annotations

import base64
import os
import threading
from pathlib import Path
from typing import Any

from .crypto import b64url

DEFAULT_TCTI = "device:/dev/tpmrm0"
SIMULATOR_TCTIS = ("swtpm", "mssim")
SIGNING_HANDLE = 0x81000018
CODE_KEY_HANDLE = 0x8100001A
EK_RSA_CERT_INDEX = 0x01C00002
ALGORITHM = "ECDSA-P256"
# Propriétés fixes (TPM 2.0 Part 2, TPM_PT) : famille, fabricant, chaîne du vendeur, version du microcode.
PT_FAMILY_INDICATOR = 0x100
PT_MANUFACTURER = 0x105
PT_VENDOR_STRING_1 = 0x106
PT_FIRMWARE_VERSION_1 = 0x10B
PT_FIRMWARE_VERSION_2 = 0x10C


class TpmError(RuntimeError):
    """TPM absent, inaccessible ou en erreur : l'agent configuré en ``tpm2`` ne démarre pas sans lui (jamais de repli logiciel)."""


def _lib():
    # Une poignée persistante absente au premier démarrage est un cas prévu : les journaux d'erreur ESAPI ne sont que du bruit.
    os.environ.setdefault("TSS2_LOG", "esys+none")
    try:
        import tpm2_pytss
        from tpm2_pytss import ESAPI, TSS2_Exception  # noqa: F401
    except (ImportError, OSError) as exc:  # bibliothèque tpm2-tss absente du système
        raise TpmError(f"tpm2-pytss indisponible ({exc}) : installer libtss2 et tpm2-pytss (requirements.txt)") from exc
    return tpm2_pytss


def tcti_name(tcti: str) -> str:
    return tcti.split(":", 1)[0]


def is_simulator_tcti(tcti: str) -> bool:
    return tcti_name(tcti) in SIMULATOR_TCTIS


def _device_path(tcti: str) -> str | None:
    name, _, conf = tcti.partition(":")
    if name != "device":
        return None
    return conf or "/dev/tpmrm0"


def open_context(tcti: str):
    path = _device_path(tcti)
    if path is not None and not Path(path).exists():
        raise TpmError(f"aucun TPM : {path} absent (activer le module, dtoverlay=tpm-slb9670 sur Raspberry Pi)")
    lib = _lib()
    try:
        return lib.ESAPI(tcti)
    except (lib.TSS2_Exception, RuntimeError, OSError) as exc:
        raise TpmError(f"TPM injoignable par {tcti} : {exc}") from exc


def _text(values: list[int]) -> str:
    raw = b"".join(v.to_bytes(4, "big") for v in values)
    return raw.replace(b"\x00", b"").decode("ascii", "replace").strip()


def _properties(ctx) -> dict[int, int]:
    lib = _lib()
    out: dict[int, int] = {}
    _more, data = ctx.get_capability(lib.TPM2_CAP.TPM_PROPERTIES, lib.TPM2_PT.FIXED, 64)
    for p in data.data.tpmProperties.tpmProperty:
        out[int(p.property)] = int(p.value)
    return out


def _nv_read_all(ctx, index: int) -> bytes:
    lib = _lib()
    handle = ctx.tr_from_tpmpublic(index)
    try:
        public, _ = ctx.nv_read_public(handle)
        size = int(public.nvPublic.dataSize)
        chunk = 512
        data = b""
        while len(data) < size:
            n = min(chunk, size - len(data))
            data += bytes(ctx.nv_read(handle, n, len(data), lib.ESYS_TR.OWNER))
        return data
    finally:
        ctx.tr_close(handle)


def ek_certificate(ctx) -> bytes | None:
    """Certificat EK RSA 2048 (index NV 0x01C00002 du profil TCG), DER ; ``None`` si le fabricant n'en a pas déposé."""
    lib = _lib()
    try:
        raw = _nv_read_all(ctx, EK_RSA_CERT_INDEX)
    except lib.TSS2_Exception:
        return None
    # Certains fabricants complètent l'index par des octets nuls : longueur DER de la SEQUENCE extérieure.
    if len(raw) > 4 and raw[0] == 0x30 and raw[1] == 0x82:
        raw = raw[: 4 + int.from_bytes(raw[2:4], "big")]
    return raw


def probe(tcti: str = DEFAULT_TCTI, ctx=None) -> dict[str, Any]:
    """État du TPM exposé par ``/api/agent/tpm`` et remonté au cloud. Ne lève jamais.

    ``ctx`` : contexte déjà ouvert (celui du magasin de clés) ; un simulateur ``swtpm`` ne sert qu'une connexion à la fois.
    """
    simulator = is_simulator_tcti(tcti)
    path = _device_path(tcti)
    base: dict[str, Any] = {
        "tcti": tcti_name(tcti),
        "device": path,
        "simulator": simulator,
    }
    if ctx is None and path is not None and not Path(path).exists():
        return {**base, "status": "ABSENT", "detail": f"{path} absent"}
    own = ctx is None
    if own:
        try:
            ctx = open_context(tcti)
        except TpmError as exc:
            return {**base, "status": "ERROR", "detail": str(exc)[:200]}
    try:
        props = _properties(ctx)
        manufacturer = _text([props.get(PT_MANUFACTURER, 0)])
        vendor = _text([props.get(PT_VENDOR_STRING_1 + i, 0) for i in range(4)])
        family = _text([props.get(PT_FAMILY_INDICATOR, 0)])
        fw1 = props.get(PT_FIRMWARE_VERSION_1, 0)
        fw2 = props.get(PT_FIRMWARE_VERSION_2, 0)
        ctx.get_random(8)  # le TPM répond à une commande, pas seulement à la lecture de ses propriétés
        # Le TPM logiciel d'IBM (swtpm, libtpms) se déclare « IBM » / « SW TPM » : simulateur même derrière /dev/tpmrm0 (vTPM).
        simulator = simulator or (manufacturer == "IBM" and "SW" in vendor.replace(" ", "").upper())
        cert = ek_certificate(ctx)
        return {
            **base,
            "simulator": simulator,
            "status": "OPERATIONAL",
            "family": family,
            "manufacturer": manufacturer,
            "vendor": vendor,
            "firmware": f"{fw1 >> 16}.{fw1 & 0xFFFF}.{fw2 >> 16}.{fw2 & 0xFFFF}",
            "ek_certificate": cert is not None,
        }
    except Exception as exc:  # noqa: BLE001 - une sonde ne lève pas
        return {
            **base,
            "status": "ERROR",
            "detail": f"{type(exc).__name__}: {exc}"[:200],
        }
    finally:
        if own:
            ctx.close()


class Tpm2KeyStore:
    """Port ``KeyStore`` sur TPM 2.0 : la clé privée ne sort jamais du module ; chaque signature est calculée par lui."""

    kind = "TPM2"
    algorithm = ALGORITHM

    def __init__(
        self,
        tcti: str = DEFAULT_TCTI,
        handle: int = SIGNING_HANDLE,
        code_handle: int = CODE_KEY_HANDLE,
    ) -> None:
        self.tcti = tcti
        self.simulator = is_simulator_tcti(tcti)
        self._lib = _lib()
        self._ctx = open_context(tcti)
        self._lock = threading.RLock()
        self._handle = handle
        self._code_handle = code_handle
        try:
            self._key = self._persistent(handle, self._signing_template)
            public, name, _ = self._ctx.read_public(self._key)
        except self._lib.TSS2_Exception as exc:
            self._ctx.close()
            raise TpmError(f"clé d'identité TPM indisponible : {exc}") from exc
        self._public = public
        self._name = bytes(name)
        ecc = public.publicArea.unique.ecc
        self._point = b"\x04" + bytes(ecc.x).rjust(32, b"\x00") + bytes(ecc.y).rjust(32, b"\x00")

    # ------------------------------------------------------------------ gabarits

    def _signing_template(self):
        lib = self._lib
        attrs = (
            lib.TPMA_OBJECT.FIXEDTPM
            | lib.TPMA_OBJECT.FIXEDPARENT
            | lib.TPMA_OBJECT.SENSITIVEDATAORIGIN
            | lib.TPMA_OBJECT.USERWITHAUTH
            | lib.TPMA_OBJECT.SIGN_ENCRYPT
        )
        return lib.TPM2B_PUBLIC.parse(
            "ecc256:ecdsa-sha256:null",
            nameAlg=lib.TPM2_ALG.SHA256,
            objectAttributes=attrs,
        )

    def _ak_template(self):
        lib = self._lib
        attrs = (
            lib.TPMA_OBJECT.FIXEDTPM
            | lib.TPMA_OBJECT.FIXEDPARENT
            | lib.TPMA_OBJECT.SENSITIVEDATAORIGIN
            | lib.TPMA_OBJECT.USERWITHAUTH
            | lib.TPMA_OBJECT.RESTRICTED
            | lib.TPMA_OBJECT.SIGN_ENCRYPT
        )
        return lib.TPM2B_PUBLIC.parse(
            "ecc256:ecdsa-sha256:null",
            nameAlg=lib.TPM2_ALG.SHA256,
            objectAttributes=attrs,
        )

    def _srk(self):
        lib = self._lib
        attrs = lib.TPMA_OBJECT.DEFAULT_TPM2_TOOLS_CREATEPRIMARY_ATTRS
        handle, *_ = self._ctx.create_primary(
            lib.TPM2B_SENSITIVE_CREATE(),
            lib.TPM2B_PUBLIC.parse("ecc256:aes128cfb", objectAttributes=attrs),
            lib.ESYS_TR.OWNER,
        )
        return handle

    def _persistent(self, handle: int, template):
        lib = self._lib
        try:
            return self._ctx.tr_from_tpmpublic(handle)
        except lib.TSS2_Exception:
            pass
        srk = self._srk()
        try:
            private, public, *_ = self._ctx.create(srk, lib.TPM2B_SENSITIVE_CREATE(), template())
            loaded = self._ctx.load(srk, private, public)
            try:
                return self._ctx.evict_control(lib.ESYS_TR.OWNER, loaded, handle)
            finally:
                self._ctx.flush_context(loaded)
        finally:
            self._ctx.flush_context(srk)

    # ------------------------------------------------------------------ KeyStore

    def public_key(self) -> str:
        """Point public non compressé (0x04 ‖ X ‖ Y, 65 octets) en base64url : 87 caractères."""
        return b64url(self._point)

    def sign(self, message: bytes) -> str:
        """ECDSA P-256 / SHA-256 calculée par le TPM ; signature r ‖ s (64 octets, format IEEE P1363) en base64url."""
        import hashlib

        lib = self._lib
        digest = hashlib.sha256(message).digest()
        with self._lock:
            sig = self._ctx.sign(
                self._key,
                lib.TPM2B_DIGEST(digest),
                lib.TPMT_SIG_SCHEME(scheme=lib.TPM2_ALG.NULL),
                lib.TPMT_TK_HASHCHECK(tag=lib.TPM2_ST.HASHCHECK, hierarchy=lib.TPM2_RH.NULL),
            )
        r = bytes(sig.signature.ecdsa.signatureR).rjust(32, b"\x00")
        s = bytes(sig.signature.ecdsa.signatureS).rjust(32, b"\x00")
        return b64url(r + s)

    def probe(self) -> dict[str, Any]:
        with self._lock:
            return probe(self.tcti, self._ctx)

    # ------------------------------------------------------------------ attestation d'enrôlement (E187)

    def attestation(self, qualifying_data: bytes) -> dict[str, Any]:
        """Preuve TPM de la clé d'identité : certificat EK, AK, ``TPM2_Certify`` de la clé par l'AK sur ``qualifying_data``."""
        lib = self._lib
        with self._lock:
            cert = ek_certificate(self._ctx)
            ak = self._ak()
            try:
                ak_public, _, _ = self._ctx.read_public(ak)
                info, signature = self._ctx.certify(
                    self._key,
                    ak,
                    lib.TPM2B_DATA(qualifying_data),
                    lib.TPMT_SIG_SCHEME(scheme=lib.TPM2_ALG.NULL),
                )
            finally:
                self._ctx.flush_context(ak)
        return {
            "format": "TPM2-CERTIFY-V1",
            "ek_certificate": base64.b64encode(cert).decode("ascii") if cert else None,
            "ak_public": base64.b64encode(ak_public.publicArea.marshal()).decode("ascii"),
            "key_public": base64.b64encode(self._public.publicArea.marshal()).decode("ascii"),
            "certify_info": base64.b64encode(bytes(info)).decode("ascii"),
            "certify_signature": base64.b64encode(signature.marshal()).decode("ascii"),
            "simulator": self.simulator,
        }

    def _ak(self):
        lib = self._lib
        handle, *_ = self._ctx.create_primary(
            lib.TPM2B_SENSITIVE_CREATE(), self._ak_template(), lib.ESYS_TR.ENDORSEMENT
        )
        return handle

    def _ek(self):
        from tpm2_pytss.utils import NoSuchIndex, create_ek_template

        lib = self._lib

        def nv_read(index):
            try:
                return _nv_read_all(self._ctx, int(index))
            except lib.TSS2_Exception as exc:
                raise NoSuchIndex(int(index)) from exc

        _, template = create_ek_template("EK-RSA2048", nv_read)
        handle, *_ = self._ctx.create_primary(lib.TPM2B_SENSITIVE_CREATE(), template, lib.ESYS_TR.ENDORSEMENT)
        return handle

    def activate_credential(self, credential_blob: bytes, encrypted_secret: bytes) -> bytes:
        """Défi du cloud : le TPM ne rend le secret que si l'AK et l'EK certifié sont dans le même module."""
        lib = self._lib
        with self._lock:
            ek = self._ek()
            ak = self._ak()
            session = None
            try:
                session = self._ctx.start_auth_session(
                    lib.ESYS_TR.NONE,
                    lib.ESYS_TR.NONE,
                    lib.TPM2_SE.POLICY,
                    lib.TPMT_SYM_DEF(algorithm=lib.TPM2_ALG.NULL),
                    lib.TPM2_ALG.SHA256,
                )
                self._ctx.policy_secret(lib.ESYS_TR.ENDORSEMENT, session, b"", b"", b"", 0)
                blob, _ = lib.TPM2B_ID_OBJECT.unmarshal(credential_blob)
                secret, _ = lib.TPM2B_ENCRYPTED_SECRET.unmarshal(encrypted_secret)
                out = self._ctx.activate_credential(
                    ak,
                    ek,
                    blob,
                    secret,
                    session1=lib.ESYS_TR.PASSWORD,
                    session2=session,
                )
                session = None  # session de politique consommée par la commande
                return bytes(out)
            finally:
                if session is not None:
                    self._ctx.flush_context(session)
                self._ctx.flush_context(ak)
                self._ctx.flush_context(ek)

    # ------------------------------------------------------------------ clé de code (E177)

    def code_public_key(self) -> str:
        """Clé publique RSA (SubjectPublicKeyInfo DER, base64) qui chiffre la clé AES du paquet de cet agent (implémentation de l'amorce)."""
        from h2s_boot.keys import TpmCodeKey

        with self._lock:
            return TpmCodeKey(self._ctx, self._code_handle).public()

    def unwrap_key(self, wrapped: bytes) -> bytes:
        """RSA-OAEP (SHA-256) par le TPM : la clé du paquet n'existe en clair qu'en mémoire du processus."""
        from h2s_boot.keys import TpmCodeKey

        with self._lock:
            return TpmCodeKey(self._ctx, self._code_handle).unwrap(wrapped)

    def close(self) -> None:
        with self._lock:
            self._ctx.close()

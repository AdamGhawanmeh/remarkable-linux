"""aiortc compatibility: DTLS fingerprint checks against the tablet's certificate.

The tablet's WebRTC stack (libdatachannel/libjuice) presents a self-signed certificate
whose X.509 version field is 1 (v2). aiortc checks the peer's fingerprint by loading
the certificate with `cryptography`, which refuses that version ("1 is not a valid X509
version", seen in the first live session on 2026-11-02), so the handshake dies before
anything is compared. A fingerprint is only a hash of the certificate's DER bytes, so
this check takes those bytes straight from OpenSSL and never parses the certificate.
"""

from __future__ import annotations

import hashlib
import logging

from aiortc import rtcdtlstransport
from OpenSSL import crypto

log = logging.getLogger("rm.liveview.dtls")

HASHES = {"sha-1": "sha1", "sha-224": "sha224", "sha-256": "sha256", "sha-384": "sha384", "sha-512": "sha512"}


def fingerprint(der: bytes, algorithm: str) -> str | None:
    name = HASHES.get(algorithm.lower())
    return ":".join(f"{b:02X}" for b in hashlib.new(name, der).digest()) if name else None


def validate_peer_identity(self, remoteParameters) -> None:
    """RTCDtlsTransport._validate_peer_identity, same rules: at least one fingerprint
    with a supported algorithm, and every supported one must match."""
    der = crypto.dump_certificate(crypto.FILETYPE_ASN1, self._ssl.get_peer_certificate())
    supported = valid = 0
    for remote in remoteParameters.fingerprints:
        ours = fingerprint(der, remote.algorithm)
        if ours is not None:
            supported += 1
            valid += remote.value.upper() == ours
    if not supported or valid != supported:
        log.warning("DTLS fingerprint mismatch: the peer is not who the offer said")
        self._set_state(rtcdtlstransport.State.FAILED)


def install() -> None:
    rtcdtlstransport.RTCDtlsTransport._validate_peer_identity = validate_peer_identity

"""A software WebAuthn authenticator for tests: ES256, attestation "none", a counter that grows by one.

``register(options_json)`` answers ``navigator.credentials.create`` and ``assert_(options_json)``
answers ``navigator.credentials.get`` with the JSON a browser sends (base64url fields). The key,
credential id and user handle live on the instance, so one object is one passkey.
"""

import base64
import hashlib
import json
import os
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

UP, UV, AT = 0x01, 0x04, 0x40


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SoftAuthenticator:
    def __init__(self, rp_id, origin):
        self.rp_id, self.origin = rp_id, origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.user_handle = b""
        self.sign_count = 0

    def _cose_key(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps(
            {
                1: 2,
                3: -7,
                -1: 1,
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    def _client_data(self, kind, challenge_b64) -> bytes:
        return json.dumps(
            {
                "type": kind,
                "challenge": challenge_b64,
                "origin": self.origin,
                "crossOrigin": False,
            },
            separators=(",", ":"),
        ).encode("utf-8")

    def register(self, options_json, *, uv=True) -> str:
        options = (
            json.loads(options_json) if isinstance(options_json, str) else options_json
        )
        self.user_handle = unb64url(options["user"]["id"])
        client_data = self._client_data("webauthn.create", options["challenge"])
        auth_data = (
            hashlib.sha256(options["rp"]["id"].encode()).digest()
            + bytes([UP | AT | (UV if uv else 0)])
            + struct.pack(">I", self.sign_count)
            + b"\x00" * 16
            + struct.pack(">H", len(self.credential_id))
            + self.credential_id
            + self._cose_key()
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return json.dumps(
            {
                "id": b64url(self.credential_id),
                "rawId": b64url(self.credential_id),
                "type": "public-key",
                "response": {
                    "clientDataJSON": b64url(client_data),
                    "attestationObject": b64url(attestation),
                    "transports": ["internal"],
                },
                "clientExtensionResults": {},
                "authenticatorAttachment": "platform",
            }
        )

    def assert_(self, options_json, credential_id=None, *, uv=True) -> str:
        options = (
            json.loads(options_json) if isinstance(options_json, str) else options_json
        )
        self.sign_count += 1
        client_data = self._client_data("webauthn.get", options["challenge"])
        auth_data = (
            hashlib.sha256(options.get("rpId", self.rp_id).encode()).digest()
            + bytes([UP | (UV if uv else 0)])
            + struct.pack(">I", self.sign_count)
        )
        signature = self.key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        cred = credential_id or self.credential_id
        return json.dumps(
            {
                "id": b64url(cred),
                "rawId": b64url(cred),
                "type": "public-key",
                "response": {
                    "clientDataJSON": b64url(client_data),
                    "authenticatorData": b64url(auth_data),
                    "signature": b64url(signature),
                    "userHandle": b64url(self.user_handle),
                },
                "clientExtensionResults": {},
                "authenticatorAttachment": "platform",
            }
        )

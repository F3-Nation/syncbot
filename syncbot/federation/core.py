"""Cross-instance federation for SyncBot.

Provides:

* **Ed25519 signing and verification** of inter-instance HTTP requests.
* **Auto-generated keypair** created on first boot and stored in the DB.
* **HTTP client** for pushing events (messages, edits, deletes, reactions,
  user-directory exchanges) to federated workspaces.
* **Connection code** generation and parsing (encodes webhook URL + code +
  instance ID + public key + primary Team ID; the primary Workspace name is
  display-only).
"""

import base64
import hashlib
import ipaddress
import json
import os
import secrets
import time
from datetime import UTC, datetime
from urllib.parse import urlparse

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
    load_pem_public_key,
)

import constants
from db import DbManager, schemas
from helpers.encryption import decrypt_bot_token, encrypt_bot_token
from logger import log_debug, log_error, log_info, log_warning

FEDERATION_USER_AGENT = "SyncBot-Federation/1.0"

# ---------------------------------------------------------------------------
# Instance identity
# ---------------------------------------------------------------------------

_INSTANCE_ID: str | None = None
_LEGACY_INSTANCE_ID_WARNED = False


def public_key_fingerprint(public_key_pem: str) -> str:
    """Return the SHA-256 hex fingerprint of an Ed25519 public key (64 chars).

    Hashes the raw 32-byte key, not the PEM wrapping, so the id stays stable
    across PEM header or line-wrap differences.
    """
    public_key = load_pem_public_key(public_key_pem.encode())
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("not_ed25519_public_key")
    raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()


def instance_id_matches_public_key(instance_id: str, public_key_pem: str) -> bool:
    """Return True when *instance_id* is this key's fingerprint."""
    if not instance_id:
        return False
    try:
        return instance_id == public_key_fingerprint(public_key_pem)
    except Exception:
        return False


def get_instance_id() -> str:
    """Return this instance's federation id: SHA-256 of the Ed25519 public key.

    The value is the PK of the self ``instances`` row. Leftover
    ``SYNCBOT_INSTANCE_ID`` is ignored and warned.
    """
    global _INSTANCE_ID
    _warn_legacy_instance_id_env()
    if _INSTANCE_ID:
        return _INSTANCE_ID
    _, public_pem = get_or_create_instance_keypair()
    fingerprint = public_key_fingerprint(public_pem)
    _INSTANCE_ID = fingerprint
    return _INSTANCE_ID


def _warn_legacy_instance_id_env() -> None:
    global _LEGACY_INSTANCE_ID_WARNED
    if _LEGACY_INSTANCE_ID_WARNED:
        return
    raw = os.environ.get(constants.SYNCBOT_INSTANCE_ID)
    if raw is None or raw.strip() == "":
        return
    _LEGACY_INSTANCE_ID_WARNED = True
    log_warning("legacy_env_ignored", env=constants.SYNCBOT_INSTANCE_ID)


def _self_instance_rows() -> list:
    return DbManager.find_records(
        schemas.Instance,
        [schemas.Instance.private_key_encrypted.isnot(None)],
    )


def _upgrade_instance_id(record: schemas.Instance, instance_id: str) -> schemas.Instance:
    """Move an Instance and its runtime references to a fingerprint PK."""
    if record.instance_id == instance_id:
        return record

    replacement = schemas.Instance(
        instance_id=instance_id,
        public_key=record.public_key,
        private_key_encrypted=record.private_key_encrypted,
        webhook_url=record.webhook_url,
        status=record.status,
        trust_status=record.trust_status,
        name=record.name,
        primary_team_id=record.primary_team_id,
        primary_workspace_name=record.primary_workspace_name,
        created_at=record.created_at,
        updated_at=datetime.now(UTC).replace(tzinfo=None),
    )
    DbManager.create_record(replacement)
    DbManager.update_records(
        schemas.Workspace,
        [schemas.Workspace.instance_id == record.instance_id],
        {schemas.Workspace.instance_id: instance_id},
    )
    DbManager.update_records(
        schemas.FederationWorkspaceAllowlist,
        [schemas.FederationWorkspaceAllowlist.instance_id == record.instance_id],
        {schemas.FederationWorkspaceAllowlist.instance_id: instance_id},
    )
    DbManager.update_records(
        schemas.FederationPendingStub,
        [schemas.FederationPendingStub.instance_id == record.instance_id],
        {schemas.FederationPendingStub.instance_id: instance_id},
    )
    DbManager.delete_records(
        schemas.Instance,
        [schemas.Instance.instance_id == record.instance_id],
    )
    log_info("instance_id_upgraded", old_instance_id=record.instance_id, instance_id=instance_id)
    return DbManager.get_record(schemas.Instance, id=instance_id)


def get_public_url(context: dict | None = None) -> str:
    """Return the public base URL of this instance (no trailing slash).

    Same origin Slack already uses for events: the Host of incoming requests,
    via :func:`helpers.oauth.get_public_base_url`.
    """
    from helpers.oauth import get_public_base_url

    url = get_public_base_url(context) or ""
    if not url:
        log_warning("federation_public_base_unknown")
    return url


def federation_endpoint_url(context: dict | None = None) -> str:
    """Return this instance's full federation endpoint (origin + mount path).

    This is the ``webhook_url`` in connection codes and pair
    payloads. Peers store it verbatim and append resource subpaths such as
    ``/message`` — they do not assume the mount path. Returns ``""`` when the
    public origin is unknown.
    """
    base = get_public_url(context)
    if not base:
        return ""
    return base.rstrip("/") + constants.FEDERATION_API_BASE_PATH


# ---------------------------------------------------------------------------
# Ed25519 keypair management
# ---------------------------------------------------------------------------

_cached_private_key = None
_cached_public_pem: str | None = None


def get_or_create_instance_keypair():
    """Return this instance's Ed25519 (private_key, public_key_pem).

    Auto-generates and persists the keypair on first call when the self
    ``instances`` row is missing. Never rotates an existing pair. The private
    key is Fernet-encrypted at rest. Runs whether or not federation is enabled.
    """
    global _cached_private_key, _cached_public_pem
    if _cached_private_key and _cached_public_pem:
        return _cached_private_key, _cached_public_pem

    existing = _self_instance_rows()
    if existing:
        self_record = existing[0]
        fingerprint = public_key_fingerprint(self_record.public_key)
        if self_record.instance_id != fingerprint:
            self_record = _upgrade_instance_id(self_record, fingerprint)
        private_pem = decrypt_bot_token(self_record.private_key_encrypted)
        private_key = load_pem_private_key(private_pem.encode(), password=None)
        _cached_private_key = private_key
        _cached_public_pem = self_record.public_key
        return private_key, self_record.public_key

    private_key = Ed25519PrivateKey.generate()
    public_pem = private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    private_pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    fingerprint = public_key_fingerprint(public_pem)

    record = schemas.Instance(
        instance_id=fingerprint,
        public_key=public_pem,
        private_key_encrypted=encrypt_bot_token(private_pem),
        webhook_url=None,
        status="active",
        trust_status="trusted",
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )
    DbManager.create_record(record)

    _cached_private_key = private_key
    _cached_public_pem = public_pem
    log_info("instance_keypair_generated")
    return private_key, public_pem


# ---------------------------------------------------------------------------
# Ed25519 signing / verification
# ---------------------------------------------------------------------------

_TIMESTAMP_MAX_AGE = 300  # 5 minutes


def federation_sign(body: str) -> tuple[str, str]:
    """Sign *body* with this instance's Ed25519 private key.

    Returns ``(signature_b64, timestamp_str)``.
    """
    private_key, _ = get_or_create_instance_keypair()
    ts = str(int(time.time()))
    signing_str = f"{ts}:{body}".encode()
    sig = private_key.sign(signing_str)
    return base64.b64encode(sig).decode(), ts


def federation_verify(body: str, signature_b64: str, timestamp: str, public_key_pem: str) -> bool:
    """Verify an incoming federation request using the sender's public key.

    Returns *True* if the signature is valid and the timestamp is fresh.
    """
    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        return False

    if abs(time.time() - ts_int) > _TIMESTAMP_MAX_AGE:
        log_warning("federation_verify: timestamp too old/future", ts=timestamp)
        return False

    try:
        public_key = load_pem_public_key(public_key_pem.encode())
        signing_str = f"{timestamp}:{body}".encode()
        public_key.verify(base64.b64decode(signature_b64), signing_str)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def sign_body(body: str) -> str:
    """Sign *body* only (no timestamp). Used for migration export integrity."""
    private_key, _ = get_or_create_instance_keypair()
    sig = private_key.sign(body.encode())
    return base64.b64encode(sig).decode()


def verify_body(body: str, signature_b64: str, public_key_pem: str) -> bool:
    """Verify a signature over *body* (no timestamp). Used for migration import."""
    try:
        public_key = load_pem_public_key(public_key_pem.encode())
        public_key.verify(base64.b64decode(signature_b64), body.encode())
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# URL validation (SSRF protection)
# ---------------------------------------------------------------------------

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def validate_webhook_url(url: str) -> bool:
    """Return *True* if *url* is safe to use as a federation webhook target.

    Rejects private/loopback IPs (SSRF protection) and requires HTTPS in
    production.  HTTP is allowed only when ``LOCAL_DEVELOPMENT`` is true.
    """
    if not url:
        return False

    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if constants.LOCAL_DEVELOPMENT:
        if parsed.scheme not in ("http", "https"):
            return False
    else:
        if parsed.scheme != "https":
            return False

    hostname = parsed.hostname
    if not hostname:
        return False

    import socket

    try:
        addr_infos = socket.getaddrinfo(hostname, None)
        for info in addr_infos:
            addr = ipaddress.ip_address(info[4][0])
            for net in _PRIVATE_NETWORKS:
                if addr in net:
                    log_warning("federation_ssrf_blocked", url=url, resolved_ip=str(addr))
                    return False
    except (socket.gaierror, ValueError):
        return False

    return True


# ---------------------------------------------------------------------------
# Connection code generation / parsing
# ---------------------------------------------------------------------------


_CONNECTION_CODE_SIGNED_KEYS = ("code", "webhook_url", "instance_id", "public_key")
_CONNECTION_CODE_OPTIONAL_SIGNED_KEYS = ("label", "primary_team_id")


def this_primary_team_id() -> str | None:
    """This instance's primary Workspace Slack Team ID (stable). Independent of the allowlist."""
    team_id = (os.environ.get(constants.PRIMARY_WORKSPACE) or "").strip()
    return team_id or None


def this_primary_workspace_name() -> str | None:
    """This instance's primary Workspace name for display only. Names can change; do not sign this."""
    team_id = this_primary_team_id()
    if not team_id:
        return None
    matches = DbManager.find_records(
        schemas.Workspace,
        [schemas.Workspace.team_id == team_id, schemas.Workspace.deleted_at.is_(None)],
    )
    if not matches:
        return None
    return (matches[0].workspace_name or "").strip() or None


def _connection_payload_canonical(payload: dict) -> str:
    """Canonical JSON of the signed connection-code fields (excludes ``sig``)."""
    body = {key: payload[key] for key in _CONNECTION_CODE_SIGNED_KEYS}
    for key in _CONNECTION_CODE_OPTIONAL_SIGNED_KEYS:
        if key in payload:
            body[key] = payload[key]
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def encode_federation_connection_blob(
    webhook_url: str,
    instance_id: str,
    public_key_pem: str,
    code: str,
    *,
    label: str | None = None,
    primary_team_id: str | None = None,
    primary_workspace_name: str | None = None,
) -> str:
    """Return a signed, base64-encoded connection payload.

    Trust is the instance fingerprint. ``primary_team_id`` is signed when present
    (stable Slack Team ID). ``primary_workspace_name`` is the current name for
    display only and is not signed.
    """
    payload = {
        "code": code,
        "webhook_url": webhook_url,
        "instance_id": instance_id,
        "public_key": public_key_pem,
    }
    if label:
        payload["label"] = label
    team_id = (primary_team_id or "").strip()
    if team_id:
        payload["primary_team_id"] = team_id
    name = (primary_workspace_name or "").strip()
    if name:
        payload["primary_workspace_name"] = name[:200]
    payload["sig"] = sign_body(_connection_payload_canonical(payload))
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def generate_federation_code(
    label: str | None = None,
    *,
    subject_team_id: str | None = None,
    context: dict | None = None,
    workspace_ids: list[int] | None = None,
) -> tuple[str, str]:
    """Generate a pairing-only federation connection code.

    Returns ``(encoded_payload, raw_code)``. Does **not** create a WorkspaceGroup.
    ``subject_team_id`` null = operator External Connections code; set = workspace-
    scoped migration code. Both appear as waiting External Connections until
    consumed or expired. Raises ``ValueError`` if this instance's public URL is
    unknown. Only primary-workspace admins should call this. Allowed Workspaces
    stay on the pairing row; the signed blob includes the primary Team ID.
    """
    endpoint = federation_endpoint_url(context)
    if not endpoint:
        raise ValueError("public_url_unknown")
    instance_id = get_instance_id()
    _, public_key_pem = get_or_create_instance_keypair()

    raw_code = "FED-" + secrets.token_hex(4).upper()
    friendly = (label or "External connection")[:200]
    encoded = encode_federation_connection_blob(
        endpoint,
        instance_id,
        public_key_pem,
        raw_code,
        label=friendly,
        primary_team_id=this_primary_team_id(),
        primary_workspace_name=this_primary_workspace_name(),
    )

    allowed = None
    if workspace_ids:
        allowed = json.dumps(sorted({int(wid) for wid in workspace_ids if wid}))

    now = datetime.now(UTC).replace(tzinfo=None)
    DbManager.create_record(
        schemas.FederationPairingCode(
            code=raw_code,
            created_at=now,
            subject_team_id=(str(subject_team_id).strip() or None) if subject_team_id else None,
            label=friendly,
            allowed_workspace_ids=allowed,
        )
    )
    return encoded, raw_code


def delete_pairing_code(pairing_id: int) -> None:
    """Delete a pairing code after clearing any pairing-request pointer.

    ``federation_pairing_requests.pairing_code_id`` has no ``ON DELETE SET
    NULL``, so a migration-requested waiting code cannot be deleted until that
    column is nulled.
    """
    DbManager.update_records(
        schemas.FederationPairingRequest,
        [schemas.FederationPairingRequest.pairing_code_id == pairing_id],
        {schemas.FederationPairingRequest.pairing_code_id: None},
    )
    DbManager.delete_records(
        schemas.FederationPairingCode,
        [schemas.FederationPairingCode.id == pairing_id],
    )


def parse_federation_code(encoded: str) -> dict | None:
    """Decode and verify a federation connection payload.

    Returns the signed fields (``code``, ``webhook_url``, ``instance_id``,
    ``public_key``, optional ``label`` / ``primary_team_id``, and ``sig``)
    plus unsigned display ``primary_workspace_name``.
    Returns *None* if the payload is invalid, unsigned, tampered, or the
    webhook URL fails SSRF checks.
    """
    try:
        decoded = base64.urlsafe_b64decode(encoded.encode()).decode()
        payload = json.loads(decoded)
        required = ("code", "webhook_url", "instance_id", "public_key", "sig")
        if not all(k in payload and payload[k] for k in required):
            return None
        if not verify_body(_connection_payload_canonical(payload), payload["sig"], payload["public_key"]):
            log_warning("federation_code_bad_signature")
            return None
        if not instance_id_matches_public_key(payload["instance_id"], payload["public_key"]):
            log_warning("federation_code_instance_id_mismatch")
            return None
        if not validate_webhook_url(payload["webhook_url"]):
            log_warning("federation_code_invalid_webhook")
            return None
        return payload
    except Exception:
        log_debug("decode_federation_code", reason="invalid payload")
    return None


# ---------------------------------------------------------------------------
# Federated workspace management
# ---------------------------------------------------------------------------


def get_or_create_instance(
    instance_id: str,
    webhook_url: str,
    public_key: str,
    name: str | None = None,
    *,
    primary_team_id: str | None = None,
    primary_workspace_name: str | None = None,
) -> schemas.Instance:
    """Find or create a peer ``instances`` row (never the self keypair row)."""
    fingerprint = public_key_fingerprint(public_key)
    instance_id = fingerprint
    matches = DbManager.find_records(
        schemas.Instance,
        [schemas.Instance.instance_id == instance_id],
    )
    existing = matches[0] if matches else None
    any_by_key = DbManager.find_records(
        schemas.Instance,
        [schemas.Instance.public_key == public_key],
    )
    if any(row.private_key_encrypted for row in any_by_key):
        raise ValueError("cannot_update_self_as_peer")
    if existing is None:
        by_key = [row for row in any_by_key if not row.private_key_encrypted]
        existing = by_key[0] if by_key else None
    if existing:
        # Do not overwrite the self row's private key.
        if existing.private_key_encrypted:
            raise ValueError("cannot_update_self_as_peer")
        was_inactive = getattr(existing, "status", "active") == "inactive"
        update_fields = {
            schemas.Instance.webhook_url: webhook_url,
            schemas.Instance.public_key: public_key,
            schemas.Instance.status: "active",
            schemas.Instance.updated_at: datetime.now(UTC).replace(tzinfo=None),
        }
        if name is not None:
            update_fields[schemas.Instance.name] = name
        if primary_team_id is not None:
            update_fields[schemas.Instance.primary_team_id] = primary_team_id
        if primary_workspace_name is not None:
            update_fields[schemas.Instance.primary_workspace_name] = primary_workspace_name
        if existing.instance_id != instance_id:
            existing = _upgrade_instance_id(existing, instance_id)
        DbManager.update_records(
            schemas.Instance,
            [schemas.Instance.instance_id == existing.instance_id],
            update_fields,
        )
        restored = DbManager.get_record(schemas.Instance, id=instance_id)
        if was_inactive and restored is not None:
            from helpers.workspace_kind import restore_peer_stubs

            restore_peer_stubs(restored.instance_id, source="pair")
        return restored

    peer = schemas.Instance(
        instance_id=instance_id,
        webhook_url=webhook_url,
        public_key=public_key,
        private_key_encrypted=None,
        status="active",
        trust_status="trusted",
        name=name,
        primary_team_id=primary_team_id,
        primary_workspace_name=primary_workspace_name,
        created_at=datetime.now(UTC).replace(tzinfo=None),
        updated_at=datetime.now(UTC).replace(tzinfo=None),
    )
    DbManager.create_record(peer)
    return DbManager.get_record(schemas.Instance, id=instance_id)


# ---------------------------------------------------------------------------
# HTTP client — push events to a federated workspace
# ---------------------------------------------------------------------------

_REQUEST_TIMEOUT = 15  # seconds — pair, offer, ping
_MESSAGE_TIMEOUT = 90  # seconds — envelope POST
_FILE_PART_TIMEOUT = 90  # seconds — raw file part POST
_USERS_TIMEOUT = 90  # seconds — directory exchange
_MAX_RETRIES = 3
_RETRY_BACKOFF = [1, 2, 4]  # seconds between retries


def _federation_request(
    fed_ws: schemas.Instance,
    path: str,
    payload: dict,
    method: str = "POST",
    *,
    timeout: int | None = None,
    accept_statuses: frozenset[int] | None = None,
) -> dict | None:
    """Send an authenticated JSON request to a federated workspace.

    *path* is a resource subpath (for example ``/message``) appended to the
    peer's ``webhook_url``; this client does not assume the peer's
    mount path. Signs the request with this instance's Ed25519 private key and
    retries up to :data:`_MAX_RETRIES` times on transient failures.
    """
    url = fed_ws.webhook_url.rstrip("/") + path
    body = json.dumps(payload)
    req_timeout = timeout if timeout is not None else _REQUEST_TIMEOUT
    accepted = accept_statuses or frozenset({200})

    start_time = time.time()

    for attempt in range(_MAX_RETRIES):
        try:
            sig, ts = federation_sign(body)
            headers = {
                "Content-Type": "application/json",
                "User-Agent": FEDERATION_USER_AGENT,
                "X-Federation-Signature": sig,
                "X-Federation-Timestamp": ts,
                "X-Federation-Instance": get_instance_id(),
            }
            resp = requests.request(method, url, data=body, headers=headers, timeout=req_timeout)
            elapsed = round((time.time() - start_time) * 1000, 1)

            if resp.status_code in accepted:
                log_debug(
                    "federation_request_ok", url=url, elapsed_ms=elapsed, attempts=attempt + 1, status=resp.status_code
                )
                try:
                    data = resp.json()
                except Exception as exc:
                    log_debug("federation_request", error=str(exc))
                    data = {"ok": True}
                if isinstance(data, dict):
                    data["_http_status"] = resp.status_code
                return data
            elif resp.status_code >= 500:
                log_warning(
                    "federation_request_retry",
                    url=url,
                    status=resp.status_code,
                    attempt=attempt + 1,
                    remote=fed_ws.instance_id,
                )
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_BACKOFF[attempt])
                continue
            elif resp.status_code == 401:
                log_error("federation_auth_rejected", url=url, remote=fed_ws.instance_id, reason="peer_not_trusted")
                return None
            else:
                data = None
                try:
                    parsed = resp.json()
                    if isinstance(parsed, dict):
                        data = parsed
                except Exception:
                    data = None
                error = None
                if data:
                    err = data.get("error")
                    if isinstance(err, str) and len(err) < 80:
                        error = err
                log_error(
                    "federation_request_failed",
                    url=url,
                    status=resp.status_code,
                    error=error,
                    remote=fed_ws.instance_id,
                )
                if data is None:
                    return None
                data["_http_status"] = resp.status_code
                return data
        except requests.exceptions.Timeout:
            log_warning("federation_request_timeout", url=url, attempt=attempt + 1, remote=fed_ws.instance_id)
        except requests.exceptions.ConnectionError as e:
            log_warning(
                "federation_connection_error", url=url, attempt=attempt + 1, error=str(e), remote=fed_ws.instance_id
            )
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_BACKOFF[attempt])
        except Exception as e:
            log_error("federation_request_error", url=url, error=str(e), remote=fed_ws.instance_id)
            return None

    elapsed = round((time.time() - start_time) * 1000, 1)
    log_error(
        "federation_request_exhausted", url=url, elapsed_ms=elapsed, attempts=_MAX_RETRIES, remote=fed_ws.instance_id
    )
    return None


def push_file_offer(fed_ws: schemas.Instance, sha256: str, size: int) -> dict | None:
    """Ask the peer whether it already has *sha256*; learn ``file_chunk_mb``."""
    return _federation_request(
        fed_ws,
        "/file/offer",
        {"sha256": sha256, "size": int(size)},
        timeout=_REQUEST_TIMEOUT,
    )


def push_file_part(
    fed_ws: schemas.Instance,
    *,
    sha256: str,
    part_index: int,
    total: int,
    size: int,
    payload: bytes,
) -> dict | None:
    """POST one raw file part. Retries once on 413 with the peer's file_chunk_mb."""
    import hashlib as _hashlib

    url = fed_ws.webhook_url.rstrip("/") + "/file"
    chunk_sha = _hashlib.sha256(payload).hexdigest()
    sign_body_str = f"POST:/api/federation/file:{sha256}:{part_index}:{total}:{chunk_sha}"

    def _once() -> tuple[int, dict | None]:
        sig, ts = federation_sign(sign_body_str)
        headers = {
            "Content-Type": "application/octet-stream",
            "User-Agent": FEDERATION_USER_AGENT,
            "X-Federation-Signature": sig,
            "X-Federation-Timestamp": ts,
            "X-Federation-Instance": get_instance_id(),
            "X-Federation-File-Sha256": sha256,
            "X-Federation-File-Index": str(part_index),
            "X-Federation-File-Total": str(total),
            "X-Federation-File-Size": str(int(size)),
        }
        resp = requests.post(url, data=payload, headers=headers, timeout=_FILE_PART_TIMEOUT)
        try:
            data = resp.json()
        except Exception:
            data = {"ok": resp.status_code == 200}
        if isinstance(data, dict):
            data["_http_status"] = resp.status_code
        return resp.status_code, data if isinstance(data, dict) else None

    try:
        status, data = _once()
        if status == 200:
            return data
        if status == 413 and isinstance(data, dict) and data.get("file_chunk_mb") is not None:
            return data
        if status >= 500:
            time.sleep(_RETRY_BACKOFF[0])
            status, data = _once()
            if status == 200:
                return data
        return data
    except Exception as exc:
        log_warning("push_file_part_failed", error=str(exc), sha256=sha256)
        return None


def push_message(fed_ws: schemas.Instance, payload: dict) -> dict | None:
    """Forward a message (new post, thread reply) to a federated workspace."""
    return _federation_request(
        fed_ws,
        "/message",
        payload,
        timeout=_MESSAGE_TIMEOUT,
        accept_statuses=frozenset({200, 409}),
    )


def push_edit(fed_ws: schemas.Instance, payload: dict) -> dict | None:
    """Forward a message edit to a federated workspace."""
    return _federation_request(
        fed_ws,
        "/message/edit",
        payload,
        timeout=_MESSAGE_TIMEOUT,
        accept_statuses=frozenset({200, 409}),
    )


def push_delete(fed_ws: schemas.Instance, payload: dict) -> dict | None:
    """Forward a message deletion to a federated workspace."""
    return _federation_request(
        fed_ws,
        "/message/delete",
        payload,
        timeout=_MESSAGE_TIMEOUT,
        accept_statuses=frozenset({200, 409}),
    )


def push_reaction(fed_ws: schemas.Instance, payload: dict) -> dict | None:
    """Forward a reaction add/remove to a federated workspace."""
    return _federation_request(
        fed_ws,
        "/message/react",
        payload,
        timeout=_MESSAGE_TIMEOUT,
        accept_statuses=frozenset({200, 409}),
    )


def _users_batch_for_cap(users: list, start: int, chunk_mb: int, base: dict) -> tuple[list, int]:
    """Take a JSON-sized batch of *users* from *start*. Returns (batch, next_start)."""
    if start >= len(users):
        return [], start
    if chunk_mb == 0:
        return users[start:], len(users)
    cap = chunk_mb * 1024 * 1024
    batch: list = []
    index = start
    while index < len(users):
        candidate = batch + [users[index]]
        probe = json.dumps({**base, "users": candidate, "offset": 0})
        if batch and len(probe.encode()) > cap:
            break
        batch.append(users[index])
        index += 1
    return batch, index


def push_users(fed_ws: schemas.Instance, payload: dict) -> dict | None:
    """Exchange user directory with a federated workspace, paging to the peer JSON cap."""
    users = list(payload.get("users") or [])
    base = {key: value for key, value in payload.items() if key != "users"}
    send_from = 0
    recv_offset = 0
    peer_mb: int | None = None
    collected: list = []
    last: dict | None = None
    while True:
        cap_mb = constants.LEGACY_FEDERATION_JSON_CHUNK_MB if peer_mb is None else peer_mb
        if send_from < len(users):
            batch, send_from = _users_batch_for_cap(users, send_from, cap_mb, base)
            if not batch:
                log_error("federation_users_page_too_large", peer_instance_id=fed_ws.instance_id)
                return None
        else:
            batch = []
        page = {**base, "users": batch, "offset": recv_offset}
        last = _federation_request(fed_ws, "/users", page, timeout=_USERS_TIMEOUT)
        if not last:
            return None
        raw_mb = last.get("json_chunk_mb")
        if raw_mb is not None:
            try:
                peer_mb = int(raw_mb)
            except (TypeError, ValueError):
                peer_mb = constants.LEGACY_FEDERATION_JSON_CHUNK_MB
        collected.extend(last.get("users") or [])
        nxt = last.get("next_offset")
        if nxt is None:
            if send_from >= len(users):
                break
            recv_offset = 0
        else:
            try:
                recv_offset = int(nxt)
            except (TypeError, ValueError):
                break
    merged = dict(last)
    merged["users"] = collected
    return merged


def push_teams(fed_ws: schemas.Instance, payload: dict) -> dict | None:
    """Advertise this instance's Workspaces so the peer can create/heal stubs.

    *payload* carries ``workspaces`` (``team_id`` + display ``name``),
    ``primary_team_id``, and ``primary_workspace_name``. The peer heals each
    listed team and pauses this peer's stubs that left the allowlist. Trust
    stays the fingerprint. ``409 owner_on_connection`` is accepted.
    """
    return _federation_request(
        fed_ws,
        "/teams",
        payload,
        timeout=_REQUEST_TIMEOUT,
        accept_statuses=frozenset({200, 409}),
    )


def allowed_workspace_entries(instance_id: str) -> list[dict]:
    """Local Workspaces currently allowed on *instance_id*, keyed by Slack team_id."""
    instance_id = (instance_id or "").strip()
    if not instance_id:
        return []
    rows = DbManager.find_records(
        schemas.FederationWorkspaceAllowlist,
        [schemas.FederationWorkspaceAllowlist.instance_id == instance_id],
    )
    entries: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not row.workspace_id:
            continue
        matches = DbManager.find_records(
            schemas.Workspace,
            [
                schemas.Workspace.id == row.workspace_id,
                schemas.Workspace.deleted_at.is_(None),
            ],
        )
        workspace = matches[0] if matches else None
        if workspace is None:
            continue
        team_id = (workspace.team_id or "").strip()
        if not team_id or team_id in seen:
            continue
        seen.add(team_id)
        entries.append(
            {
                "team_id": team_id,
                "name": (workspace.workspace_name or "").strip() or team_id,
            }
        )
    return sorted(entries, key=lambda item: item["team_id"])


def teams_allowlist_payload(instance_id: str) -> dict:
    """Body for ``POST /teams``: allowlist plus primary Team ID and display name."""
    entries = allowed_workspace_entries(instance_id)
    payload: dict = {
        "workspaces": entries,
    }
    team_id = this_primary_team_id()
    if team_id:
        payload["primary_team_id"] = team_id
    name = this_primary_workspace_name()
    if name:
        payload["primary_workspace_name"] = name
    return payload


def push_allowed_workspaces(fed_ws: schemas.Instance) -> dict | None:
    """Push this instance's current allowlist and primary Workspace name to *fed_ws*."""
    return push_teams(fed_ws, teams_allowlist_payload(fed_ws.instance_id))


def refresh_instance() -> None:
    """Keep-warm / Health / Refresh pulse: purge stale rows and push peers.

    Never raises. Does not ``views.publish`` Home (post-deploy ready does that).
    """
    try:
        from helpers.notifications import purge_stale_soft_deletes

        purge_stale_soft_deletes()
    except Exception:
        pass
    refresh_federation_allowlists()


def refresh_federation_allowlists() -> None:
    """Best-effort allowlist refresh to every trusted peer (keep-warm).

    There is no separate federation heartbeat. Keep-warm (EventBridge or
    ``GET /health``) is the periodic pulse; this also pushes allowed Workspaces
    so Remote Workspaces and primary Workspace names stay fresh, and a
    group/sync snapshot so a newly connected instance receives members and
    Channels. Never raises.
    """
    try:
        from helpers import federation_enabled

        if not federation_enabled():
            return
    except Exception:
        return
    try:
        peers = DbManager.find_records(
            schemas.Instance,
            [
                schemas.Instance.private_key_encrypted.is_(None),
                schemas.Instance.status == "active",
                schemas.Instance.trust_status == "trusted",
            ],
        )
    except Exception:
        return
    for peer in peers:
        try:
            result = push_allowed_workspaces(peer)
            ok = bool(result and result.get("ok"))
            emit = log_warning if not ok else log_debug
            emit(
                "push_teams",
                peer_instance_id=peer.instance_id,
                ok=ok,
                source="keep_warm",
            )
        except Exception:
            log_error("push_teams", peer_instance_id=peer.instance_id, ok=False, source="keep_warm")
        try:
            from federation.replicate import replicate_peer_snapshot

            replicate_peer_snapshot(peer)
        except Exception:
            log_error("federation_snapshot", peer_instance_id=peer.instance_id, ok=False, source="keep_warm")


def push_group_upsert(fed_ws: schemas.Instance, payload: dict) -> dict | None:
    return _federation_request(fed_ws, "/group-upsert", payload, timeout=_REQUEST_TIMEOUT)


def push_group_invite(fed_ws: schemas.Instance, payload: dict) -> dict | None:
    return _federation_request(fed_ws, "/group-invite", payload, timeout=_REQUEST_TIMEOUT)


def push_group_leave(fed_ws: schemas.Instance, payload: dict) -> dict | None:
    return _federation_request(fed_ws, "/group-leave", payload, timeout=_REQUEST_TIMEOUT)


def push_sync_upsert(fed_ws: schemas.Instance, payload: dict) -> dict | None:
    return _federation_request(fed_ws, "/sync-upsert", payload, timeout=_REQUEST_TIMEOUT)


def push_sync_channel_upsert(fed_ws: schemas.Instance, payload: dict) -> dict | None:
    return _federation_request(fed_ws, "/sync-channel-upsert", payload, timeout=_REQUEST_TIMEOUT)


def push_sync_channel_remove(fed_ws: schemas.Instance, payload: dict) -> dict | None:
    return _federation_request(fed_ws, "/sync-channel-remove", payload, timeout=_REQUEST_TIMEOUT)


def initiate_federation_connect(
    remote_url: str,
    code: str,
    *,
    team_id: str | None = None,
    workspace_name: str | None = None,
    context: dict | None = None,
) -> dict | None:
    """Call the remote instance's pair endpoint.

    *remote_url* is the ``webhook_url`` from the connection code — the peer's
    full federation endpoint — and this appends ``/pair`` to it rather than
    assuming a mount path. Signs the request with this instance's Ed25519
    private key so the receiver can verify we control the keypair in
    the connection code. Optionally sends team_id and workspace_name so the
    remote (Instance A) can convert a matching local install into a stub.
    """
    endpoint = federation_endpoint_url(context)
    if not endpoint:
        log_error("federation_pair", direction="outbound", ok=False, reason="no_public_url")
        return None
    if not validate_webhook_url(remote_url):
        log_error("federation_pair", direction="outbound", ok=False, reason="invalid_remote_url")
        return None

    _, public_key_pem = get_or_create_instance_keypair()

    url = remote_url.rstrip("/") + "/pair"
    payload = {
        "code": code,
        "webhook_url": endpoint,
        "instance_id": get_instance_id(),
        "public_key": public_key_pem,
    }
    if team_id:
        payload["team_id"] = team_id
    if workspace_name:
        payload["workspace_name"] = workspace_name
    body = json.dumps(payload)
    sig, ts = federation_sign(body)

    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.post(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": FEDERATION_USER_AGENT,
                    "X-Federation-Signature": sig,
                    "X-Federation-Timestamp": ts,
                    "X-Federation-Instance": get_instance_id(),
                },
                timeout=_REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                log_info(
                    "federation_pair",
                    direction="outbound",
                    ok=True,
                    status=200,
                    peer_hostname=urlparse(remote_url).hostname,
                )
                return resp.json()
            elif resp.status_code >= 500:
                log_warning("federation_pair_retry", url=url, status=resp.status_code, attempt=attempt + 1)
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_BACKOFF[attempt])
                continue
            else:
                reason = None
                try:
                    err = (resp.json() or {}).get("error")
                    if isinstance(err, str) and err.strip():
                        reason = err.strip()[:80]
                except Exception:
                    reason = None
                log_error(
                    "federation_pair",
                    direction="outbound",
                    ok=False,
                    status=resp.status_code,
                    reason=reason,
                    peer_hostname=urlparse(remote_url).hostname,
                )
                return None
        except requests.exceptions.ConnectionError as e:
            log_warning("federation_pair_connection_error", url=url, attempt=attempt + 1, error=str(e))
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_BACKOFF[attempt])
        except requests.exceptions.Timeout:
            log_warning("federation_pair_timeout", url=url, attempt=attempt + 1)
        except Exception as e:
            log_error("federation_pair_error", url=url, error=str(e))
            return None

    log_error(
        "federation_pair",
        direction="outbound",
        ok=False,
        reason="exhausted",
        peer_hostname=urlparse(remote_url).hostname,
    )
    return None

"""Cross-instance federation for SyncBot.

Re-exports public API from :mod:`federation.core` and
:mod:`federation.api` so callers can use ``import federation``
and access all federation functions directly.
"""

from federation.core import (
    FEDERATION_USER_AGENT,
    build_delete_payload,
    build_edit_payload,
    build_message_payload,
    build_reaction_payload,
    federation_endpoint_url,
    federation_sign,
    federation_verify,
    generate_federation_code,
    get_instance_id,
    get_or_create_federated_workspace,
    get_or_create_instance_keypair,
    get_public_url,
    initiate_federation_connect,
    instance_id_matches_public_key,
    parse_federation_code,
    public_key_fingerprint,
    push_delete,
    push_edit,
    push_message,
    push_reaction,
    push_users,
    sign_body,
    validate_webhook_url,
    verify_body,
)
from federation.deliver import deliver_remote, federation_image_payloads

__all__ = [
    "FEDERATION_USER_AGENT",
    "build_delete_payload",
    "build_edit_payload",
    "build_message_payload",
    "build_reaction_payload",
    "deliver_remote",
    "federation_endpoint_url",
    "federation_image_payloads",
    "federation_sign",
    "federation_verify",
    "generate_federation_code",
    "get_instance_id",
    "get_or_create_federated_workspace",
    "get_or_create_instance_keypair",
    "get_public_url",
    "initiate_federation_connect",
    "instance_id_matches_public_key",
    "parse_federation_code",
    "public_key_fingerprint",
    "push_delete",
    "push_edit",
    "push_message",
    "push_reaction",
    "push_users",
    "sign_body",
    "validate_webhook_url",
    "verify_body",
]

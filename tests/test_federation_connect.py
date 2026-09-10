"""Federation connection-code signing, Host resolution, and instance-id fingerprint."""

from __future__ import annotations

import base64
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from federation import core as federation_core  # noqa: E402


def _keypair():
    private = Ed25519PrivateKey.generate()
    public_pem = (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private, public_pem


class TestConnectionCodeSignVerify:
    def test_roundtrip_signed_blob(self):
        private, public_pem = _keypair()
        with (
            patch.object(federation_core, "get_or_create_instance_keypair", return_value=(private, public_pem)),
            patch.object(federation_core, "validate_webhook_url", return_value=True),
        ):
            encoded = federation_core.encode_federation_connection_blob(
                "https://peer.example/api/federation",
                "inst-1",
                public_pem,
                "FED-ABCD",
            )
            parsed = federation_core.parse_federation_code(encoded)
        assert parsed is not None
        assert parsed["webhook_url"] == "https://peer.example/api/federation"
        assert parsed["code"] == "FED-ABCD"
        assert parsed["public_key"] == public_pem
        assert parsed["sig"]

    def test_tampered_url_fails_verify(self):
        private, public_pem = _keypair()
        with (
            patch.object(federation_core, "get_or_create_instance_keypair", return_value=(private, public_pem)),
            patch.object(federation_core, "validate_webhook_url", return_value=True),
        ):
            encoded = federation_core.encode_federation_connection_blob(
                "https://peer.example/api/federation",
                "inst-1",
                public_pem,
                "FED-ABCD",
            )
            payload = json.loads(base64.urlsafe_b64decode(encoded.encode()).decode())
            payload["webhook_url"] = "https://evil.example/api/federation"
            tampered = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
            assert federation_core.parse_federation_code(tampered) is None

    def test_unsigned_blob_is_rejected(self):
        payload = {
            "code": "FED-ABCD",
            "webhook_url": "https://peer.example/api/federation",
            "instance_id": "inst-1",
            "public_key": "-----BEGIN PUBLIC KEY-----\nMAo=\n-----END PUBLIC KEY-----",
        }
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        assert federation_core.parse_federation_code(encoded) is None

    def test_generate_raises_when_public_url_unknown(self):
        with (
            patch.object(federation_core, "get_public_url", return_value=""),
            pytest.raises(ValueError, match="public_url_unknown"),
        ):
            federation_core.generate_federation_code(1, context={})

    def test_initiate_returns_none_when_public_url_unknown(self):
        with patch.object(federation_core, "get_public_url", return_value=""):
            assert federation_core.initiate_federation_connect("https://peer.example", "FED-1") is None


class TestInstanceIdFingerprint:
    def test_fingerprint_is_sha256_of_raw_key_not_pem(self):
        import hashlib

        private, public_pem = _keypair()
        raw = private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        fingerprint = federation_core.public_key_fingerprint(public_pem)
        assert len(fingerprint) == 64
        assert fingerprint == hashlib.sha256(raw).hexdigest()
        assert fingerprint != hashlib.sha256(public_pem.encode()).hexdigest()

    def test_get_instance_id_derives_from_key_and_persists(self, monkeypatch):
        federation_core._INSTANCE_ID = None
        federation_core._LEGACY_INSTANCE_ID_WARNED = False
        monkeypatch.delenv("SYNCBOT_INSTANCE_ID", raising=False)
        private, public_pem = _keypair()
        expected = federation_core.public_key_fingerprint(public_pem)
        row = SimpleNamespace(id=7, instance_id=None)
        with (
            patch.object(federation_core, "get_or_create_instance_keypair", return_value=(private, public_pem)),
            patch.object(federation_core.DbManager, "find_records", return_value=[row]),
            patch.object(federation_core.DbManager, "update_records") as update,
        ):
            assert federation_core.get_instance_id() == expected
        update.assert_called_once()
        federation_core._INSTANCE_ID = None

    def test_legacy_env_is_ignored_and_warned_once(self, monkeypatch, caplog):
        federation_core._INSTANCE_ID = None
        federation_core._LEGACY_INSTANCE_ID_WARNED = False
        monkeypatch.setenv("SYNCBOT_INSTANCE_ID", "env-instance-id")
        private, public_pem = _keypair()
        expected = federation_core.public_key_fingerprint(public_pem)
        row = SimpleNamespace(id=7, instance_id="stored-uuid")
        with (
            patch.object(federation_core, "get_or_create_instance_keypair", return_value=(private, public_pem)),
            patch.object(federation_core.DbManager, "find_records", return_value=[row]),
            patch.object(federation_core.DbManager, "update_records"),
            caplog.at_level("WARNING", logger="federation.core"),
        ):
            assert federation_core.get_instance_id() == expected
            assert federation_core.get_instance_id() == expected
        warnings = [r.message for r in caplog.records if "SYNCBOT_INSTANCE_ID is ignored" in r.message]
        assert len(warnings) == 1
        federation_core._INSTANCE_ID = None

    def test_stored_uuid_is_replaced_with_fingerprint(self, monkeypatch):
        federation_core._INSTANCE_ID = None
        federation_core._LEGACY_INSTANCE_ID_WARNED = False
        monkeypatch.delenv("SYNCBOT_INSTANCE_ID", raising=False)
        private, public_pem = _keypair()
        expected = federation_core.public_key_fingerprint(public_pem)
        row = SimpleNamespace(id=7, instance_id="stored-uuid")
        with (
            patch.object(federation_core, "get_or_create_instance_keypair", return_value=(private, public_pem)),
            patch.object(federation_core.DbManager, "find_records", return_value=[row]),
            patch.object(federation_core.DbManager, "update_records") as update,
        ):
            assert federation_core.get_instance_id() == expected
        update.assert_called_once()
        fields = update.call_args.args[2]
        from db import schemas

        assert fields[schemas.InstanceKey.instance_id] == expected
        federation_core._INSTANCE_ID = None

    def test_parse_rejects_mismatched_hex_instance_id(self):
        private, public_pem = _keypair()
        wrong = "a" * 64
        with (
            patch.object(federation_core, "get_or_create_instance_keypair", return_value=(private, public_pem)),
            patch.object(federation_core, "validate_webhook_url", return_value=True),
        ):
            encoded = federation_core.encode_federation_connection_blob(
                "https://peer.example/api/federation",
                wrong,
                public_pem,
                "FED-ABCD",
            )
            assert federation_core.parse_federation_code(encoded) is None

    def test_parse_accepts_legacy_uuid_instance_id(self):
        private, public_pem = _keypair()
        with (
            patch.object(federation_core, "get_or_create_instance_keypair", return_value=(private, public_pem)),
            patch.object(federation_core, "validate_webhook_url", return_value=True),
        ):
            encoded = federation_core.encode_federation_connection_blob(
                "https://peer.example/api/federation",
                "inst-1",
                public_pem,
                "FED-ABCD",
            )
            parsed = federation_core.parse_federation_code(encoded)
        assert parsed is not None
        assert parsed["instance_id"] == "inst-1"

    def test_reconnect_upgrades_uuid_row_by_public_key(self):
        from db import schemas

        _private, public_pem = _keypair()
        fingerprint = federation_core.public_key_fingerprint(public_pem)
        existing = SimpleNamespace(id=3, instance_id="old-uuid", public_key=public_pem)
        with (
            patch.object(federation_core.DbManager, "find_records", side_effect=[[], [existing]]),
            patch.object(federation_core.DbManager, "update_records") as update,
            patch.object(federation_core.DbManager, "get_record", return_value=existing),
        ):
            federation_core.get_or_create_federated_workspace(
                instance_id=fingerprint,
                webhook_url="https://peer.example/api/federation",
                public_key=public_pem,
            )
        fields = update.call_args.args[2]
        assert fields[schemas.FederatedWorkspace.instance_id] == fingerprint


class TestInboundInstanceIdUpgrade:
    def test_verify_updates_uuid_row_when_signature_matches(self):
        from db import schemas
        from federation import api as federation_api

        _private, public_pem = _keypair()
        fingerprint = federation_core.public_key_fingerprint(public_pem)
        existing = SimpleNamespace(id=3, instance_id="old-uuid", public_key=public_pem, status="active")
        headers = {
            "X-Federation-Signature": "sig",
            "X-Federation-Timestamp": "1",
            "X-Federation-Instance": fingerprint,
        }
        with (
            patch.object(federation_api.DbManager, "find_records", side_effect=[[], [existing]]),
            patch.object(federation_api.federation, "federation_verify", return_value=True),
            patch.object(federation_api.DbManager, "update_records") as update,
            patch.object(federation_api.DbManager, "get_record", return_value=existing),
        ):
            got = federation_api._verify_federated_request("{}", headers)
        assert got is existing
        fields = update.call_args.args[2]
        assert fields[schemas.FederatedWorkspace.instance_id] == fingerprint

    def test_verify_accepts_lowercase_function_url_headers(self):
        from federation import api as federation_api

        _private, public_pem = _keypair()
        fingerprint = federation_core.public_key_fingerprint(public_pem)
        existing = SimpleNamespace(id=3, instance_id=fingerprint, public_key=public_pem, status="active")
        headers = {
            "x-federation-signature": "sig",
            "x-federation-timestamp": "1",
            "x-federation-instance": fingerprint,
        }
        with (
            patch.object(federation_api.DbManager, "find_records", return_value=[existing]),
            patch.object(federation_api.federation, "federation_verify", return_value=True) as verify,
        ):
            got = federation_api._verify_federated_request("{}", headers)
        assert got is existing
        verify.assert_called_once_with("{}", "sig", "1", public_pem)


class TestEndpointAndSubpaths:
    def test_endpoint_url_appends_mount_path(self):
        with patch.object(federation_core, "get_public_url", return_value="https://myhost"):
            assert federation_core.federation_endpoint_url() == "https://myhost/api/federation"

    def test_endpoint_url_empty_when_origin_unknown(self):
        with patch.object(federation_core, "get_public_url", return_value=""):
            assert federation_core.federation_endpoint_url() == ""

    def test_generate_code_advertises_full_endpoint(self):
        _private, public_pem = _keypair()
        captured = {}

        def _capture(webhook_url, instance_id, public_key_pem, code):
            captured["webhook_url"] = webhook_url
            return "encoded"

        with (
            patch.object(federation_core, "get_public_url", return_value="https://myhost"),
            patch.object(federation_core, "get_instance_id", return_value="fp"),
            patch.object(federation_core, "get_or_create_instance_keypair", return_value=(_private, public_pem)),
            patch.object(federation_core, "encode_federation_connection_blob", side_effect=_capture),
            patch.object(federation_core.DbManager, "create_record"),
        ):
            federation_core.generate_federation_code(1, context={})
        assert captured["webhook_url"] == "https://myhost/api/federation"

    def test_push_helpers_use_resource_subpaths(self):
        fed_ws = SimpleNamespace(webhook_url="https://peer.example/api/federation", instance_id="fp")
        calls = []
        with patch.object(
            federation_core, "_federation_request", side_effect=lambda fw, path, payload: calls.append(path)
        ):
            federation_core.push_message(fed_ws, {})
            federation_core.push_edit(fed_ws, {})
            federation_core.push_delete(fed_ws, {})
            federation_core.push_reaction(fed_ws, {})
            federation_core.push_users(fed_ws, {})
        assert calls == ["/message", "/message/edit", "/message/delete", "/message/react", "/users"]

    def test_request_appends_subpath_to_peer_endpoint(self):
        fed_ws = SimpleNamespace(webhook_url="https://peer.example/api/federation", instance_id="fp")
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"ok": True}
        with (
            patch.object(federation_core, "federation_sign", return_value=("sig", "1")),
            patch.object(federation_core, "get_instance_id", return_value="fp"),
            patch.object(federation_core.requests, "request", return_value=resp) as req,
        ):
            federation_core._federation_request(fed_ws, "/message", {"a": 1})
        assert req.call_args.args[1] == "https://peer.example/api/federation/message"

    def test_initiate_appends_pair_and_sends_endpoint(self):
        _private, public_pem = _keypair()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"ok": True, "public_key": public_pem}
        with (
            patch.object(federation_core, "get_public_url", return_value="https://myhost"),
            patch.object(federation_core, "validate_webhook_url", return_value=True),
            patch.object(federation_core, "get_or_create_instance_keypair", return_value=(_private, public_pem)),
            patch.object(federation_core, "federation_sign", return_value=("sig", "1")),
            patch.object(federation_core, "get_instance_id", return_value="fp"),
            patch.object(federation_core.requests, "post", return_value=resp) as post,
        ):
            federation_core.initiate_federation_connect("https://peer.example/api/federation", "FED-1")
        assert post.call_args.args[0] == "https://peer.example/api/federation/pair"
        sent = json.loads(post.call_args.kwargs["data"])
        assert sent["webhook_url"] == "https://myhost/api/federation"


class TestEmptyUrlDm:
    def test_generate_code_dms_when_url_missing(self):
        from handlers.federation_cmds import handle_federation_label_submit

        body = {
            "user": {"id": "U_ADMIN"},
            "view": {"state": {"values": {}}},
            "team": {"id": "T1"},
        }
        client = MagicMock()
        workspace = SimpleNamespace(id=1, team_id="T1")
        with (
            patch("handlers.federation_cmds.helpers.federation_enabled", return_value=True),
            patch("handlers.federation_cmds._require_primary_admin", return_value=workspace),
            patch("handlers.federation_cmds.federation.get_public_url", return_value=""),
            patch("handlers.federation_cmds._dm_actor") as dm,
        ):
            handle_federation_label_submit(body, client, MagicMock(), {})
        dm.assert_called_once()
        assert "public URL" in dm.call_args.args[2]


class TestFedWsCacheInvalidation:
    def test_remove_connection_invalidates_fed_ws_cache(self):
        from handlers.federation_cmds import handle_remove_federation_connection

        body = {
            "user": {"id": "U_ADMIN"},
            "team": {"id": "T1"},
            "actions": [{"action_id": "remove_federation_connection_9"}],
        }
        workspace = SimpleNamespace(id=1, team_id="T1")
        member = SimpleNamespace(id=9, group_id=3, federated_workspace_id=2)
        with (
            patch("handlers.federation_cmds._require_primary_admin", return_value=workspace),
            patch("handlers.federation_cmds.DbManager.get_record", return_value=member),
            patch("handlers.federation_cmds.DbManager.update_records"),
            patch("handlers.federation_cmds.invalidate_fed_ws_for_sync_cache") as inv,
            patch("handlers.federation_cmds.helpers.get_workspace_record", return_value=workspace),
            patch("handlers.federation_cmds.builders.refresh_home_tab_for_workspace"),
        ):
            handle_remove_federation_connection(body, MagicMock(), MagicMock(), {})
        inv.assert_called_once_with()

    def test_inbound_pair_invalidates_fed_ws_cache(self):
        from federation import api as federation_api

        body = {
            "code": "FED-ABCD1234",
            "webhook_url": "https://peer.example/api/federation",
            "instance_id": "a" * 64,
            "public_key": "pem",
        }
        body_str = json.dumps(body)
        group = SimpleNamespace(id=5, invite_code="FED-ABCD1234", status="active")
        fed_ws = SimpleNamespace(id=7, instance_id="a" * 64)
        with (
            patch.object(federation_api.federation, "validate_webhook_url", return_value=True),
            patch.object(federation_api.federation, "federation_verify", return_value=True),
            patch.object(federation_api.federation, "instance_id_matches_public_key", return_value=True),
            patch.object(federation_api.federation, "get_or_create_federated_workspace", return_value=fed_ws),
            patch.object(federation_api.federation, "get_or_create_instance_keypair", return_value=(None, "our-pem")),
            patch.object(federation_api.federation, "get_instance_id", return_value="b" * 64),
            patch.object(federation_api.DbManager, "find_records", side_effect=[[group], []]),
            patch.object(federation_api.DbManager, "create_record"),
            patch.object(federation_api, "invalidate_fed_ws_for_sync_cache") as inv,
        ):
            status, resp = federation_api.handle_pair(
                body,
                body_str,
                {
                    "X-Federation-Signature": "sig",
                    "X-Federation-Timestamp": "1",
                },
            )
        assert status == 200
        assert resp.get("ok") is True
        inv.assert_called_once_with()

    def test_invalidate_helper_deletes_prefix(self):
        from helpers.workspace import invalidate_fed_ws_for_sync_cache

        with patch("helpers._cache._cache_delete_prefix") as delete_prefix:
            invalidate_fed_ws_for_sync_cache()
        delete_prefix.assert_called_once_with("fed_ws_for_sync:")

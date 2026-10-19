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
                federation_core.public_key_fingerprint(public_pem),
                public_pem,
                "FED-ABCD",
            )
            parsed = federation_core.parse_federation_code(encoded)
        assert parsed is not None
        assert parsed["webhook_url"] == "https://peer.example/api/federation"
        assert parsed["code"] == "FED-ABCD"
        assert parsed["public_key"] == public_pem
        assert parsed["sig"]

    def test_roundtrip_includes_signed_label_and_primary_team(self):
        private, public_pem = _keypair()
        with (
            patch.object(federation_core, "get_or_create_instance_keypair", return_value=(private, public_pem)),
            patch.object(federation_core, "validate_webhook_url", return_value=True),
        ):
            encoded = federation_core.encode_federation_connection_blob(
                "https://peer.example/api/federation",
                federation_core.public_key_fingerprint(public_pem),
                public_pem,
                "FED-ABCD",
                label="Partner Org",
                primary_team_id="TPRIMARY",
                primary_workspace_name="Workspace A",
            )
            parsed = federation_core.parse_federation_code(encoded)
            payload = json.loads(base64.urlsafe_b64decode(encoded.encode()).decode())
            payload["primary_workspace_name"] = "Evil"
            name_tampered = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
            payload["primary_team_id"] = "TEVIL"
            team_tampered = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
            renamed = federation_core.parse_federation_code(name_tampered)
            team_parse = federation_core.parse_federation_code(team_tampered)
        assert parsed is not None
        assert parsed["label"] == "Partner Org"
        assert parsed["primary_team_id"] == "TPRIMARY"
        assert parsed["primary_workspace_name"] == "Workspace A"
        assert "workspaces" not in parsed
        assert renamed is not None
        assert renamed["primary_workspace_name"] == "Evil"
        assert team_parse is None

    def test_tampered_url_fails_verify(self):
        private, public_pem = _keypair()
        with (
            patch.object(federation_core, "get_or_create_instance_keypair", return_value=(private, public_pem)),
            patch.object(federation_core, "validate_webhook_url", return_value=True),
        ):
            encoded = federation_core.encode_federation_connection_blob(
                "https://peer.example/api/federation",
                federation_core.public_key_fingerprint(public_pem),
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
            federation_core.generate_federation_code(subject_team_id=None, context={})

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
        with (
            patch.object(federation_core, "get_or_create_instance_keypair", return_value=(private, public_pem)),
        ):
            assert federation_core.get_instance_id() == expected
        federation_core._INSTANCE_ID = None

    def test_legacy_env_is_ignored_and_warned_once(self, monkeypatch, caplog):
        federation_core._INSTANCE_ID = None
        federation_core._LEGACY_INSTANCE_ID_WARNED = False
        monkeypatch.setenv("SYNCBOT_INSTANCE_ID", "env-instance-id")
        private, public_pem = _keypair()
        expected = federation_core.public_key_fingerprint(public_pem)
        with (
            patch.object(federation_core, "get_or_create_instance_keypair", return_value=(private, public_pem)),
            caplog.at_level("WARNING", logger="syncbot"),
        ):
            assert federation_core.get_instance_id() == expected
            assert federation_core.get_instance_id() == expected
        warnings = [
            r
            for r in caplog.records
            if r.message == "legacy_env_ignored" and getattr(r, "env", None) == "SYNCBOT_INSTANCE_ID"
        ]
        assert len(warnings) == 1
        federation_core._INSTANCE_ID = None

    def test_get_instance_id_uses_key_fingerprint(self, monkeypatch):
        federation_core._INSTANCE_ID = None
        federation_core._LEGACY_INSTANCE_ID_WARNED = False
        monkeypatch.delenv("SYNCBOT_INSTANCE_ID", raising=False)
        private, public_pem = _keypair()
        expected = federation_core.public_key_fingerprint(public_pem)
        with patch.object(federation_core, "get_or_create_instance_keypair", return_value=(private, public_pem)):
            assert federation_core.get_instance_id() == expected
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

    def test_parse_rejects_non_fingerprint_instance_id(self):
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
            assert federation_core.parse_federation_code(encoded) is None

    def test_reconnect_upgrades_uuid_row_by_public_key(self):
        _private, public_pem = _keypair()
        fingerprint = federation_core.public_key_fingerprint(public_pem)
        existing = SimpleNamespace(
            instance_id="old-uuid",
            public_key=public_pem,
            private_key_encrypted=None,
        )
        upgraded = SimpleNamespace(
            instance_id=fingerprint,
            public_key=public_pem,
            private_key_encrypted=None,
        )
        with (
            patch.object(federation_core.DbManager, "find_records", side_effect=[[], [existing]]),
            patch.object(federation_core, "_upgrade_instance_id", return_value=upgraded) as upgrade,
            patch.object(federation_core.DbManager, "update_records") as update,
            patch.object(federation_core.DbManager, "get_record", return_value=upgraded),
        ):
            federation_core.get_or_create_instance(
                instance_id=fingerprint,
                webhook_url="https://peer.example/api/federation",
                public_key=public_pem,
            )
        upgrade.assert_called_once_with(existing, fingerprint)
        assert update.call_args.args[1][0].right.value == fingerprint


class TestInboundInstanceIdUpgrade:
    def test_verify_ignores_uuid_row_when_fingerprint_missing(self):
        from federation import api as federation_api

        _private, public_pem = _keypair()
        fingerprint = federation_core.public_key_fingerprint(public_pem)
        headers = {
            "X-Federation-Signature": "sig",
            "X-Federation-Timestamp": "1",
            "X-Federation-Instance": fingerprint,
        }
        with (
            patch.object(federation_api.DbManager, "find_records", return_value=[]),
            patch.object(federation_api.federation, "federation_verify", return_value=True),
            patch.object(federation_api.DbManager, "update_records") as update,
        ):
            got = federation_api._verify_federated_request("{}", headers)
        assert got is None
        update.assert_not_called()

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
        with patch.object(federation_core, "get_public_url", return_value="https://this-instance.example"):
            assert federation_core.federation_endpoint_url() == "https://this-instance.example/api/federation"

    def test_endpoint_url_empty_when_origin_unknown(self):
        with patch.object(federation_core, "get_public_url", return_value=""):
            assert federation_core.federation_endpoint_url() == ""

    def test_generate_code_signs_primary_team_not_allowlist(self):
        _private, public_pem = _keypair()
        captured = {}

        def _capture(webhook_url, instance_id, public_key_pem, code, **kwargs):
            captured.update(kwargs)
            captured["webhook_url"] = webhook_url
            return "encoded"

        primary = SimpleNamespace(team_id="T_PRIMARY", workspace_name="Workspace A")
        with (
            patch.object(federation_core, "get_public_url", return_value="https://this-instance.example"),
            patch.object(federation_core, "get_instance_id", return_value="fp"),
            patch.object(federation_core, "get_or_create_instance_keypair", return_value=(_private, public_pem)),
            patch.object(federation_core, "encode_federation_connection_blob", side_effect=_capture),
            patch.object(federation_core.DbManager, "create_record"),
            patch.dict(os.environ, {"PRIMARY_WORKSPACE": "T_PRIMARY"}),
            patch.object(federation_core.DbManager, "find_records", return_value=[primary]),
        ):
            federation_core.generate_federation_code(
                subject_team_id=None,
                context={},
                workspace_ids=[99],
            )
        assert captured["primary_team_id"] == "T_PRIMARY"
        assert captured["primary_workspace_name"] == "Workspace A"
        assert "workspaces" not in captured or captured.get("workspaces") is None
        assert captured["webhook_url"] == "https://this-instance.example/api/federation"

    def test_generate_code_uses_full_endpoint(self):
        _private, public_pem = _keypair()
        captured = {}

        def _capture(webhook_url, instance_id, public_key_pem, code, **_kwargs):
            captured["webhook_url"] = webhook_url
            return "encoded"

        with (
            patch.object(federation_core, "get_public_url", return_value="https://this-instance.example"),
            patch.object(federation_core, "get_instance_id", return_value="fp"),
            patch.object(federation_core, "get_or_create_instance_keypair", return_value=(_private, public_pem)),
            patch.object(federation_core, "encode_federation_connection_blob", side_effect=_capture),
            patch.object(federation_core, "this_primary_workspace_name", return_value=None),
            patch.object(federation_core.DbManager, "create_record"),
        ):
            federation_core.generate_federation_code(subject_team_id=None, context={})
        assert captured["webhook_url"] == "https://this-instance.example/api/federation"

    def test_push_helpers_use_resource_subpaths(self):
        fed_ws = SimpleNamespace(webhook_url="https://peer.example/api/federation", instance_id="fp")
        calls = []
        with patch.object(
            federation_core,
            "_federation_request",
            side_effect=lambda fw, path, payload, **kwargs: calls.append(path),
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
            patch.object(federation_core, "get_public_url", return_value="https://this-instance.example"),
            patch.object(federation_core, "validate_webhook_url", return_value=True),
            patch.object(federation_core, "get_or_create_instance_keypair", return_value=(_private, public_pem)),
            patch.object(federation_core, "federation_sign", return_value=("sig", "1")),
            patch.object(federation_core, "get_instance_id", return_value="fp"),
            patch.object(federation_core.requests, "post", return_value=resp) as post,
        ):
            federation_core.initiate_federation_connect("https://peer.example/api/federation", "FED-1")
        assert post.call_args.args[0] == "https://peer.example/api/federation/pair"
        sent = json.loads(post.call_args.kwargs["data"])
        assert sent["webhook_url"] == "https://this-instance.example/api/federation"


class TestEmptyUrlDm:
    def test_create_connection_dms_when_url_missing(self):
        from handlers.federation_cmds import handle_create_external_connection_submit

        body = {
            "user": {"id": "U_ADMIN"},
            "view": {
                "state": {
                    "values": {
                        "create_external_connection_name": {
                            "create_external_connection_name": {"value": "Partner Org"}
                        },
                        "select_create_external_workspaces": {
                            "select_create_external_workspaces": {
                                "selected_options": [{"value": "1"}],
                            }
                        },
                    }
                }
            },
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
            handle_create_external_connection_submit(body, client, MagicMock(), {})
        dm.assert_called_once()
        assert "public URL" in dm.call_args.args[2]


class TestFedWsCacheInvalidation:
    def test_leave_connection_invalidates_fed_ws_cache(self):
        from handlers.federation_cmds import handle_leave_external_connection_confirm

        body = {
            "user": {"id": "U_ADMIN"},
            "team": {"id": "T1"},
            "view": {"private_metadata": json.dumps({"instance_id": "peer"}), "id": "V1"},
            "actions": [{"action_id": "confirm_leave_external_connection", "value": "peer"}],
        }
        workspace = SimpleNamespace(id=1, team_id="T1")
        fed_ws = SimpleNamespace(id=2, instance_id="peer", name="Partner Org", private_key_encrypted=None)
        stub = SimpleNamespace(id=9, instance_id="peer")
        with (
            patch("handlers.federation_cmds._require_primary_admin", return_value=workspace),
            patch("handlers.federation_cmds.DbManager.get_record", return_value=fed_ws),
            patch("handlers.federation_cmds.DbManager.find_records", return_value=[stub]),
            patch("handlers.federation_cmds.DbManager.delete_records"),
            patch("handlers.federation_cmds.DbManager.update_records"),
            patch("handlers.federation_cmds.helpers.soft_delete_workspace") as pause,
            patch("handlers.federation_cmds.invalidate_fed_ws_for_sync_cache") as inv,
            patch("handlers.federation_cmds._close_modal_done"),
            patch("handlers.federation_cmds.builders.refresh_home_tab_for_workspace"),
        ):
            handle_leave_external_connection_confirm(body, MagicMock(), MagicMock(), {})
        pause.assert_called_once_with(stub)
        inv.assert_called_once_with()

    def test_leave_connection_refuses_self_instance(self):
        from handlers.federation_cmds import handle_leave_external_connection

        body = {
            "user": {"id": "U_ADMIN"},
            "team": {"id": "T1"},
            "trigger_id": "tr",
            "actions": [{"action_id": "leave_external_connection_selfid", "value": "selfid"}],
        }
        workspace = SimpleNamespace(id=1, team_id="T1")
        self_row = SimpleNamespace(instance_id="selfid", private_key_encrypted="gAAAAA")
        client = MagicMock()
        with (
            patch("handlers.federation_cmds._require_primary_admin", return_value=workspace),
            patch("handlers.federation_cmds.DbManager.get_record", return_value=self_row),
            patch("handlers.federation_cmds.DbManager.update_records") as update,
            patch("handlers.federation_cmds.helpers.soft_delete_workspace") as pause,
        ):
            handle_leave_external_connection(body, client, MagicMock(), {})
        update.assert_not_called()
        pause.assert_not_called()
        client.views_update.assert_not_called()

    def test_inbound_pair_invalidates_fed_ws_cache(self):
        from federation import api as federation_api

        body = {
            "code": "FED-ABCD1234",
            "webhook_url": "https://peer.example/api/federation",
            "instance_id": "a" * 64,
            "public_key": "pem",
        }
        body_str = json.dumps(body)
        pairing = SimpleNamespace(id=3, code="FED-ABCD1234", subject_team_id=None, label="Peer")
        fed_ws = SimpleNamespace(instance_id="a" * 64, private_key_encrypted=None)
        creator = SimpleNamespace(id=1, team_id="T_LOCAL", workspace_name="Local")
        with (
            patch.object(federation_api.federation, "validate_webhook_url", return_value=True),
            patch.object(federation_api.federation, "federation_verify", return_value=True),
            patch.object(federation_api.federation, "instance_id_matches_public_key", return_value=True),
            patch.object(federation_api.federation, "get_or_create_instance", return_value=fed_ws),
            patch.object(federation_api.federation, "get_or_create_instance_keypair", return_value=(None, "our-pem")),
            patch.object(federation_api.federation, "get_instance_id", return_value="b" * 64),
            patch.object(federation_api.federation, "push_allowed_workspaces", return_value={"ok": True}),
            patch("federation.replicate.replicate_peer_snapshot"),
            patch.object(
                federation_api.DbManager,
                "find_records",
                side_effect=[[pairing], [], []],
            ),
            patch.object(federation_api.DbManager, "create_record"),
            patch.object(federation_api.federation, "delete_pairing_code"),
            patch.object(federation_api.DbManager, "delete_records"),
            patch.object(federation_api.helpers, "get_workspace_by_id", return_value=creator),
            patch.object(federation_api, "invalidate_fed_ws_for_sync_cache") as inv,
        ):
            status, resp = federation_api.handle_pair(
                body,
                body_str,
                {
                    "X-Federation-Signature": "sig",
                    "X-Federation-Timestamp": "1",
                    "X-Federation-Instance": body["instance_id"],
                },
            )
        assert status == 200
        assert resp.get("ok") is True
        inv.assert_called_once_with()

    def test_inbound_pair_rejects_connection_blob_as_code(self):
        from federation import api as federation_api

        blob = "A" * 80
        body = {
            "code": blob,
            "webhook_url": "https://peer.example/api/federation",
            "instance_id": "a" * 64,
            "public_key": "pem",
        }
        status, resp = federation_api.handle_pair(
            body,
            json.dumps(body),
            {
                "X-Federation-Signature": "sig",
                "X-Federation-Timestamp": "1",
                "X-Federation-Instance": body["instance_id"],
            },
        )
        assert status == 400
        assert resp == {"error": "code_too_long"}

    def test_invalidate_helper_deletes_prefix(self):
        from helpers.workspace import invalidate_fed_ws_for_sync_cache

        with patch("helpers._cache._cache_delete_prefix") as delete_prefix:
            invalidate_fed_ws_for_sync_cache()
        delete_prefix.assert_called_once_with("fed_ws_for_sync:")


class TestExternalConnectionModals:
    def test_create_ack_requires_workspaces(self):
        from handlers.federation_cmds import handle_create_external_connection_submit_ack

        body = {
            "view": {
                "state": {
                    "values": {
                        "create_external_connection_name": {
                            "create_external_connection_name": {"value": "Partner Org"}
                        },
                        "select_create_external_workspaces": {
                            "select_create_external_workspaces": {"selected_options": []}
                        },
                    }
                }
            }
        }
        with patch("handlers.federation_cmds.helpers.federation_enabled", return_value=True):
            result = handle_create_external_connection_submit_ack(body, MagicMock(), {})
        assert result["response_action"] == "errors"
        assert "select_create_external_workspaces" in result["errors"]

    def test_join_ack_updates_to_review(self):
        from handlers.federation_cmds import handle_join_external_connection_submit_ack

        payload = {
            "code": "FED-ABCD",
            "webhook_url": "https://peer.example/api/federation",
            "instance_id": "a" * 64,
            "label": "Partner Org",
            "primary_workspace_name": "Workspace A",
            "primary_team_id": "TPRIMARY",
        }
        body = {
            "view": {
                "state": {
                    "values": {"join_external_connection_code": {"join_external_connection_code": {"value": "ENCODED"}}}
                }
            }
        }
        with (
            patch("handlers.federation_cmds.helpers.federation_enabled", return_value=True),
            patch("handlers.federation_cmds.federation.parse_federation_code", return_value=payload),
            patch("handlers.federation_cmds._local_workspace_options", return_value=[]),
        ):
            result = handle_join_external_connection_submit_ack(body, MagicMock(), {})
        assert result["response_action"] == "update"
        view = result["view"]
        assert view["callback_id"] == "join_external_connection_review"
        assert view["submit"]["text"] == "Join"
        assert view["title"]["text"] == "Join Connection"
        text = json.dumps(view)
        assert "Partner Org" in text
        assert "Primary Workspace: `Workspace A`" in text
        assert "Team ID: `TPRIMARY`" in text
        assert "Remote HQ" not in text
        assert "https://peer.example/api/federation" in text

    def test_create_ack_keeps_modal_open(self):
        from handlers._common import _DM_WAIT_TEXT
        from handlers.federation_cmds import handle_create_external_connection_submit_ack

        body = {
            "view": {
                "state": {
                    "values": {
                        "create_external_connection_name": {
                            "create_external_connection_name": {"value": "Partner Org"}
                        },
                        "select_create_external_workspaces": {
                            "select_create_external_workspaces": {"selected_options": [{"value": "1"}]}
                        },
                    }
                }
            }
        }
        with patch("handlers.federation_cmds.helpers.federation_enabled", return_value=True):
            result = handle_create_external_connection_submit_ack(body, MagicMock(), {})
        assert result["response_action"] == "update"
        assert result["view"]["title"]["text"] == "Create Connection"
        assert "submit" not in result["view"]
        text = json.dumps(result["view"])
        assert _DM_WAIT_TEXT in text
        assert result["view"]["close"]["text"] == "Close"
        meta = json.loads(result["view"]["private_metadata"])
        assert meta["label"] == "Partner Org"
        assert meta["workspace_ids"] == [1]

    def test_join_review_ack_keeps_wait_modal(self):
        from handlers._common import _STAY_WAIT_TEXT
        from handlers.federation_cmds import handle_join_external_connection_review_ack

        body = {
            "view": {
                "private_metadata": json.dumps({"code": "FED-ABCD"}),
                "state": {
                    "values": {
                        "select_join_external_workspaces": {
                            "select_join_external_workspaces": {"selected_options": [{"value": "1"}]}
                        }
                    }
                },
            }
        }
        with patch("handlers.federation_cmds.helpers.federation_enabled", return_value=True):
            result = handle_join_external_connection_review_ack(body, MagicMock(), {})
        assert result["response_action"] == "update"
        assert result["view"]["title"]["text"] == "Join Connection"
        assert result["view"]["close"]["text"] == "Close"
        assert "submit" not in result["view"]
        assert result["view"]["blocks"][0]["text"]["text"] == _STAY_WAIT_TEXT
        assert json.loads(result["view"]["private_metadata"])["code"] == "FED-ABCD"

    def test_join_review_ack_still_errors_without_allowlist(self):
        from handlers.federation_cmds import handle_join_external_connection_review_ack

        body = {"view": {"state": {"values": {}}}}
        with patch("handlers.federation_cmds.helpers.federation_enabled", return_value=True):
            result = handle_join_external_connection_review_ack(body, MagicMock(), {})
        assert result["response_action"] == "errors"
        assert "select_join_external_workspaces" in result["errors"]

    def test_create_ack_preserves_request_id(self):
        from handlers.federation_cmds import handle_create_external_connection_submit_ack

        body = {
            "view": {
                "private_metadata": json.dumps({"request_id": 9, "exclude_workspace_id": 10}),
                "state": {
                    "values": {
                        "create_external_connection_name": {
                            "create_external_connection_name": {"value": "Partner Org"}
                        },
                        "select_create_external_workspaces": {
                            "select_create_external_workspaces": {"selected_options": [{"value": "1"}]}
                        },
                    }
                },
            }
        }
        with patch("handlers.federation_cmds.helpers.federation_enabled", return_value=True):
            result = handle_create_external_connection_submit_ack(body, MagicMock(), {})
        meta = json.loads(result["view"]["private_metadata"])
        assert meta["request_id"] == 9
        assert meta["exclude_workspace_id"] == 10
        assert meta["workspace_ids"] == [1]
        assert 10 not in meta["workspace_ids"]

    def test_workspaces_block_uses_multi_select(self):
        from handlers.federation_cmds import _workspaces_block
        from slack import orm

        option = orm.SelectorOption(name="Workspace A", value="1")
        with patch("handlers.federation_cmds._local_workspace_options", return_value=[option]):
            blocks = _workspaces_block(action="select_create_external_workspaces")
        assert isinstance(blocks[0].element, orm.MultiStaticSelectElement)
        rendered = orm.BlockView(blocks=blocks).as_form_field()
        assert rendered[0]["element"]["type"] == "multi_static_select"
        assert rendered[0]["element"]["options"][0]["value"] == "1"

    def test_workspaces_block_owner_note_on_established_edit(self):
        from handlers.federation_cmds import _workspaces_block
        from slack import orm

        option = orm.SelectorOption(name="Workspace A", value="1")
        with patch("handlers.federation_cmds._local_workspace_options", return_value=[option]):
            blocks = _workspaces_block(action="select_edit_external_workspaces", show_owner_note=True)
        assert "Give Up Ownership" in blocks[1].element.initial_value

    def test_local_workspace_options_skips_excluded(self):
        from handlers.federation_cmds import _local_workspace_options

        keep = SimpleNamespace(id=1, team_id="T1", workspace_name="Workspace A", deleted_at=None)
        leave = SimpleNamespace(id=10, team_id="T_SRC", workspace_name="Workspace B", deleted_at=None)
        with (
            patch("handlers.federation_cmds.DbManager.find_records", return_value=[keep, leave]),
            patch("helpers.workspace_kind.is_local_workspace", return_value=True),
            patch("handlers.federation_cmds.helpers.resolve_workspace_name", side_effect=lambda ws: ws.workspace_name),
        ):
            options = _local_workspace_options(exclude_ids=[10])
        assert [option.value for option in options] == ["1"]

    def test_show_connection_code_empty_state(self):
        from handlers.federation_cmds import handle_show_external_connection_code

        body = {
            "user": {"id": "U_ADMIN"},
            "team": {"id": "T1"},
            "trigger_id": "tr",
            "actions": [{"action_id": "show_external_connection_code_3", "value": "3"}],
        }
        client = MagicMock()
        workspace = SimpleNamespace(id=1, team_id="T1")
        with (
            patch("handlers.federation_cmds.helpers.federation_enabled", return_value=True),
            patch("handlers.federation_cmds._require_primary_admin", return_value=workspace),
            patch("handlers.federation_cmds.DbManager.get_record", return_value=None),
        ):
            handle_show_external_connection_code(body, client, MagicMock(), {})
        view = client.views_update.call_args.kwargs["view"]
        assert view["title"]["text"] == "Connection Code"
        assert "submit" not in view
        assert "no longer available" in json.dumps(view)

    def test_create_submit_uses_ack_private_metadata(self):
        from handlers.federation_cmds import handle_create_external_connection_submit

        body = {
            "user": {"id": "U_ADMIN"},
            "team": {"id": "T1"},
            "view": {
                "id": "V1",
                "private_metadata": json.dumps({"label": "Partner Org", "workspace_ids": [10]}),
                "state": {"values": {}},
            },
        }
        workspace = SimpleNamespace(id=1, team_id="T1")
        with (
            patch("handlers.federation_cmds.helpers.federation_enabled", return_value=True),
            patch("handlers.federation_cmds._require_primary_admin", return_value=workspace),
            patch("handlers.federation_cmds.federation.get_public_url", return_value="https://me.example"),
            patch(
                "handlers.federation_cmds.federation.generate_federation_code",
                return_value=("ENCODED", "FED-1"),
            ) as generate,
            patch("handlers.federation_cmds._update_connection_code_modal"),
            patch("handlers.federation_cmds.builders.refresh_home_tab_for_workspace"),
        ):
            handle_create_external_connection_submit(body, MagicMock(), MagicMock(), {})
        assert generate.call_args.kwargs["label"] == "Partner Org"
        assert generate.call_args.kwargs["workspace_ids"] == [10]
        assert "workspaces" not in generate.call_args.kwargs

    def test_create_submit_from_request_sets_subject_and_fulfills(self):
        from handlers.federation_cmds import handle_create_external_connection_submit

        request = SimpleNamespace(id=9, status="pending", subject_team_id="T_SRC")
        body = {
            "user": {"id": "U_ADMIN"},
            "team": {"id": "T1"},
            "view": {
                "id": "V1",
                "private_metadata": json.dumps(
                    {
                        "label": "Partner Org",
                        "workspace_ids": [1, 10],
                        "request_id": 9,
                        "exclude_workspace_id": 10,
                    }
                ),
                "state": {"values": {}},
            },
        }
        workspace = SimpleNamespace(id=1, team_id="T1")
        order: list[str] = []

        def track_modal(*_args, **_kwargs):
            order.append("modal")

        def track_fulfill(*_args, **_kwargs):
            order.append("fulfill")

        with (
            patch("handlers.federation_cmds.helpers.federation_enabled", return_value=True),
            patch("handlers.federation_cmds._require_primary_admin", return_value=workspace),
            patch("handlers.federation_cmds.DbManager.get_record", return_value=request),
            patch("handlers.federation_cmds.federation.get_public_url", return_value="https://me.example"),
            patch(
                "handlers.federation_cmds.federation.generate_federation_code",
                return_value=("ENCODED", "FED-1"),
            ) as generate,
            patch("handlers.export_import.fulfill_pairing_request", side_effect=track_fulfill) as fulfill,
            patch("handlers.federation_cmds._update_connection_code_modal", side_effect=track_modal),
            patch("handlers.federation_cmds.builders.refresh_home_tab_for_workspace"),
        ):
            handle_create_external_connection_submit(body, MagicMock(), MagicMock(), {})
        assert generate.call_args.kwargs["subject_team_id"] == "T_SRC"
        assert generate.call_args.kwargs["workspace_ids"] == [1]
        fulfill.assert_called_once()
        assert fulfill.call_args.args[0] is request
        assert fulfill.call_args.kwargs["encoded"] == "ENCODED"
        assert fulfill.call_args.kwargs["raw_code"] == "FED-1"
        assert order == ["modal", "fulfill"]

    def test_edit_pending_opens_edit_modal(self):
        from handlers.federation_cmds import handle_edit_pending_external_connection

        pairing = SimpleNamespace(
            id=3,
            subject_team_id=None,
            label="Partner Org",
            allowed_workspace_ids="[10]",
            created_at=None,
        )
        body = {
            "user": {"id": "U_ADMIN"},
            "team": {"id": "T1"},
            "trigger_id": "tr",
            "actions": [{"action_id": "edit_pending_external_connection_3", "value": "3"}],
        }
        client = MagicMock()
        with (
            patch("handlers.federation_cmds.helpers.federation_enabled", return_value=True),
            patch("handlers.federation_cmds._require_primary_admin", return_value=SimpleNamespace(id=1)),
            patch("handlers.federation_cmds.DbManager.get_record", return_value=pairing),
            patch("handlers.federation_cmds._local_workspace_options", return_value=[]),
        ):
            handle_edit_pending_external_connection(body, client, MagicMock(), {})
        view = client.views_update.call_args.kwargs["view"]
        assert view["title"]["text"] == "Edit Connection"
        assert json.loads(view["private_metadata"]) == {"pairing_id": 3}
        assert "edit_external_connection_name" in json.dumps(view)

    def test_edit_submit_updates_pending_allowlist(self):
        from handlers.federation_cmds import handle_edit_external_connection_submit

        pairing = SimpleNamespace(id=3, subject_team_id=None, created_at=None)
        body = {
            "user": {"id": "U_ADMIN"},
            "team": {"id": "T1"},
            "view": {
                "private_metadata": json.dumps({"pairing_id": 3}),
                "state": {
                    "values": {
                        "edit_external_connection_name": {"edit_external_connection_name": {"value": "Partner Org"}},
                        "select_edit_external_workspaces": {
                            "select_edit_external_workspaces": {"selected_options": [{"value": "10"}]}
                        },
                    }
                },
            },
        }
        with (
            patch("handlers.federation_cmds.helpers.federation_enabled", return_value=True),
            patch("handlers.federation_cmds._require_primary_admin", return_value=SimpleNamespace(id=1, team_id="T1")),
            patch("handlers.federation_cmds.DbManager.get_record", return_value=pairing),
            patch("handlers.federation_cmds.DbManager.update_records") as update,
            patch("handlers.federation_cmds.replace_federation_allowlist") as replace,
            patch("handlers.federation_cmds.builders.refresh_home_tab_for_workspace"),
        ):
            handle_edit_external_connection_submit(body, MagicMock(), MagicMock(), {})
        update.assert_called_once()
        replace.assert_not_called()
        assert list(update.call_args.args[2].values()) == ["Partner Org", "[10]"]

    def test_cancel_pending_confirm_deletes_pairing(self):
        from handlers.federation_cmds import handle_cancel_pending_external_connection_confirm

        pairing = SimpleNamespace(id=3, subject_team_id="T_SRC", label="Workspace B")
        body = {
            "user": {"id": "U_ADMIN"},
            "team": {"id": "T1"},
            "view": {"id": "V1", "private_metadata": json.dumps({"pairing_id": 3})},
            "actions": [{"action_id": "confirm_cancel_pending_external_connection", "value": "3"}],
        }
        with (
            patch("handlers.federation_cmds._require_primary_admin", return_value=SimpleNamespace(id=1, team_id="T1")),
            patch("handlers.federation_cmds.DbManager.get_record", return_value=pairing),
            patch("handlers.federation_cmds.federation.delete_pairing_code") as delete,
            patch("handlers.federation_cmds._close_modal_done"),
            patch("handlers.federation_cmds.builders.refresh_home_tab_for_workspace"),
        ):
            handle_cancel_pending_external_connection_confirm(body, MagicMock(), MagicMock(), {})
        delete.assert_called_once_with(3)

    def test_cancel_pending_confirm_reports_delete_failure(self):
        from handlers.federation_cmds import handle_cancel_pending_external_connection_confirm

        pairing = SimpleNamespace(id=3, subject_team_id="T_SRC", label="Workspace B")
        body = {
            "user": {"id": "U_ADMIN"},
            "team": {"id": "T1"},
            "view": {"id": "V1", "private_metadata": json.dumps({"pairing_id": 3})},
            "actions": [{"action_id": "confirm_cancel_pending_external_connection", "value": "3"}],
        }
        with (
            patch("handlers.federation_cmds._require_primary_admin", return_value=SimpleNamespace(id=1, team_id="T1")),
            patch("handlers.federation_cmds.DbManager.get_record", return_value=pairing),
            patch(
                "handlers.federation_cmds.federation.delete_pairing_code",
                side_effect=RuntimeError("fk"),
            ),
            patch("handlers.federation_cmds._close_modal_done") as close,
            patch("handlers.federation_cmds.builders.refresh_home_tab_for_workspace") as refresh,
        ):
            handle_cancel_pending_external_connection_confirm(body, MagicMock(), MagicMock(), {})
        close.assert_called_once()
        assert "Could not cancel" in close.call_args.args[2]
        refresh.assert_not_called()

    def test_delete_pairing_code_unlinks_pairing_requests_first(self):
        from db import schemas
        from federation.core import delete_pairing_code

        with (
            patch("federation.core.DbManager.update_records") as update,
            patch("federation.core.DbManager.delete_records") as delete,
        ):
            delete_pairing_code(3)
        update.assert_called_once()
        assert update.call_args.args[0] is schemas.FederationPairingRequest
        assert list(update.call_args.args[2].values()) == [None]
        delete.assert_called_once()
        assert delete.call_args.args[0] is schemas.FederationPairingCode

    def test_edit_submit_renames_peer(self):
        from handlers.federation_cmds import handle_edit_external_connection_submit

        peer = SimpleNamespace(instance_id="aabb", name="Old Name", private_key_encrypted=None)
        body = {
            "user": {"id": "U_ADMIN"},
            "team": {"id": "T1"},
            "view": {
                "private_metadata": json.dumps({"instance_id": "aabb"}),
                "state": {
                    "values": {
                        "edit_external_connection_name": {"edit_external_connection_name": {"value": "Partner Org"}},
                        "select_edit_external_workspaces": {
                            "select_edit_external_workspaces": {"selected_options": [{"value": "10"}]}
                        },
                    }
                },
            },
        }
        with (
            patch("handlers.federation_cmds.helpers.federation_enabled", return_value=True),
            patch("handlers.federation_cmds._require_primary_admin", return_value=SimpleNamespace(id=1, team_id="T1")),
            patch("handlers.federation_cmds.DbManager.get_record", return_value=peer),
            patch("handlers.federation_cmds.DbManager.update_records") as update,
            patch("handlers.federation_cmds.replace_federation_allowlist") as replace,
            patch("handlers.federation_cmds.invalidate_fed_ws_for_sync_cache"),
            patch("handlers.federation_cmds.federation.push_allowed_workspaces"),
            patch("federation.replicate.replicate_peer_snapshot"),
            patch("handlers.federation_cmds.builders.refresh_home_tab_for_workspace"),
        ):
            handle_edit_external_connection_submit(body, MagicMock(), MagicMock(), {})
        update.assert_called_once()
        assert "Partner Org" in update.call_args.args[2].values()
        replace.assert_called_once_with("aabb", [10])

"""Unit tests for syncbot.app.view_ack and main_response (ack + lazy work)."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

import app as app_module  # noqa: E402
from slack import actions  # noqa: E402


def _body_view_submit(callback_id: str) -> dict:
    return {
        "type": "view_submission",
        "team_id": "T001",
        "view": {"callback_id": callback_id},
    }


class TestViewAck:
    """Production ``view_ack``: deferred views get custom ack kwargs."""

    def test_returns_dict_uses_ack_kwargs(self):
        ack = MagicMock()
        context: dict = {}

        def ack_handler(b, c, ctx):
            return {
                "response_action": "errors",
                "errors": {actions.CONFIG_BACKUP_RESTORE_JSON_INPUT: "bad"},
            }

        custom = {actions.CONFIG_BACKUP_RESTORE_SUBMIT: ack_handler}
        with patch.object(app_module, "VIEW_ACK_MAPPER", custom):
            app_module.view_ack(
                _body_view_submit(actions.CONFIG_BACKUP_RESTORE_SUBMIT),
                MagicMock(),
                MagicMock(),
                ack,
                context,
            )

        ack.assert_called_once()
        assert ack.call_args.kwargs["response_action"] == "errors"
        assert "errors" in ack.call_args.kwargs

    def test_returns_none_calls_empty_ack(self):
        ack = MagicMock()
        context: dict = {}

        def ack_handler(b, c, ctx):
            return None

        custom = {actions.CONFIG_CREATE_SYNC_SUBMIT: ack_handler}
        with patch.object(app_module, "VIEW_ACK_MAPPER", custom):
            app_module.view_ack(
                _body_view_submit(actions.CONFIG_CREATE_SYNC_SUBMIT),
                MagicMock(),
                MagicMock(),
                ack,
                context,
            )

        ack.assert_called_once_with()

    def test_unknown_callback_calls_empty_ack(self):
        ack = MagicMock()
        context: dict = {}
        with patch.object(app_module, "VIEW_ACK_MAPPER", {}):
            app_module.view_ack(_body_view_submit("unknown_callback"), MagicMock(), MagicMock(), ack, context)
        ack.assert_called_once_with()

    def test_ack_handler_exception_still_acks(self):
        ack = MagicMock()
        context: dict = {}

        def ack_handler(b, c, ctx):
            raise RuntimeError("unknown column")

        custom = {actions.CONFIG_CREATE_SYNC_SUBMIT: ack_handler}
        with patch.object(app_module, "VIEW_ACK_MAPPER", custom):
            app_module.view_ack(
                _body_view_submit(actions.CONFIG_CREATE_SYNC_SUBMIT),
                MagicMock(),
                MagicMock(),
                ack,
                context,
            )

        ack.assert_called_once_with()


class TestMainResponseLocalDevViewSubmission:
    """With LOCAL_DEVELOPMENT, main_response runs ack + work in one call."""

    @patch.object(app_module, "LOCAL_DEVELOPMENT", True)
    def test_non_deferred_ack_before_handler(self):
        ack = MagicMock()
        context: dict = {}

        def handler(b, c, log, ctx):
            assert ack.call_count == 1
            return None

        cid = actions.CONFIG_SETTINGS_SUBMIT
        custom = {cid: handler}
        with (
            patch.object(app_module, "MAIN_MAPPER", {"view_submission": custom}),
            patch.object(app_module, "emit_metric"),
        ):
            app_module.main_response(_body_view_submit(cid), MagicMock(), MagicMock(), ack, context)

        ack.assert_called_once_with()


class TestMainResponseProdViewSubmission:
    """Production main_response (lazy): does not call ack for view_submission."""

    @patch.object(app_module, "LOCAL_DEVELOPMENT", False)
    def test_view_submission_skips_ack_in_main_response(self):
        ack = MagicMock()
        context: dict = {}

        def handler(b, c, log, ctx):
            return None

        cid = actions.CONFIG_SETTINGS_SUBMIT
        custom = {cid: handler}
        with (
            patch.object(app_module, "MAIN_MAPPER", {"view_submission": custom}),
            patch.object(app_module, "emit_metric"),
        ):
            app_module.main_response(_body_view_submit(cid), MagicMock(), MagicMock(), ack, context)

        ack.assert_not_called()


class TestLambdaHandler:
    """AWS Lambda :func:`~app.handler` branches (migrate, warmup, Slack)."""

    def test_handler_migrate_event_calls_initialize_database(self):
        with patch.object(app_module, "initialize_database") as mock_init:
            result = app_module.handler({"action": "migrate"}, {})
        mock_init.assert_called_once()
        assert result["statusCode"] == 200
        assert json.loads(result["body"]) == {"status": "ok", "action": "migrate"}

    def test_handler_warmup_scheduler_returns_ok(self):
        with patch.object(app_module, "SlackRequestHandler") as mock_srh:
            result = app_module.handler({"source": "aws.scheduler"}, {})
        mock_srh.assert_not_called()
        assert result["statusCode"] == 200
        assert json.loads(result["body"]) == {"status": "ok", "action": "warmup"}

    def test_handler_warmup_events_returns_ok(self):
        with patch.object(app_module, "SlackRequestHandler") as mock_srh:
            result = app_module.handler({"source": "aws.events"}, {})
        mock_srh.assert_not_called()
        assert result["statusCode"] == 200
        assert json.loads(result["body"])["action"] == "warmup"

    def test_handler_slack_event_delegates_to_bolt(self):
        mock_handle = MagicMock(return_value={"statusCode": 200, "body": "{}"})
        with patch.object(app_module, "SlackRequestHandler") as mock_srh_class:
            mock_srh_class.return_value.handle = mock_handle
            app_module.handler({"httpMethod": "POST", "path": "/slack/events", "body": "{}"}, {})
        mock_srh_class.assert_called_once_with(app=app_module.app)
        mock_handle.assert_called_once()

    def test_handler_raises_when_lambda_adapter_missing(self):
        with (
            patch.object(app_module, "SlackRequestHandler", None),
            pytest.raises(RuntimeError, match="Lambda adapter is unavailable"),
        ):
            app_module.handler({"httpMethod": "POST", "path": "/slack/events", "body": "{}"}, {})

    def test_get_favicon_is_not_treated_as_oauth_install(self):
        with patch.object(app_module, "SlackRequestHandler") as mock_srh:
            result = app_module.handler(
                {"requestContext": {"http": {"method": "GET"}}, "rawPath": "/favicon.ico"},
                {},
            )
        mock_srh.assert_not_called()
        assert result["statusCode"] == 404

    def test_get_install_is_delegated_to_bolt(self):
        mock_handle = MagicMock(
            return_value={
                "statusCode": 302,
                "headers": {"location": "https://slack.com/oauth", "set-cookie": "slack-app-oauth-state=abc"},
                "body": "",
            }
        )
        with patch.object(app_module, "SlackRequestHandler") as mock_srh_class:
            mock_srh_class.return_value.handle = mock_handle
            result = app_module.handler(
                {"requestContext": {"http": {"method": "GET"}}, "rawPath": "/slack/install"},
                {},
            )
        mock_handle.assert_called_once()
        assert result["statusCode"] == 302
        assert "set-cookie" not in {k.lower() for k in (result.get("headers") or {})}
        assert result["cookies"] == ["slack-app-oauth-state=abc"]

    def test_get_install_remembers_public_base_from_host_header(self):
        mock_handle = MagicMock(return_value={"statusCode": 302, "headers": {}, "body": ""})
        with patch.object(app_module, "SlackRequestHandler") as mock_srh_class:
            mock_srh_class.return_value.handle = mock_handle
            app_module.handler(
                {
                    "requestContext": {"http": {"method": "GET"}},
                    "rawPath": "/slack/install",
                    "headers": {"host": "fn.lambda-url.us-east-1.on.aws", "x-forwarded-proto": "https"},
                },
                {},
            )
        from helpers.oauth import get_public_base_url

        assert get_public_base_url() == "https://fn.lambda-url.us-east-1.on.aws"

    def test_federation_path_is_404_when_settings_off(self):
        with (
            patch.object(app_module, "federation_enabled", return_value=False),
            patch.object(app_module, "SlackRequestHandler") as mock_srh,
        ):
            result = app_module.handler(
                {
                    "requestContext": {"http": {"method": "GET"}},
                    "rawPath": "/api/federation/ping",
                    "headers": {"host": "fn.example"},
                },
                {},
            )
        mock_srh.assert_not_called()
        assert result["statusCode"] == 404


class TestAsFunctionUrlResponse:
    def test_moves_set_cookie_header_to_cookies_array(self):
        resp = app_module._as_function_url_response(
            {
                "statusCode": 302,
                "headers": {"Location": "https://slack.com", "Set-Cookie": "slack-app-oauth-state=xyz"},
                "body": "",
            }
        )
        assert resp["cookies"] == ["slack-app-oauth-state=xyz"]
        assert "Set-Cookie" not in resp["headers"]
        assert resp["headers"]["Location"] == "https://slack.com"

    def test_leaves_responses_without_set_cookie_unchanged(self):
        original = {"statusCode": 200, "headers": {"Content-Type": "text/plain"}, "body": "ok"}
        assert app_module._as_function_url_response(original) is original

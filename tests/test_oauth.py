"""Unit tests for OAuth flow construction."""

import os
from unittest.mock import patch

from slack_manifest_scopes import USER_SCOPES

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from helpers.oauth import get_oauth_flow


class TestGetOAuthFlow:
    @patch("helpers.oauth.constants.LOCAL_DEVELOPMENT", True)
    @patch.dict(os.environ, {}, clear=True)
    def test_local_dev_without_oauth_credentials_returns_none(self):
        assert get_oauth_flow() is None

    @patch("helpers.oauth.constants.LOCAL_DEVELOPMENT", True)
    @patch.dict(
        os.environ,
        {
            "SLACK_CLIENT_ID": "cid",
            "SLACK_CLIENT_SECRET": "csecret",
            "SLACK_BOT_SCOPES": "chat:write,channels:read",
        },
        clear=True,
    )
    @patch("db.get_engine")
    @patch("helpers.oauth.SQLAlchemyOAuthStateStore")
    @patch("helpers.oauth.EncryptedSQLAlchemyInstallationStore")
    def test_local_dev_with_credentials_uses_sql_stores(
        self,
        mock_installation_store_cls,
        mock_state_store_cls,
        mock_get_engine,
    ):
        engine = object()
        mock_get_engine.return_value = engine

        flow = get_oauth_flow()

        assert flow is not None
        assert flow.settings.install_page_rendering_enabled is False
        mock_get_engine.assert_called_once_with()
        mock_installation_store_cls.assert_called_once_with(client_id="cid", engine=engine)
        mock_state_store_cls.assert_called_once_with(expiration_seconds=600, engine=engine)

    @patch("helpers.oauth.constants.LOCAL_DEVELOPMENT", False)
    @patch.dict(
        os.environ,
        {
            "SLACK_CLIENT_ID": "prod-cid",
            "SLACK_CLIENT_SECRET": "prod-secret",
            "SLACK_BOT_SCOPES": "chat:write,groups:read",
        },
        clear=True,
    )
    @patch("db.get_engine")
    @patch("helpers.oauth.SQLAlchemyOAuthStateStore")
    @patch("helpers.oauth.EncryptedSQLAlchemyInstallationStore")
    def test_production_uses_sql_stores_without_s3(
        self,
        mock_installation_store_cls,
        mock_state_store_cls,
        mock_get_engine,
    ):
        engine = object()
        mock_get_engine.return_value = engine

        flow = get_oauth_flow()

        assert flow is not None
        assert flow.settings.scopes == ["chat:write", "groups:read"]
        assert flow.settings.user_scopes == list(USER_SCOPES)
        assert flow.settings.install_page_rendering_enabled is False
        mock_get_engine.assert_called_once_with()
        mock_installation_store_cls.assert_called_once_with(client_id="prod-cid", engine=engine)
        mock_state_store_cls.assert_called_once_with(expiration_seconds=600, engine=engine)

    @patch("helpers.oauth.constants.LOCAL_DEVELOPMENT", True)
    @patch.dict(
        os.environ,
        {
            "SLACK_CLIENT_ID": "cid",
            "SLACK_CLIENT_SECRET": "csecret",
            "SLACK_BOT_SCOPES": "chat:write",
            "SLACK_USER_SCOPES": "chat:write,users:read",
        },
        clear=True,
    )
    @patch("db.get_engine")
    @patch("helpers.oauth.SQLAlchemyOAuthStateStore")
    @patch("helpers.oauth.EncryptedSQLAlchemyInstallationStore")
    def test_slack_user_scopes_env_overrides_default(
        self,
        mock_installation_store_cls,
        mock_state_store_cls,
        mock_get_engine,
    ):
        mock_get_engine.return_value = object()

        flow = get_oauth_flow()

        assert flow is not None
        assert flow.settings.user_scopes == ["chat:write", "users:read"]
        assert flow.settings.callback_options is not None


class TestPublicBaseFromHeaders:
    def test_host_and_forwarded_proto(self):
        from helpers.oauth import public_base_from_headers

        assert (
            public_base_from_headers({"Host": "abc.lambda-url.us-east-1.on.aws", "X-Forwarded-Proto": "https"})
            == "https://abc.lambda-url.us-east-1.on.aws"
        )

    def test_bolt_list_headers(self):
        from helpers.oauth import public_base_from_headers

        assert (
            public_base_from_headers({"host": ["example.run.app"], "x-forwarded-proto": ["https"]})
            == "https://example.run.app"
        )

    def test_missing_host(self):
        from helpers.oauth import public_base_from_headers

        assert public_base_from_headers({"x-forwarded-proto": "https"}) is None

    def test_capture_sets_context_and_remembered_host(self):
        from helpers.oauth import capture_public_base, get_public_base_url

        context: dict = {}
        assert (
            capture_public_base({"host": "fn.example", "x-forwarded-proto": "https"}, context) == "https://fn.example"
        )
        assert context["public_base_url"] == "https://fn.example"
        assert get_public_base_url() == "https://fn.example"


class TestGetPublicBaseUrl:
    def test_context_wins_over_remembered_host(self):
        from helpers.oauth import get_public_base_url, remember_public_base

        remember_public_base("https://cached.example")
        assert (
            get_public_base_url({"public_base_url": "https://from-request.example"}) == "https://from-request.example"
        )

    def test_remembered_host_when_context_empty(self):
        from helpers.oauth import get_public_base_url, remember_public_base

        remember_public_base("https://cached.example")
        assert get_public_base_url({}) == "https://cached.example"

    def test_legacy_env_is_ignored_and_warned_once(self, monkeypatch, caplog):
        import helpers.oauth as oauth_mod

        monkeypatch.setenv("SYNCBOT_PUBLIC_URL", "https://legacy.example")
        oauth_mod._LEGACY_PUBLIC_URL_WARNED = False
        remember = oauth_mod.remember_public_base
        remember("https://from-host.example")

        with caplog.at_level("WARNING", logger="helpers.oauth"):
            assert oauth_mod.get_public_base_url() == "https://from-host.example"
            assert oauth_mod.get_public_base_url() == "https://from-host.example"

        warnings = [r.message for r in caplog.records if "SYNCBOT_PUBLIC_URL is ignored" in r.message]
        assert len(warnings) == 1

    def test_falls_back_to_persisted_instance_setting(self):
        from helpers.oauth import get_public_base_url

        with (
            patch("helpers.oauth._cache_get", return_value=None),
            patch("helpers.oauth._load_persisted_public_base", return_value="https://from-db.example"),
        ):
            assert get_public_base_url() == "https://from-db.example"

    def test_lambda_event_uses_request_context_domain(self):
        from helpers.oauth import public_base_from_lambda_event

        with patch("helpers.oauth.constants.LOCAL_DEVELOPMENT", False):
            assert (
                public_base_from_lambda_event(
                    {"requestContext": {"domainName": "abc.lambda-url.us-east-1.on.aws"}, "headers": {}}
                )
                == "https://abc.lambda-url.us-east-1.on.aws"
            )


class TestRefreshHomeAfterOauthInstall:
    def test_publishes_home_for_the_authorizing_user(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from helpers.oauth import refresh_home_after_oauth_install

        installation = SimpleNamespace(team_id="T1", user_id="U9", bot_token="xoxb-bot")
        with patch("builders.build_home_tab") as build:
            refresh_home_after_oauth_install(installation)

        build.assert_called_once()
        body, client, _logger, context = build.call_args.args[:4]
        assert body == {"team": {"id": "T1"}, "user": {"id": "U9"}}
        assert client.token == "xoxb-bot"
        assert build.call_args.kwargs["user_id"] == "U9"
        assert context == {}

    def test_skips_when_installation_is_incomplete(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from helpers.oauth import refresh_home_after_oauth_install

        with patch("builders.build_home_tab") as build:
            refresh_home_after_oauth_install(SimpleNamespace(team_id="T1", user_id=None, bot_token="xoxb"))

        build.assert_not_called()


class TestSkipEmptyUserInstallations:
    def test_tokenless_per_user_row_is_treated_as_missing(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from helpers.oauth import _skip_empty_user_installations

        store = MagicMock()
        store.find_installation.return_value = SimpleNamespace(bot_token=None, user_token=None)
        _skip_empty_user_installations(store)

        assert store.find_installation(enterprise_id=None, team_id="T1", user_id="U9") is None

    def test_row_with_copied_bot_token_but_no_user_token_is_treated_as_missing(self):
        """Slack's store copies the workspace bot token onto a leftover user row."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from helpers.oauth import _skip_empty_user_installations

        store = MagicMock()
        store.find_installation.return_value = SimpleNamespace(bot_token="xoxb-copied", user_token=None)
        _skip_empty_user_installations(store)

        assert store.find_installation(enterprise_id=None, team_id="T1", user_id="U9") is None

    def test_row_with_user_token_is_kept(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from helpers.oauth import _skip_empty_user_installations

        row = SimpleNamespace(bot_token=None, user_token="xoxp-1")
        store = MagicMock()
        store.find_installation.return_value = row
        _skip_empty_user_installations(store)

        assert store.find_installation(enterprise_id=None, team_id="T1", user_id="U9") is row

    def test_team_lookup_without_user_id_is_unchanged(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from helpers.oauth import _skip_empty_user_installations

        row = SimpleNamespace(bot_token=None, user_token=None)
        store = MagicMock()
        store.find_installation.return_value = row
        _skip_empty_user_installations(store)

        assert store.find_installation(enterprise_id=None, team_id="T1") is row


class TestOauthSuccessCallback:
    def test_refreshes_home_then_renders_the_default_success_page(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        from helpers.oauth import _oauth_success

        installation = SimpleNamespace(team_id="T1", user_id="U1", bot_token="xoxb")
        default = SimpleNamespace(success=MagicMock(return_value="ok-page"))
        args = SimpleNamespace(installation=installation, default=default)
        with patch("helpers.oauth.refresh_home_after_oauth_install") as refresh:
            result = _oauth_success(args)

        refresh.assert_called_once_with(installation)
        default.success.assert_called_once_with(args)
        assert result == "ok-page"


class TestFederationGetPublicUrl:
    def test_uses_remembered_host_and_ignores_legacy_env(self, monkeypatch):
        from federation.core import get_public_url
        from helpers.oauth import remember_public_base

        monkeypatch.setenv("SYNCBOT_PUBLIC_URL", "https://legacy.example")
        remember_public_base("https://from-host.example")

        assert get_public_url() == "https://from-host.example"

    def test_prefers_request_context(self):
        from federation.core import get_public_url
        from helpers.oauth import remember_public_base

        remember_public_base("https://cached.example")
        assert get_public_url({"public_base_url": "https://ctx.example"}) == "https://ctx.example"

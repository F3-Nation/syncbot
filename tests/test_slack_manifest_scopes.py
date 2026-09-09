"""slack-manifest.json stays aligned with syncbot/slack_manifest_scopes.py."""

import json
import re
from pathlib import Path

from slack_manifest_scopes import (
    BOT_SCOPES,
    USER_PERMISSION_GROUPS,
    USER_SCOPES,
    bot_scopes_comma_separated,
    user_scopes_comma_separated,
)


def _manifest() -> dict:
    root = Path(__file__).resolve().parent.parent
    return json.loads(root.joinpath("slack-manifest.json").read_text(encoding="utf-8"))


def test_slack_manifest_bot_scopes_match_constants():
    bot = _manifest()["oauth_config"]["scopes"]["bot"]
    assert bot == list(BOT_SCOPES)
    assert {"emoji:read", "usergroups:read"} <= set(bot)
    assert "im:write" in bot
    assert not {"bookmarks:read", "bookmarks:write", "pins:read", "pins:write"} & set(bot)


def test_slack_manifest_user_scopes_match_constants():
    user = _manifest()["oauth_config"]["scopes"]["user"]
    assert user == list(USER_SCOPES)
    assert "im:write" not in user
    assert not {"bookmarks:read", "bookmarks:write", "pins:read", "pins:write"} & set(user)


def test_sam_template_slack_oauth_default_matches_bot_scopes():
    """infra/aws/template.yaml SlackOauthBotScopes Default must match BOT_SCOPES."""
    root = Path(__file__).resolve().parent.parent
    text = root.joinpath("infra/aws/template.yaml").read_text(encoding="utf-8")
    m = re.search(
        r'^\s*SlackOauthBotScopes:\s*\n(?:^\s+.*\n)*?\s*Default:\s*"([^"]+)"',
        text,
        re.MULTILINE,
    )
    assert m, "SlackOauthBotScopes Default not found in template.yaml"
    assert m.group(1) == bot_scopes_comma_separated()


def test_sam_template_slack_user_oauth_default_matches_user_scopes():
    """infra/aws/template.yaml SlackOauthUserScopes Default must match USER_SCOPES."""
    root = Path(__file__).resolve().parent.parent
    text = root.joinpath("infra/aws/template.yaml").read_text(encoding="utf-8")
    m = re.search(
        r'^\s*SlackOauthUserScopes:\s*\n(?:^\s+.*\n)*?\s*Default:\s*"([^"]*)"',
        text,
        re.MULTILINE,
    )
    assert m, "SlackOauthUserScopes Default not found in template.yaml"
    assert m.group(1) == user_scopes_comma_separated()


def test_bot_scopes_comma_separated_roundtrip():
    assert bot_scopes_comma_separated().split(",") == list(BOT_SCOPES)


def test_user_scopes_comma_separated_roundtrip():
    s = user_scopes_comma_separated()
    assert [x.strip() for x in s.split(",") if x.strip()] == list(USER_SCOPES)


def test_every_user_scope_is_in_exactly_one_permission_group():
    """The Home tab lists groups, not raw scopes, so the catalog must cover USER_SCOPES."""
    grouped: list[str] = []
    for _label, scopes in USER_PERMISSION_GROUPS:
        grouped.extend(scopes)
    assert sorted(grouped) == sorted(USER_SCOPES)
    assert len(grouped) == len(set(grouped))


def test_slack_manifest_subscribes_to_uninstall_and_token_revoke():
    """Bolt's store cleanup needs both events; the generated stage manifests copy this list."""
    events = _manifest()["settings"]["event_subscriptions"]["bot_events"]
    assert "app_uninstalled" in events
    assert "tokens_revoked" in events


def test_permission_group_labels_are_plain_language():
    """People should not have to read Slack API names on the Home tab."""
    for label, scopes in USER_PERMISSION_GROUPS:
        assert ":" not in label
        assert label[0].isupper()
        assert 1 <= len(label.split()) <= 4
        assert scopes


def test_gcp_variables_slack_scope_defaults_match_constants():
    root = Path(__file__).resolve().parent.parent
    text = root.joinpath("infra/gcp/variables.tf").read_text(encoding="utf-8")
    bot = re.search(
        r'variable "slack_bot_scopes"\s*\{[^}]*default\s*=\s*"([^"]+)"',
        text,
        re.DOTALL,
    )
    user = re.search(
        r'variable "slack_user_scopes"\s*\{[^}]*default\s*=\s*"([^"]+)"',
        text,
        re.DOTALL,
    )
    assert bot, "slack_bot_scopes default not found"
    assert user, "slack_user_scopes default not found"
    assert bot.group(1) == bot_scopes_comma_separated()
    assert user.group(1) == user_scopes_comma_separated()


def test_aws_deploy_script_scope_fallbacks_match_constants():
    root = Path(__file__).resolve().parent.parent
    for rel in (
        "infra/aws/scripts/ci_sam_deploy_with_fallback.sh",
        "infra/aws/scripts/deploy.sh",
    ):
        text = root.joinpath(rel).read_text(encoding="utf-8")
        bot_m = re.search(r"SLACK_BOT_SCOPES:-([^}]+)\}", text)
        user_m = re.search(r"SLACK_USER_SCOPES:-([^}]+)\}", text)
        assert bot_m and user_m, f"scope fallbacks missing in {rel}"
        assert bot_m.group(1) == bot_scopes_comma_separated()
        assert user_m.group(1) == user_scopes_comma_separated()

"""Canonical Slack OAuth scopes — keep in sync with repo root ``slack-manifest.json``.

``oauth_config.scopes.bot`` must match :envvar:`SLACK_BOT_SCOPES` (comma-separated).
``oauth_config.scopes.user`` must match :envvar:`SLACK_USER_SCOPES` (comma-separated).
This app always uses both **bot** and **user** scopes; ``USER_SCOPES`` is non-empty and must
match the manifest ``user`` array (order included). When changing scopes, edit this module and
``slack-manifest.json`` / ``slack-manifest_test.json`` together, then AWS SAM defaults,
GCP ``slack_user_scopes``, and env examples.

``USER_PERMISSION_GROUPS`` is the Home tab Authorize list. How to extend it is
documented on that constant (and in ``AGENTS.md`` / ``docs/AI_AGENTS.md``).
"""

from __future__ import annotations

# --- Must match slack-manifest.json oauth_config.scopes.bot (order as in manifest) ---

BOT_SCOPES: tuple[str, ...] = (
    "app_mentions:read",
    "channels:history",
    "channels:join",
    "channels:read",
    "channels:manage",
    "chat:write",
    "chat:write.customize",
    "emoji:read",
    "files:read",
    "files:write",
    "groups:history",
    "groups:read",
    "groups:write",
    "im:write",
    "reactions:read",
    "reactions:write",
    "team:read",
    "usergroups:read",
    "users:read",
    "users:read.email",
)

# --- Must match slack-manifest.json oauth_config.scopes.user (order as in manifest) ---

USER_SCOPES: tuple[str, ...] = (
    "chat:write",
    "channels:history",
    "channels:read",
    "files:read",
    "files:write",
    "groups:history",
    "groups:read",
    "groups:write",
    "reactions:read",
    "reactions:write",
    "team:read",
    "users:read",
    "users:read.email",
)

# Home tab Authorize section: plain-language groups derived from USER_SCOPES.
#
# How this list was built (keep new rows consistent):
# 1. Start from USER_SCOPES / the manifest ``user`` array — never bot scopes.
#    Those are the app, not the person authorizing.
# 2. Look up each scope on Slack's scopes reference
#    (https://docs.slack.dev/reference/scopes) for what it grants on a *user*
#    token, then write 2–4 ordinary words. Never paste ``channels:history``.
#    Do not start with "Can" or "Allow". Capitalize "Channel" the way the rest
#    of the product does.
# 3. Fold scopes people experience as one capability: history+read of the same
#    conversation type, files read+write, reactions read+write, users.read plus
#    users:read.email. Keep ``groups:write`` on its own line ("Manage private
#    Channels") because inviting SyncBot in is a different promise than viewing
#    a private Channel. Singleton groups: chat:write, team:read.
# 4. A group is already-allowed only when *every* scope in it is on the stored
#    user token. Incomplete pairs stay under Needed. First-time authorize hides
#    the already-allowed list (it would be empty); a later scope add shows it
#    with checkmarks so re-authorize does not look like a redo.
# 5. ``tests/test_slack_manifest_scopes.py`` requires every USER_SCOPE in
#    exactly one group and labels with no ``:``. Add a new user scope to an
#    existing group when it is the twin of something already listed, else a
#    new group. The Home tab reads this constant; do not duplicate the labels
#    in the builder.
USER_PERMISSION_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Post messages", ("chat:write",)),
    ("View public Channels", ("channels:history", "channels:read")),
    ("View private Channels", ("groups:history", "groups:read")),
    ("Manage private Channels", ("groups:write",)),
    ("Share files", ("files:read", "files:write")),
    ("Use emoji reactions", ("reactions:read", "reactions:write")),
    ("View workspace info", ("team:read",)),
    ("View people", ("users:read", "users:read.email")),
)


def bot_scopes_comma_separated() -> str:
    """Return the bot scope string for SLACK_BOT_SCOPES / CloudFormation."""
    return ",".join(BOT_SCOPES)


def user_scopes_comma_separated() -> str:
    """Return the user scope string for SLACK_USER_SCOPES / CloudFormation / Terraform."""
    return ",".join(USER_SCOPES)

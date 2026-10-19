"""Reaction add/remove event handlers."""

import logging

from slack_sdk.web import WebClient

import helpers
from db.event_claims import run_claimed
from logger import log_debug


def _sync_reaction_records(body: dict, client: WebClient, reacted_records: list[tuple]) -> None:
    """Publish a reaction add/remove through the target pipeline."""
    event = body.get("event", {})
    reaction = event.get("reaction")
    user_id = event.get("user")
    item = event.get("item", {})
    channel_id = item.get("channel")
    event_type = event.get("type")
    action = helpers.ACTION_ADD if event_type == "reaction_added" else helpers.ACTION_REMOVE
    source_rows = helpers.get_publishing_post_records(reacted_records, channel_id)
    if not source_rows:
        return
    post_meta, source_sync_channel, source_workspace = source_rows[0]
    user_name, user_profile_url = helpers.get_user_info(client, user_id) if user_id else (None, None)
    people = [helpers.build_people_entry(user_id, name=user_name, avatar_url=user_profile_url)] if user_id else None
    raw_event_ts = event.get("event_ts") or event.get("event_time")
    event_ts = helpers.slack_message_ts(raw_event_ts) if raw_event_ts else None
    envelope = helpers.build_envelope(
        kind=helpers.KIND_REACTION,
        action=action,
        post_id=str(post_meta.post_id),
        source_channel_id=channel_id,
        source_workspace_id=source_workspace.id,
        source_team_id=helpers.get_team_id_from_body(body),
        source_sync_channel_id=source_sync_channel.id,
        people=people,
        reaction=reaction,
        source_user_id=user_id,
        user_name=user_name,
        user_avatar_url=user_profile_url,
        workspace_name=helpers.resolve_workspace_name(source_workspace),
        source_ts=item.get("ts"),
        event_ts=event_ts,
    )
    helpers.run_sync_pipeline(
        envelope,
        source_channel_id=channel_id,
        source_client=client,
        source_sync_channel=source_sync_channel,
    )


def handle_reaction(
    body: dict,
    client: WebClient,
    logger: logging.Logger,
    context: dict,
) -> None:
    """Sync reaction add/remove to linked channels that receive them."""
    event = body.get("event", {})
    reaction = event.get("reaction")
    user_id = event.get("user")
    item = event.get("item", {})
    item_type = item.get("type")
    channel_id = item.get("channel")
    msg_ts = item.get("ts")
    event_type = event.get("type")

    if event_type not in ("reaction_added", "reaction_removed"):
        return

    if not reaction or not channel_id or not msg_ts or item_type != "message":
        return

    own_user_id = helpers.get_own_bot_user_id(client, context)
    if own_user_id and user_id == own_user_id:
        return

    team_id = helpers.get_team_id_from_body(body)

    def _sync_reaction() -> None:
        if team_id and user_id and reaction and channel_id and msg_ts:
            fingerprint = helpers.reaction_echo_fingerprint(channel_id, msg_ts, reaction)
            if helpers.take_user_action_echo(str(team_id), user_id, event_type, fingerprint):
                return
        if not helpers.channel_has_membership(channel_id):
            return
        if not helpers.origin_publishes_anywhere(channel_id):
            return

        reacted_records = helpers.get_post_records(msg_ts)
        if not reacted_records:
            log_debug("reaction_no_post_meta", msg_ts=msg_ts, channel_id=channel_id)
            # Message PostMeta may still be in flight; do not complete the claim.
            return False
        _sync_reaction_records(body, client, reacted_records)

    run_claimed(body, _sync_reaction)

"""Invariant: deferred-ack view callback IDs stay registered in VIEW_ACK_MAPPER / VIEW_MAPPER."""

from routing import ACTION_MAPPER, VIEW_ACK_MAPPER, VIEW_MAPPER
from slack import actions
from slack.deferred_ack_views import DEFERRED_ACK_VIEW_CALLBACK_IDS

# Dropped Slack ids must not stay routed. Refresh Home after a rename.
_REMOVED_SLACK_IDS = (
    "publish_channel",
    "publish_channel_select",
    "publish_channel_submit",
    "publish_participation",
    "publish_participation_submit",
    "publish_reaction_style",
    "unpublish_channel",
    "stop_sync",
    "confirm_stop_sync",
    "subscribe_channel",
    "subscribe_channel_select",
    "subscribe_channel_submit",
    "subscribe_participation",
    "subscribe_participation_submit",
    "subscribe_reaction_style",
    "join_sync_select",
    "manage_user_matching",
    "user_mapping_auto_match",
)


def test_deferred_ack_matches_view_ack_mapper():
    assert frozenset(VIEW_ACK_MAPPER.keys()) == DEFERRED_ACK_VIEW_CALLBACK_IDS


def test_create_and_join_submit_on_the_final_modal():
    assert actions.CONFIG_CREATE_SYNC_SUBMIT in VIEW_ACK_MAPPER
    assert actions.CONFIG_CREATE_SYNC_SUBMIT in VIEW_MAPPER
    assert actions.CONFIG_JOIN_SYNC_SUBMIT in VIEW_ACK_MAPPER
    assert actions.CONFIG_JOIN_SYNC_SUBMIT in VIEW_MAPPER


def test_removed_slack_ids_are_not_routed():
    routed = set(ACTION_MAPPER) | set(VIEW_MAPPER) | set(VIEW_ACK_MAPPER)
    for leftover in _REMOVED_SLACK_IDS:
        assert leftover not in routed


def test_deferred_work_views_have_work_handlers():
    for callback_id in DEFERRED_ACK_VIEW_CALLBACK_IDS:
        assert callback_id in VIEW_MAPPER, f"missing VIEW_MAPPER work entry for {callback_id!r}"


def test_deferred_ack_set_is_nonempty():
    assert len(DEFERRED_ACK_VIEW_CALLBACK_IDS) >= 1

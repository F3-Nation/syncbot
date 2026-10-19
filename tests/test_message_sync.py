"""Tests for post record deduplication."""

from types import SimpleNamespace
from unittest.mock import patch

from helpers.post_meta import get_post_records
from helpers.sync_participation import get_channel_memberships


class TestFindChannelMembershipsDeduplication:
    def test_deduplicates_same_workspace_and_channel(self):
        ws = SimpleNamespace(id=42, team_id="T1", workspace_name="WS")
        sc_dup_a = SimpleNamespace(id=2, sync_id=7, channel_id="C999")
        sc_dup_b = SimpleNamespace(id=3, sync_id=7, channel_id="C999")

        with (
            patch("helpers.sync_participation._cache_get", return_value=None),
            patch("helpers.sync_participation._cache_set") as cache_set,
            patch(
                "helpers.sync_participation.DbManager.find_join_records2",
                return_value=[(sc_dup_a, ws), (sc_dup_b, ws)],
            ),
        ):
            result = get_channel_memberships("Csource")

        assert len(result) == 1
        assert result[0][0] is sc_dup_a
        assert result[0][1] is ws  # first wins among duplicates
        cache_set.assert_called_once()


class TestGetPostRecordsDeduplication:
    def test_deduplicates_same_workspace_and_channel(self):
        pm = SimpleNamespace(id=1, post_id="p1", ts=123.456789)
        ws = SimpleNamespace(id=42)
        sc_a = SimpleNamespace(id=10, channel_id="C777")
        sc_b = SimpleNamespace(id=11, channel_id="C777")

        with (
            patch("helpers.post_meta.DbManager.find_records", return_value=[pm]),
            patch(
                "helpers.post_meta.DbManager.find_join_records3",
                return_value=[(pm, sc_a, ws), (pm, sc_b, ws)],
            ),
        ):
            result = get_post_records("123.456789")

        assert len(result) == 1
        assert result[0][1] is sc_a

    def test_dedup_prefers_lower_post_meta_id(self):
        """Two PostMeta rows can share a post_id; the earlier row wins."""
        pm_file = SimpleNamespace(id=99, post_id="p1", ts=888.888)
        pm_text = SimpleNamespace(id=10, post_id="p1", ts=111.111)
        ws = SimpleNamespace(id=42)
        sc = SimpleNamespace(id=10, channel_id="C777")

        with (
            patch("helpers.post_meta.DbManager.find_records", return_value=[pm_file]),
            patch(
                "helpers.post_meta.DbManager.find_join_records3",
                return_value=[(pm_file, sc, ws), (pm_text, sc, ws)],
            ),
        ):
            result = get_post_records("888.888")

        assert len(result) == 1
        assert result[0][0].id == 10
        assert result[0][0].ts == 111.111


class TestFindPublishingPostRecords:
    def test_skips_subscribe_only_and_other_channels(self):
        from helpers.post_meta import get_publishing_post_records

        origin = SimpleNamespace(id=1, post_id="p1", source_workspace_id=1)
        copy = SimpleNamespace(id=2, post_id="p1", source_workspace_id=1)
        posted_copy = SimpleNamespace(id=3, post_id="p1", posted_as_user_id="U2", source_workspace_id=2)
        bot_copy = SimpleNamespace(id=4, post_id="p1", source_workspace_id=2)
        fed_copy = SimpleNamespace(id=5, post_id="p1", source_workspace_id=None)
        hub_pub = SimpleNamespace(id=11, channel_id="C_HUB", publishes=True)
        hub_sub = SimpleNamespace(id=12, channel_id="C_HUB", publishes=False)
        ao = SimpleNamespace(id=13, channel_id="C_AO", publishes=True)
        ws = SimpleNamespace(id=1)
        rows = [
            (origin, hub_pub, ws),
            (copy, hub_sub, ws),
            (copy, ao, ws),
            (posted_copy, hub_pub, ws),
            (bot_copy, hub_pub, ws),
            (fed_copy, hub_pub, ws),
        ]

        result = get_publishing_post_records(rows, "C_HUB")

        assert result == [(origin, hub_pub, ws)]

    def test_publishing_copy_on_this_channel_originates(self):
        from helpers.post_meta import get_publishing_post_records

        copy = SimpleNamespace(id=3, post_id="p1", posted_as_user_id="U2", source_workspace_id=1)
        hub_pub = SimpleNamespace(id=11, channel_id="C_HUB", publishes=True)
        ws = SimpleNamespace(id=2)
        assert get_publishing_post_records([(copy, hub_pub, ws)], "C_HUB") == [(copy, hub_pub, ws)]

    def test_reaction_notice_does_not_originate(self):
        from helpers.post_meta import get_publishing_post_records

        notice = SimpleNamespace(id=1, post_id="rxn-1", kind="reaction_notice")
        hub_pub = SimpleNamespace(id=11, channel_id="C_HUB", publishes=True)
        ws = SimpleNamespace(id=1)
        assert get_publishing_post_records([(notice, hub_pub, ws)], "C_HUB") == []

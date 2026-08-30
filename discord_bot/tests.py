import datetime

import discord
from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from discord_bot.cogs.channel_sync import ChannelSyncCog
from discord_bot.cogs.emoji_sync import EmojiSyncCog
from discord_bot.cogs.guild_sync import GuildSyncCog
from discord_bot.cogs.leaderboard_sync import LeaderboardSyncCog
from discord_bot.cogs.match_ticker import MatchTickerCog
from discord_bot.cogs.poll_creation import matches_needing_polls
from discord_bot.cogs.pool_onboarding import LEADERBOARD_PLACEHOLDER, PoolOnboardingCog
from discord_bot.cogs.reconciliation import ReconciliationCog
from discord_bot.cogs.remove_garbage import RemoveGarbageCog
from discord_bot.cogs.role_sync import RoleSyncCog
from discord_bot.components import FIGURE_SPACE, HALF_DIGIT, MuteRemindersButton
from discord_bot.constants import DISCORD_POLL_ANSWER_ORDER_MAP
from discord_bot.models import (
    ActiveMatchMessage,
    DiscordChannel,
    DiscordGuild,
    DiscordGuildPool,
    DiscordGuildRole,
    DiscordProfile,
    MatchMessageState,
    PoolNotificationPreference,
)
from discord_bot.services import aget_or_create_profile, aset_missing_vote_reminders
from predictions.models import (
    DEFAULT_REMINDER_LEAD_MINUTES,
    PoolConfiguration,
    PoolStageRule,
    Prediction,
    PredictionPool,
)
from sports.models import Competition, Match, MatchOutcome, MatchStatus, Season, Stage, StageType, Team

User = get_user_model()

# Discord component type ids (Components V2).
COMPONENT_BUTTON = 2
COMPONENT_SECTION = 9
COMPONENT_TEXT_DISPLAY = 10
COMPONENT_THUMBNAIL = 11
COMPONENT_SEPARATOR = 14
COMPONENT_CONTAINER = 17


class FakeVoter:
    def __init__(self, discord_id, name):
        self.id = discord_id
        self.name = name
        self.global_name = None


class FakePollAnswer:
    """Mimics a discord.py PollAnswer: an id plus an async voters() generator."""

    def __init__(self, answer_id, voter_ids):
        self.id = answer_id
        self._voter_ids = voter_ids

    async def voters(self):
        for voter_id in self._voter_ids:
            yield FakeVoter(voter_id, f"user{voter_id}")


class FakePoll:
    def __init__(self, answers):
        self.answers = answers


class FakeMessage:
    def __init__(self, poll):
        self.poll = poll


class FakeLeaderboardMessage:
    """The pinned leaderboard message: records what it was edited with."""

    def __init__(self, message_id=900, pinned=True):
        self.id = message_id
        self.pinned = pinned
        self.edits = []

    async def pin(self):
        self.pinned = True

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeChannel(discord.abc.Messageable):
    """Stands in for the channel (or legacy thread) a poll was posted in.

    It subclasses discord.abc.Messageable because resolve_message_container
    isinstance-checks its result before handing it back.
    """

    def __init__(self, message=None, channel_id=10):
        self._message = message
        self.id = channel_id

    async def _get_channel(self):
        return self

    async def fetch_message(self, message_id):
        return self._message


class FakeRole:
    def __init__(self, role_id, name, position=0, members=None):
        self.id = role_id
        self.name = name
        self.position = position
        self.members = members or []

    @property
    def mention(self):
        return f"<@&{self.id}>"


class FakeMember:
    def __init__(self, member_id, name="member", is_bot=False, global_name=None):
        self.id = member_id
        self.name = name
        self.bot = is_bot
        self.global_name = global_name

    @property
    def mention(self):
        return f"<@{self.id}>"


class FakeGuild:
    def __init__(self, guild_id, name, roles=None):
        self.id = guild_id
        self.name = name
        self.roles = roles or []

    def get_role(self, role_id):
        for role in self.roles:
            if role.id == role_id:
                return role
        return None


class FakeBot:
    def __init__(self, channel=None, guilds=None):
        self._channel = channel
        self.guilds = guilds or []

    def get_channel(self, channel_id):
        return self._channel

    async def fetch_channel(self, channel_id):
        return self._channel

    def get_guild(self, guild_id):
        for guild in self.guilds:
            if guild.id == guild_id:
                return guild
        return None


class ReconcileRolesTests(TestCase):
    """Covers the reconcile_roles fix: it used to deactivate DiscordChannel rows
    (filtered by role IDs) instead of the stale DiscordGuildRole rows."""

    def setUp(self):
        self.guild_row = DiscordGuild.objects.create(id=1, name="Test Guild")
        self.channel_row = DiscordChannel.objects.create(
            id=500, guild=self.guild_row, name="general", channel_type="text", is_active=True
        )
        self.stale_role = DiscordGuildRole.objects.create(
            id=999, guild=self.guild_row, name="Old Role", is_active=True
        )

    async def test_role_removed_from_guild_is_deactivated_without_touching_channels(self):
        fake_guild = FakeGuild(guild_id=self.guild_row.id, name="Test Guild", roles=[FakeRole(42, "Live Role")])
        cog = ReconciliationCog(bot=FakeBot(guilds=[fake_guild]))

        await cog.reconcile_roles()

        stale_role = await DiscordGuildRole.objects.aget(id=self.stale_role.id)
        self.assertFalse(stale_role.is_active)

        channel = await DiscordChannel.objects.aget(id=self.channel_row.id)
        self.assertTrue(channel.is_active)

    async def test_role_still_present_in_guild_stays_active(self):
        fake_guild = FakeGuild(
            guild_id=self.guild_row.id, name="Test Guild", roles=[FakeRole(self.stale_role.id, "Old Role")]
        )
        cog = ReconciliationCog(bot=FakeBot(guilds=[fake_guild]))

        await cog.reconcile_roles()

        role = await DiscordGuildRole.objects.aget(id=self.stale_role.id)
        self.assertTrue(role.is_active)


class ReconcileActivePollsTests(TestCase):
    """Covers the reconcile_active_polls fix: it used to only add/update
    predictions for currently observed votes and never delete ones no longer
    present, so a vote retracted while the bot was offline left a stale
    Prediction behind."""

    def setUp(self):
        self.competition = Competition.objects.create(name="World Cup")
        self.season = Season.objects.create(name="2026 World Cup", competition=self.competition, year=2026)
        self.stage = Stage.objects.create(season=self.season, name="Group A", stage_type=StageType.GROUP, level=1)
        self.home_team = Team.objects.create(name="Germany")
        self.away_team = Team.objects.create(name="Brazil")
        self.match = Match.objects.create(
            stage=self.stage,
            home_team=self.home_team,
            away_team=self.away_team,
            kickoff=timezone.now(),
            status=MatchStatus.SCHEDULED,
        )
        self.pool = PredictionPool.objects.create(name="Test Pool", season=self.season)
        self.guild = DiscordGuild.objects.create(id=1, name="Test Guild")
        self.channel = DiscordChannel.objects.create(id=10, guild=self.guild, name="general", channel_type="text")
        self.match_msg = ActiveMatchMessage.objects.create(
            match=self.match,
            guild=self.guild,
            pool=self.pool,
            channel=self.channel,
            poll_message_id=30,
            is_poll_finalized=False,
        )

        self.alice = User.objects.create_user(username="alice")
        self.alice_profile = DiscordProfile.objects.create(id=111, user=self.alice, username="alice")
        self.bob = User.objects.create_user(username="bob")
        self.bob_profile = DiscordProfile.objects.create(id=222, user=self.bob, username="bob")

    def make_cog(self, answers):
        message = FakeMessage(poll=FakePoll(answers=answers))
        channel = FakeChannel(message=message, channel_id=self.channel.id)
        return ReconciliationCog(bot=FakeBot(channel=channel))

    async def test_vote_retracted_while_offline_deletes_the_stale_prediction(self):
        # Alice previously voted HOME_WIN, but the current Discord poll state
        # (fetched below) only shows Bob voting - Alice must have retracted
        # her vote while the bot was offline.
        await Prediction.objects.acreate(
            pool=self.pool,
            match=self.match,
            user=self.alice,
            predicted_outcome=MatchOutcome.HOME_WIN,
        )
        cog = self.make_cog(answers=[FakePollAnswer(answer_id=1, voter_ids=[self.bob_profile.id])])

        await cog.reconcile_active_polls()

        alice_has_prediction = await Prediction.objects.filter(
            pool=self.pool, match=self.match, user=self.alice
        ).aexists()
        self.assertFalse(alice_has_prediction)

        bob_prediction = await Prediction.objects.aget(pool=self.pool, match=self.match, user=self.bob)
        self.assertEqual(bob_prediction.predicted_outcome, MatchOutcome.HOME_WIN)

    async def test_current_votes_are_synced_for_new_and_existing_predictions(self):
        await Prediction.objects.acreate(
            pool=self.pool,
            match=self.match,
            user=self.alice,
            predicted_outcome=MatchOutcome.AWAY_WIN,
        )
        cog = self.make_cog(
            answers=[
                FakePollAnswer(answer_id=1, voter_ids=[self.alice_profile.id]),
                FakePollAnswer(answer_id=3, voter_ids=[self.bob_profile.id]),
            ]
        )

        await cog.reconcile_active_polls()

        alice_prediction = await Prediction.objects.aget(pool=self.pool, match=self.match, user=self.alice)
        self.assertEqual(alice_prediction.predicted_outcome, MatchOutcome.HOME_WIN)

        bob_prediction = await Prediction.objects.aget(pool=self.pool, match=self.match, user=self.bob)
        self.assertEqual(bob_prediction.predicted_outcome, MatchOutcome.AWAY_WIN)


class PollAnswerOrderMapTests(TestCase):
    """Covers DISCORD_POLL_ANSWER_ORDER_MAP (discord_bot/constants.py).

    The map is indexed by 1-based Discord poll answer ids (slot 0 is a None
    placeholder), and both poll_creation and poll_listener read it - creation
    builds the answers in this order, the listener translates a vote back into
    a MatchOutcome by the same order. If they ever disagree, every vote is
    recorded as the wrong outcome, so the two directions are asserted here
    together.

    A stage type missing from the map makes poll_creation log an error and
    skip the match entirely, so every stage type a pool can use must be
    present."""

    def test_league_stages_offer_a_draw(self):
        """The NFL regular season is a LEAGUE stage and can end in a tie."""
        order = DISCORD_POLL_ANSWER_ORDER_MAP[StageType.LEAGUE]

        self.assertEqual(order, [None, MatchOutcome.HOME_WIN, MatchOutcome.DRAW, MatchOutcome.AWAY_WIN])

    def test_knockout_stages_offer_no_draw(self):
        """NFL playoff rounds cannot tie, so the poll has two answers."""
        order = DISCORD_POLL_ANSWER_ORDER_MAP[StageType.KNOCK_OUT]

        self.assertEqual(order, [None, MatchOutcome.HOME_WIN, MatchOutcome.AWAY_WIN])
        self.assertNotIn(MatchOutcome.DRAW, order)

    def test_every_stage_type_a_pool_uses_is_mapped(self):
        """OTHER is deliberately absent - an unclassified stage should fail
        loudly rather than have a poll layout guessed for it."""
        for stage_type in (StageType.GROUP, StageType.LEAGUE, StageType.KNOCK_OUT):
            self.assertIn(stage_type, DISCORD_POLL_ANSWER_ORDER_MAP)
        self.assertNotIn(StageType.OTHER, DISCORD_POLL_ANSWER_ORDER_MAP)

    def test_answer_ids_are_one_based(self):
        """Discord answer ids start at 1, so slot 0 must stay a placeholder -
        poll_listener indexes straight into this list with payload.answer_id."""
        for stage_type, order in DISCORD_POLL_ANSWER_ORDER_MAP.items():
            self.assertIsNone(order[0], f"{stage_type} must reserve slot 0")
            self.assertTrue(all(outcome is not None for outcome in order[1:]))

    def test_home_is_always_the_first_answer_and_away_the_last(self):
        """poll_creation renders home first and away last; a reordering here
        would silently swap which team a vote counts for."""
        for stage_type, order in DISCORD_POLL_ANSWER_ORDER_MAP.items():
            self.assertEqual(order[1], MatchOutcome.HOME_WIN, f"{stage_type} must lead with the home team")
            self.assertEqual(order[-1], MatchOutcome.AWAY_WIN, f"{stage_type} must end with the away team")


class MatchMessageContainerTests(TestCase):
    """Covers ActiveMatchMessage.container_id.

    Polls used to be posted into a per-batch thread and are now posted straight
    into the pool channel, so both shapes coexist in one table. Everything that
    fetches a poll message routes through container_id; if it stopped falling
    back to the channel, every current poll would become unreachable, and if it
    stopped preferring the thread, every poll from the threaded era would.
    """

    def setUp(self):
        self.competition = Competition.objects.create(name="World Cup")
        self.season = Season.objects.create(name="2026 World Cup", competition=self.competition, year=2026)
        self.stage = Stage.objects.create(season=self.season, name="Group A", stage_type=StageType.GROUP, level=1)
        self.match = Match.objects.create(
            stage=self.stage,
            home_team=Team.objects.create(name="Germany"),
            away_team=Team.objects.create(name="Brazil"),
            kickoff=timezone.now(),
        )
        self.pool = PredictionPool.objects.create(name="Test Pool", season=self.season)
        self.guild = DiscordGuild.objects.create(id=1, name="Test Guild")
        self.channel = DiscordChannel.objects.create(id=10, guild=self.guild, name="general", channel_type="text")

    def test_poll_posted_in_the_channel_resolves_to_the_channel(self):
        match_msg = ActiveMatchMessage.objects.create(
            match=self.match,
            guild=self.guild,
            pool=self.pool,
            channel=self.channel,
            poll_message_id=30,
        )
        self.assertIsNone(match_msg.thread_id)
        self.assertEqual(match_msg.container_id, self.channel.id)

    def test_poll_posted_in_a_legacy_thread_still_resolves_to_that_thread(self):
        match_msg = ActiveMatchMessage.objects.create(
            match=self.match,
            guild=self.guild,
            pool=self.pool,
            channel=self.channel,
            thread_id=20,
            poll_message_id=31,
        )
        self.assertEqual(match_msg.container_id, 20)


class PoolNotificationPreferenceTests(TestCase):
    """Covers the per-pool missing-vote opt-out.

    Reminders default to on, so the absence of a row is consent - only rows
    that were explicitly switched off may mute anyone, and only in the pool
    they were set for.
    """

    def setUp(self):
        self.competition = Competition.objects.create(name="NFL")
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        self.other_season = Season.objects.create(name="NFL 2025", competition=self.competition, year=2025)
        self.pool = PredictionPool.objects.create(name="NFL 2026", season=self.season)
        self.other_pool = PredictionPool.objects.create(name="NFL 2025", season=self.other_season)
        self.alice = User.objects.create_user(username="alice")
        self.bob = User.objects.create_user(username="bob")

    async def test_users_without_a_row_are_not_muted(self):
        muted = await PoolNotificationPreference.aget_muted_user_ids(self.pool.id)
        self.assertEqual(muted, set())

    async def test_only_explicitly_disabled_users_are_muted(self):
        await PoolNotificationPreference.objects.acreate(user=self.alice, pool=self.pool, notify_missing_votes=False)
        await PoolNotificationPreference.objects.acreate(user=self.bob, pool=self.pool, notify_missing_votes=True)

        muted = await PoolNotificationPreference.aget_muted_user_ids(self.pool.id)
        self.assertEqual(muted, {self.alice.id})

    async def test_muting_one_pool_leaves_another_pool_alone(self):
        await PoolNotificationPreference.objects.acreate(user=self.alice, pool=self.pool, notify_missing_votes=False)

        self.assertEqual(await PoolNotificationPreference.aget_muted_user_ids(self.other_pool.id), set())


class MatchTickerStateTests(TestCase):
    """Covers MatchTickerCog._desired_state.

    The single status message is driven entirely by this function, and the
    order of its checks matters: a finished match is also past its kickoff, so
    testing kickoff first would leave every finished match stuck on the live
    score and never post a result.
    """

    def setUp(self):
        self.competition = Competition.objects.create(name="NFL")
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        self.stage = Stage.objects.create(season=self.season, name="Regular Season", stage_type=StageType.LEAGUE)
        self.match = Match.objects.create(
            stage=self.stage,
            home_team=Team.objects.create(name="Chiefs"),
            away_team=Team.objects.create(name="Eagles"),
            kickoff=timezone.now(),
        )

    REMINDER_WINDOW = datetime.timedelta(hours=1)

    def desired_state(self, offset_from_kickoff, window=None):
        return MatchTickerCog._desired_state(
            self.match,
            self.match.kickoff + offset_from_kickoff,
            self.REMINDER_WINDOW if window is None else window,
        )

    def test_far_from_kickoff_posts_nothing(self):
        self.assertEqual(
            self.desired_state(-self.REMINDER_WINDOW - datetime.timedelta(minutes=1)), MatchMessageState.UNKNOWN
        )

    def test_inside_the_window_asks_for_the_reminder(self):
        self.assertEqual(self.desired_state(-datetime.timedelta(minutes=30)), MatchMessageState.STARTING_SOON)

    def test_after_kickoff_switches_to_the_live_score(self):
        self.assertEqual(self.desired_state(datetime.timedelta(minutes=1)), MatchMessageState.IN_PROGRESS)

    def test_a_finished_match_posts_the_result_even_though_kickoff_has_passed(self):
        self.match.status = MatchStatus.FINISHED
        self.assertEqual(self.desired_state(datetime.timedelta(hours=3)), MatchMessageState.RESULT_POSTED)

    def test_a_cancelled_match_is_finalized_before_its_kickoff(self):
        self.match.status = MatchStatus.CANCELLED
        self.assertEqual(self.desired_state(-datetime.timedelta(minutes=30)), MatchMessageState.RESULT_POSTED)


class MissingVoterMentionTests(TestCase):
    """Covers who gets pinged by the pre-kickoff reminder.

    The set is the notification role's members, minus anyone who already has a
    prediction, minus anyone who muted this pool. A role member with no
    DiscordProfile has never voted anywhere and therefore still counts as
    missing - that is the case that makes it wrong to start from the profile
    table instead of from the role.
    """

    def setUp(self):
        self.competition = Competition.objects.create(name="NFL")
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        self.stage = Stage.objects.create(season=self.season, name="Regular Season", stage_type=StageType.LEAGUE)
        self.match = Match.objects.create(
            stage=self.stage,
            home_team=Team.objects.create(name="Chiefs"),
            away_team=Team.objects.create(name="Eagles"),
            kickoff=timezone.now() + datetime.timedelta(minutes=30),
        )
        self.pool = PredictionPool.objects.create(name="NFL 2026", season=self.season)
        self.guild = DiscordGuild.objects.create(id=1, name="Test Guild")
        self.channel = DiscordChannel.objects.create(id=10, guild=self.guild, name="general", channel_type="text")
        self.role = DiscordGuildRole.objects.create(id=77, guild=self.guild, name="Pickers")
        DiscordGuildPool.objects.create(
            guild=self.guild,
            pool=self.pool,
            channel=self.channel,
            notification_role=self.role,
        )
        self.match_msg = ActiveMatchMessage.objects.create(
            match=self.match,
            guild=self.guild,
            pool=self.pool,
            channel=self.channel,
            poll_message_id=30,
        )

        self.voter = User.objects.create_user(username="voter")
        DiscordProfile.objects.create(id=111, user=self.voter, username="voter")
        self.slacker = User.objects.create_user(username="slacker")
        DiscordProfile.objects.create(id=222, user=self.slacker, username="slacker")
        self.muted = User.objects.create_user(username="muted")
        DiscordProfile.objects.create(id=333, user=self.muted, username="muted")

    def make_cog(self, members):
        role = FakeRole(role_id=self.role.id, name="Pickers", members=members)
        guild = FakeGuild(guild_id=self.guild.id, name="Test Guild", roles=[role])
        return MatchTickerCog(bot=FakeBot(guilds=[guild]))

    async def test_only_members_without_a_pick_are_mentioned(self):
        await Prediction.objects.acreate(
            pool=self.pool,
            match=self.match,
            user=self.voter,
            predicted_outcome=MatchOutcome.HOME_WIN,
        )
        cog = self.make_cog([FakeMember(111), FakeMember(222)])

        mentions = await cog._missing_voter_mentions(self.match_msg)

        self.assertEqual(mentions, ["<@222>"])

    async def test_a_member_who_muted_this_pool_is_never_mentioned(self):
        await PoolNotificationPreference.objects.acreate(
            user=self.muted,
            pool=self.pool,
            notify_missing_votes=False,
        )
        cog = self.make_cog([FakeMember(222), FakeMember(333)])

        mentions = await cog._missing_voter_mentions(self.match_msg)

        self.assertEqual(mentions, ["<@222>"])

    async def test_a_member_who_never_played_is_still_missing(self):
        cog = self.make_cog([FakeMember(999)])

        mentions = await cog._missing_voter_mentions(self.match_msg)

        self.assertEqual(mentions, ["<@999>"])

    async def test_bots_are_left_out(self):
        cog = self.make_cog([FakeMember(222), FakeMember(444, name="otterball", is_bot=True)])

        mentions = await cog._missing_voter_mentions(self.match_msg)

        self.assertEqual(mentions, ["<@222>"])

    async def test_no_notification_role_means_no_pings(self):
        await DiscordGuildPool.objects.filter(guild_id=self.guild.id, pool_id=self.pool.id).aupdate(
            notification_role=None
        )
        cog = self.make_cog([FakeMember(222)])

        self.assertEqual(await cog._missing_voter_mentions(self.match_msg), [])


def walk_components(view):
    """Every component in a LayoutView's payload, accessories and children included."""

    def _walk(components):
        for component in components:
            yield component
            yield from _walk(component.get("components", []))
            accessory = component.get("accessory")
            if accessory:
                yield accessory

    return list(_walk(view.to_components()))


def text_of(view):
    return [c["content"] for c in walk_components(view) if c["type"] == COMPONENT_TEXT_DISPLAY]


class FakeSentMessage:
    def __init__(self, message_id, **kwargs):
        self.id = message_id
        self.kwargs = kwargs
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeClosablePoll(FakePoll):
    def __init__(self, answers):
        super().__init__(answers=answers)
        self.ended = False

    def is_finalised(self):
        return self.ended

    async def end_poll(self):
        self.ended = True


class FakePollMessage:
    def __init__(self, message_id, poll):
        self.id = message_id
        self.poll = poll
        self.pinned = True
        self.unpinned = False

    async def end_poll(self):
        await self.poll.end_poll()

    async def unpin(self, reason=None):
        self.unpinned = True
        self.pinned = False


class FakePartialMessage:
    """Mimics discord.PartialMessage: edits without a prior fetch, 404s if gone."""

    def __init__(self, channel, message_id):
        self.channel = channel
        self.id = message_id

    async def edit(self, **kwargs):
        target = self.channel.messages.get(self.id)
        if target is None:
            raise discord.NotFound(_FakeResponse(), "unknown message")
        target.edits.append(kwargs)
        return target


class RecordingChannel(discord.abc.Messageable):
    """A channel that remembers what was sent to it and can hand it back."""

    def __init__(self, channel_id, messages=None):
        self.id = channel_id
        self.messages = messages or {}
        self.sent = []
        self._next_id = 900

    async def _get_channel(self):
        return self

    def get_partial_message(self, message_id):
        return FakePartialMessage(self, message_id)

    async def fetch_message(self, message_id):
        if message_id not in self.messages:
            raise discord.NotFound(_FakeResponse(), "unknown message")
        return self.messages[message_id]

    async def send(self, content=None, view=None, allowed_mentions=None, reference=None, **kwargs):
        self._next_id += 1
        message = FakeSentMessage(
            self._next_id,
            content=content,
            view=view,
            allowed_mentions=allowed_mentions,
            reference=reference,
        )
        self.messages[message.id] = message
        self.sent.append(message)
        return message


class _FakeResponse:
    status = 404
    reason = "Not Found"


class StateMessageLifecycleTests(TestCase):
    """Covers MatchTickerCog.sync_state_message for a finished match.

    This is the path that replaces the thread's implicit lifecycle: at kickoff
    the poll is ended, unpinned and re-derived one last time, and the status
    message ends up as the final score with the winners named. Ending the poll
    is what makes the last vote sync trustworthy, and `is_poll_finalized` is
    what stops the startup reconciliation from re-reading a closed poll forever.
    """

    def setUp(self):
        self.competition = Competition.objects.create(name="NFL")
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        self.stage = Stage.objects.create(season=self.season, name="Regular Season", stage_type=StageType.LEAGUE)
        self.match = Match.objects.create(
            stage=self.stage,
            home_team=Team.objects.create(name="Chiefs"),
            away_team=Team.objects.create(name="Eagles"),
            kickoff=timezone.now() - datetime.timedelta(hours=3),
            status=MatchStatus.FINISHED,
            home_score=24,
            away_score=10,
        )
        self.pool = PredictionPool.objects.create(name="NFL 2026", season=self.season)
        self.guild = DiscordGuild.objects.create(id=1, name="Test Guild")
        self.channel = DiscordChannel.objects.create(id=10, guild=self.guild, name="general", channel_type="text")
        ActiveMatchMessage.objects.create(
            match=self.match,
            guild=self.guild,
            pool=self.pool,
            channel=self.channel,
            poll_message_id=30,
        )
        self.winner = User.objects.create_user(username="winner")
        DiscordProfile.objects.create(id=111, user=self.winner, username="winner")

    async def test_kickoff_closes_the_poll_and_posts_the_result(self):
        poll = FakeClosablePoll(answers=[FakePollAnswer(answer_id=1, voter_ids=[111])])
        poll_message = FakePollMessage(message_id=30, poll=poll)
        channel = RecordingChannel(channel_id=self.channel.id, messages={30: poll_message})
        cog = MatchTickerCog(bot=FakeBot(channel=channel))

        active_msg = await ActiveMatchMessage.objects.select_related(
            "match", "match__stage", "match__home_team", "match__away_team"
        ).aget(poll_message_id=30)

        await cog.sync_state_message(active_msg)

        self.assertTrue(poll.ended)
        self.assertTrue(poll_message.unpinned)

        stored = await ActiveMatchMessage.objects.aget(poll_message_id=30)
        self.assertTrue(stored.is_poll_finalized)
        self.assertTrue(stored.is_ticker_finalized)
        self.assertEqual(stored.ticker_state, MatchMessageState.RESULT_POSTED)
        self.assertIsNotNone(stored.ticker_message_id)

        # The last vote sync ran off the ended poll, so the vote counts.
        prediction = await Prediction.objects.aget(pool=self.pool, match=self.match)
        self.assertEqual(prediction.user_id, self.winner.id)
        self.assertEqual(prediction.predicted_outcome, MatchOutcome.HOME_WIN)

        self.assertEqual(len(channel.sent), 1)
        texts = " ".join(text_of(channel.sent[0].kwargs["view"]))
        self.assertIn("24", texts)
        self.assertIn("<@111>", texts)

    async def test_a_second_pass_does_not_repost_the_result(self):
        poll = FakeClosablePoll(answers=[FakePollAnswer(answer_id=1, voter_ids=[111])])
        channel = RecordingChannel(channel_id=self.channel.id, messages={30: FakePollMessage(30, poll)})
        cog = MatchTickerCog(bot=FakeBot(channel=channel))

        for _ in range(2):
            active_msg = await ActiveMatchMessage.objects.select_related(
                "match", "match__stage", "match__home_team", "match__away_team"
            ).aget(poll_message_id=30)
            await cog.sync_state_message(active_msg)

        self.assertEqual(len(channel.sent), 1)

    async def test_a_match_cancelled_before_kickoff_still_gets_its_poll_closed(self):
        # A called-off match never reaches its kickoff, so nothing else would
        # ever end or unpin its poll.
        await Match.objects.filter(id=self.match.id).aupdate(
            status=MatchStatus.CANCELLED,
            kickoff=timezone.now() + datetime.timedelta(days=2),
            home_score=None,
            away_score=None,
        )
        poll = FakeClosablePoll(answers=[FakePollAnswer(answer_id=1, voter_ids=[111])])
        poll_message = FakePollMessage(message_id=30, poll=poll)
        channel = RecordingChannel(channel_id=self.channel.id, messages={30: poll_message})
        cog = MatchTickerCog(bot=FakeBot(channel=channel))

        active_msg = await ActiveMatchMessage.objects.select_related(
            "match", "match__stage", "match__home_team", "match__away_team"
        ).aget(poll_message_id=30)

        await cog.sync_state_message(active_msg)

        self.assertTrue(poll.ended)
        self.assertTrue(poll_message.unpinned)

        stored = await ActiveMatchMessage.objects.aget(poll_message_id=30)
        self.assertTrue(stored.is_poll_finalized)
        self.assertEqual(stored.ticker_state, MatchMessageState.RESULT_POSTED)

        self.assertIn("Cancelled", " ".join(text_of(channel.sent[0].kwargs["view"])))

    async def test_a_score_change_edits_the_status_message_instead_of_reposting(self):
        await Match.objects.filter(id=self.match.id).aupdate(
            status=MatchStatus.LIVE,
            kickoff=timezone.now() - datetime.timedelta(minutes=20),
            home_score=7,
            away_score=0,
        )
        poll = FakeClosablePoll(answers=[FakePollAnswer(answer_id=1, voter_ids=[111])])
        channel = RecordingChannel(channel_id=self.channel.id, messages={30: FakePollMessage(30, poll)})
        cog = MatchTickerCog(bot=FakeBot(channel=channel))

        async def sync():
            active_msg = await ActiveMatchMessage.objects.select_related(
                "match", "match__stage", "match__home_team", "match__away_team"
            ).aget(poll_message_id=30)
            await cog.sync_state_message(active_msg)

        await sync()
        self.assertEqual(len(channel.sent), 1)
        status_message = channel.sent[0]

        # An unchanged match must not cost a single API call.
        await sync()
        self.assertEqual(status_message.edits, [])

        await Match.objects.filter(id=self.match.id).aupdate(home_score=14)
        await sync()

        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(len(status_message.edits), 1)
        self.assertIn("14", " ".join(text_of(status_message.edits[0]["view"])))


class FakeSystemMessage:
    def __init__(self, message_type, author_id):
        self.type = message_type
        self.author = FakeVoter(author_id, f"user{author_id}")


class GarbageFilterTests(TestCase):
    """Covers RemoveGarbageCog._is_garbage.

    Discord authors a pin notice as whoever pinned the message, so this is what
    keeps the cleanup to the bot's own pins - it pins every poll at creation and
    unpins it at kickoff, while a moderator pinning something in the same
    channel keeps their notice.
    """

    def setUp(self):
        bot = FakeBot()
        bot.user = FakeVoter(42, "otterball")
        self.cog = RemoveGarbageCog(bot=bot)

    def test_the_bots_own_pin_notice_is_garbage(self):
        message = FakeSystemMessage(discord.MessageType.pins_add, author_id=42)
        self.assertTrue(self.cog._is_garbage(message))

    def test_someone_elses_pin_notice_is_left_alone(self):
        message = FakeSystemMessage(discord.MessageType.pins_add, author_id=777)
        self.assertFalse(self.cog._is_garbage(message))

    def test_poll_results_are_still_garbage(self):
        message = FakeSystemMessage(discord.MessageType.poll_result, author_id=777)
        self.assertTrue(self.cog._is_garbage(message))

    def test_ordinary_messages_are_never_touched(self):
        message = FakeSystemMessage(discord.MessageType.default, author_id=42)
        self.assertFalse(self.cog._is_garbage(message))

    def test_nothing_is_deleted_before_the_bot_knows_who_it_is(self):
        bot = FakeBot()
        bot.user = None
        cog = RemoveGarbageCog(bot=bot)

        message = FakeSystemMessage(discord.MessageType.pins_add, author_id=42)
        self.assertFalse(cog._is_garbage(message))


class ReminderWindowTests(TestCase):
    """Covers the per-pool reminder lead time.

    The window used to be a hardcoded hour in the cog. It now comes from
    PoolConfiguration.reminder_lead_minutes, with 0 meaning "never remind" and
    a missing configuration row falling back to the field's default rather than
    crashing the loop.
    """

    def setUp(self):
        self.competition = Competition.objects.create(name="NFL")
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        self.stage = Stage.objects.create(season=self.season, name="Regular Season", stage_type=StageType.LEAGUE)
        self.match = Match.objects.create(
            stage=self.stage,
            home_team=Team.objects.create(name="Chiefs"),
            away_team=Team.objects.create(name="Eagles"),
            kickoff=timezone.now() + datetime.timedelta(minutes=90),
        )
        self.pool = PredictionPool.objects.create(name="NFL 2026", season=self.season)
        self.guild = DiscordGuild.objects.create(id=1, name="Test Guild")
        self.channel = DiscordChannel.objects.create(id=10, guild=self.guild, name="general", channel_type="text")
        self.match_msg = ActiveMatchMessage.objects.create(
            match=self.match,
            guild=self.guild,
            pool=self.pool,
            channel=self.channel,
            poll_message_id=30,
        )

    async def test_the_configured_lead_time_is_used(self):
        await PoolConfiguration.objects.filter(pool_id=self.pool.id).aupdate(reminder_lead_minutes=120)

        window = await MatchTickerCog.areminder_window(self.match_msg)

        self.assertEqual(window, datetime.timedelta(minutes=120))
        self.assertEqual(
            MatchTickerCog._desired_state(self.match, timezone.now(), window),
            MatchMessageState.STARTING_SOON,
        )

    async def test_a_shorter_lead_time_holds_the_reminder_back(self):
        await PoolConfiguration.objects.filter(pool_id=self.pool.id).aupdate(reminder_lead_minutes=30)

        window = await MatchTickerCog.areminder_window(self.match_msg)

        self.assertEqual(
            MatchTickerCog._desired_state(self.match, timezone.now(), window),
            MatchMessageState.UNKNOWN,
        )

    async def test_zero_disables_the_reminder_entirely(self):
        await PoolConfiguration.objects.filter(pool_id=self.pool.id).aupdate(reminder_lead_minutes=0)

        window = await MatchTickerCog.areminder_window(self.match_msg)

        self.assertEqual(window, datetime.timedelta(0))
        # Right up to kickoff there is still nothing to post.
        self.assertEqual(
            MatchTickerCog._desired_state(self.match, self.match.kickoff - datetime.timedelta(seconds=1), window),
            MatchMessageState.UNKNOWN,
        )
        # ...but the match itself is still tracked once it starts.
        self.assertEqual(
            MatchTickerCog._desired_state(self.match, self.match.kickoff, window),
            MatchMessageState.IN_PROGRESS,
        )

    async def test_a_pool_without_a_configuration_falls_back_to_the_default(self):
        await PoolConfiguration.objects.filter(pool_id=self.pool.id).adelete()

        window = await MatchTickerCog.areminder_window(self.match_msg)

        self.assertEqual(window, datetime.timedelta(minutes=DEFAULT_REMINDER_LEAD_MINUTES))

    async def test_the_loop_horizon_covers_the_most_generous_pool(self):
        await PoolConfiguration.objects.filter(pool_id=self.pool.id).aupdate(reminder_lead_minutes=240)
        other_season = await Season.objects.acreate(name="NFL 2025", competition=self.competition, year=2025)
        other_pool = await PredictionPool.objects.acreate(name="NFL 2025", season=other_season)
        await PoolConfiguration.objects.filter(pool_id=other_pool.id).aupdate(reminder_lead_minutes=45)

        self.assertEqual(await MatchTickerCog._widest_reminder_window(), datetime.timedelta(minutes=240))

    async def test_an_inactive_pool_does_not_widen_the_horizon(self):
        await PoolConfiguration.objects.filter(pool_id=self.pool.id).aupdate(reminder_lead_minutes=30)
        other_season = await Season.objects.acreate(name="NFL 2025", competition=self.competition, year=2025)
        other_pool = await PredictionPool.objects.acreate(name="NFL 2025", season=other_season, is_active=False)
        await PoolConfiguration.objects.filter(pool_id=other_pool.id).aupdate(reminder_lead_minutes=600)

        self.assertEqual(await MatchTickerCog._widest_reminder_window(), datetime.timedelta(minutes=30))


class MatchStatusViewTests(TestCase):
    """Covers the Components V2 scoreboard on the status message.

    Assertions run against `view.to_components()` - the payload Discord
    actually receives - rather than against the Python objects, so a change in
    how discord.py builds the tree cannot quietly pass.

    Both badges show, one per Section, because a Section carries exactly one
    accessory. Discord offers no way to tint or dim an image, so which side is
    ahead has to be carried by the text beside the badge.
    """

    def setUp(self):
        self.competition = Competition.objects.create(name="NFL")
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        self.stage = Stage.objects.create(season=self.season, name="Regular Season", stage_type=StageType.LEAGUE)
        self.home = Team.objects.create(
            name="Chiefs", color="#e31837", logo_url="https://a.espncdn.com/i/teamlogos/nfl/500/kc.png"
        )
        self.away = Team.objects.create(
            name="Eagles", color="#004c54", logo_url="https://a.espncdn.com/i/teamlogos/nfl/500/phi.png"
        )
        self.match = Match.objects.create(
            stage=self.stage,
            home_team=self.home,
            away_team=self.away,
            kickoff=timezone.now(),
            status=MatchStatus.LIVE,
        )
        self.cog = MatchTickerCog(bot=FakeBot())

    def live_view(self, home_score, away_score, status=MatchStatus.LIVE):
        self.match.home_score = home_score
        self.match.away_score = away_score
        self.match.status = status
        if status == MatchStatus.FINISHED:
            return self.cog._scoreline_view(self.match, heading="### 🏁 Full time", footer="ft")
        return self.cog._render_live(self.match)

    def thumbnails_of(self, view):
        return [c["media"]["url"] for c in walk_components(view) if c["type"] == COMPONENT_THUMBNAIL]

    def accent_of(self, view):
        container = next(c for c in walk_components(view) if c["type"] == COMPONENT_CONTAINER)
        return container["accent_color"]

    def test_both_badges_are_shown_home_first(self):
        view = self.live_view(21, 7)

        self.assertEqual(self.thumbnails_of(view), [self.home.logo_url, self.away.logo_url])

    def test_the_leader_is_bold_and_the_trailer_is_not(self):
        texts = text_of(self.live_view(21, 7))

        self.assertIn("## **Chiefs**", texts)
        self.assertIn("## Eagles", texts)
        self.assertEqual(self.accent_of(self.live_view(21, 7)), discord.Color.from_str(self.home.color).value)

    def test_the_emphasis_and_the_colour_swing_together_with_the_lead(self):
        view = self.live_view(7, 21)
        texts = text_of(view)

        self.assertIn("## **Eagles**", texts)
        self.assertIn("## Chiefs", texts)
        self.assertEqual(self.accent_of(view), discord.Color.from_str(self.away.color).value)
        # Order never changes, so the scoreboard does not shift under the reader.
        self.assertEqual(self.thumbnails_of(view), [self.home.logo_url, self.away.logo_url])

    def test_a_tie_marks_nobody(self):
        """NFL regular season games really can tie, and 0-0 is every game's first minute."""
        for home_score, away_score in ((14, 14), (0, 0)):
            texts = text_of(self.live_view(home_score, away_score))
            # Neither side is emphasised, because neither is ahead.
            self.assertNotIn("**", " ".join(texts), f"{home_score}-{away_score} has no leader")
            self.assertIn("## Chiefs", texts)
            self.assertIn("## Eagles", texts)

    def test_only_full_time_carries_a_marker(self):
        """A leader mid-match is bold; a winner gets the trophy as well."""
        final = text_of(self.live_view(17, 24, status=MatchStatus.FINISHED))
        self.assertIn("## **Eagles** 🏆", final)

        live = text_of(self.live_view(17, 24))
        self.assertIn("## **Eagles**", live)
        self.assertNotIn("🏆", " ".join(live))

    def test_the_name_heads_the_row_and_the_score_sits_under_it(self):
        """`#` is the largest text Discord renders, so the score takes it."""
        texts = text_of(self.live_view(17, 10))

        self.assertIn("## **Chiefs**", texts)
        self.assertIn(f"# {FIGURE_SPACE * 2}17", texts)
        self.assertIn(f"# {FIGURE_SPACE * 2}10", texts)

    def test_an_unplayed_match_shows_a_dash_where_the_score_goes(self):
        """Same two-line shape before kickoff as during the match.

        Never a zero: an unplayed game is not 0-0, and rendering it as one
        would read as a result - the same reason ingestion keeps an empty
        score as None rather than coercing it.
        """
        texts = text_of(self.live_view(None, None))

        self.assertIn("## Chiefs", texts)
        self.assertIn(f"# {FIGURE_SPACE * 2}–", texts)
        self.assertEqual(len([t for t in texts if t.startswith("# ")]), 2)
        self.assertNotIn(f"# {FIGURE_SPACE * 2}0", texts)

    def test_a_missing_score_beside_a_real_one_is_still_lined_up(self):
        texts = text_of(self.live_view(24, None))

        self.assertIn(f"# {FIGURE_SPACE * 2}24", texts)
        self.assertIn(f"# {FIGURE_SPACE * 2}{HALF_DIGIT}–", texts)

    def test_a_team_without_a_logo_drops_to_plain_text_without_losing_the_other(self):
        self.home.logo_url = None

        view = self.live_view(21, 7)

        # A Section requires an accessory, so the badge-less side cannot be one -
        # but the other side keeps its Section and its badge.
        self.assertEqual(self.thumbnails_of(view), [self.away.logo_url])
        self.assertIn("## **Chiefs**", text_of(view))

    def test_an_unparseable_colour_falls_back_without_losing_the_badges(self):
        self.home.color = "not a colour"

        view = self.live_view(21, 7)

        self.assertEqual(self.accent_of(view), discord.Color.blurple().value)
        self.assertEqual(len(self.thumbnails_of(view)), 2)

    def test_a_divider_sits_under_the_status_line(self):
        """Every state gets it, so the heading reads as a header not a first line."""
        for view in (self.live_view(17, 10), self.live_view(None, None)):
            types = [c["type"] for c in walk_components(view)]
            self.assertIn(COMPONENT_SEPARATOR, types)
            # The first thing after the container's heading, not just somewhere.
            container = next(c for c in walk_components(view) if c["type"] == COMPONENT_CONTAINER)
            kinds = [child["type"] for child in container["components"]]
            self.assertEqual(kinds[:2], [COMPONENT_TEXT_DISPLAY, COMPONENT_SEPARATOR])

    def test_a_single_digit_score_is_nudged_onto_the_same_axis(self):
        """7 next to 24 would otherwise sit half a digit to the left.

        There is no half-figure-space character, so a quarter em stands in as
        the closest standard width to half a digit.
        """
        texts = text_of(self.live_view(24, 7))

        self.assertIn(f"# {FIGURE_SPACE * 2}24", texts)
        self.assertIn(f"# {FIGURE_SPACE * 2}{HALF_DIGIT}7", texts)

    def test_two_scores_of_equal_width_get_no_nudge(self):
        for home_score, away_score in ((17, 24), (7, 3)):
            texts = text_of(self.live_view(home_score, away_score))
            self.assertNotIn(HALF_DIGIT, " ".join(texts), f"{home_score}-{away_score} needs no nudge")

    def test_it_stays_inside_discords_component_and_character_budget(self):
        """40 components and 4000 characters of text per message, per the API docs.

        Checked on the widest layout - full time, with two team sections and a
        capped winner list.
        """
        self.match.home_score, self.match.away_score = 17, 24
        self.match.status = MatchStatus.FINISHED
        view = self.cog._scoreline_view(
            self.match,
            heading="### 🏁 Full time",
            footer="Leaderboard updates within a minute",
            detail="**🎯 Called it (40)**\n" + " ".join(f"<@{n}>" for n in range(40)),
        )

        self.assertLess(len(walk_components(view)), 40)
        self.assertLess(sum(len(t) for t in text_of(view)), 4000)


class MuteButtonTests(TestCase):
    """Covers the Mute button on the pre-kickoff reminder.

    It is a DynamicItem: the pool id travels in the custom_id and is parsed
    back out on click, which is what lets a button posted before the last
    restart still work. If the template and the emitted custom_id ever stop
    agreeing, every button silently becomes inert, so both directions are
    asserted here together.
    """

    def setUp(self):
        self.competition = Competition.objects.create(name="NFL")
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        self.stage = Stage.objects.create(season=self.season, name="Regular Season", stage_type=StageType.LEAGUE)
        self.match = Match.objects.create(
            stage=self.stage,
            home_team=Team.objects.create(name="Chiefs", logo_url="https://example.invalid/kc.png"),
            away_team=Team.objects.create(name="Eagles"),
            kickoff=timezone.now() + datetime.timedelta(minutes=30),
        )
        self.pool = PredictionPool.objects.create(name="NFL 2026", season=self.season)
        self.guild = DiscordGuild.objects.create(id=1, name="Test Guild")
        self.channel = DiscordChannel.objects.create(id=10, guild=self.guild, name="general", channel_type="text")
        self.role = DiscordGuildRole.objects.create(id=77, guild=self.guild, name="Pickers")
        DiscordGuildPool.objects.create(
            guild=self.guild, pool=self.pool, channel=self.channel, notification_role=self.role
        )
        self.match_msg = ActiveMatchMessage.objects.create(
            match=self.match, guild=self.guild, pool=self.pool, channel=self.channel, poll_message_id=30
        )

    def make_cog(self, members):
        role = FakeRole(role_id=self.role.id, name="Pickers", members=members)
        guild = FakeGuild(guild_id=self.guild.id, name="Test Guild", roles=[role])
        return MatchTickerCog(bot=FakeBot(guilds=[guild]))

    def buttons_in(self, view):
        return [c for c in walk_components(view) if c["type"] == COMPONENT_BUTTON]

    async def test_the_reminder_carries_a_mute_button_for_its_own_pool(self):
        cog = self.make_cog([FakeMember(222)])
        active_msg = await ActiveMatchMessage.objects.select_related(
            "match", "match__stage", "match__home_team", "match__away_team"
        ).aget(poll_message_id=30)

        mentions = await cog._missing_voter_mentions(active_msg)
        view, allowed_mentions = cog._render_starting_soon(active_msg, mentions)

        buttons = self.buttons_in(view)
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0]["custom_id"], f"otterball:mute:{self.pool.id}")
        self.assertIn("<@222>", " ".join(text_of(view)))
        # Mentions only notify when the allowed-mentions object permits it.
        self.assertTrue(allowed_mentions.users)

    async def test_no_button_when_nobody_is_being_pinged(self):
        """With no one to ping there is no ping to opt out of."""
        cog = self.make_cog([])
        active_msg = await ActiveMatchMessage.objects.select_related(
            "match", "match__stage", "match__home_team", "match__away_team"
        ).aget(poll_message_id=30)

        mentions = await cog._missing_voter_mentions(active_msg)
        view, allowed_mentions = cog._render_starting_soon(active_msg, mentions)

        self.assertEqual(self.buttons_in(view), [])
        self.assertIsNone(allowed_mentions)

    def test_the_custom_id_round_trips_through_the_dynamic_template(self):
        button = MuteRemindersButton(self.pool.id)
        custom_id = button.item.custom_id

        parsed = MuteRemindersButton.__discord_ui_compiled_template__.fullmatch(custom_id)

        self.assertIsNotNone(parsed, "the emitted custom_id must match the template that dispatches it")
        self.assertEqual(int(parsed["pool_id"]), self.pool.id)

    async def test_muting_writes_the_preference_for_that_pool_only(self):
        other_season = await Season.objects.acreate(name="NFL 2025", competition=self.competition, year=2025)
        other_pool = await PredictionPool.objects.acreate(name="NFL 2025", season=other_season)
        user = await User.objects.acreate_user(username="clicker")
        await DiscordProfile.objects.acreate(id=555, user=user, username="clicker")

        await aset_missing_vote_reminders(FakeMember(555, name="clicker"), self.pool.id, enabled=False)

        self.assertEqual(await PoolNotificationPreference.aget_muted_user_ids(self.pool.id), {user.id})
        self.assertEqual(await PoolNotificationPreference.aget_muted_user_ids(other_pool.id), set())

    async def test_clicking_mute_creates_an_account_for_a_user_who_never_voted(self):
        await aset_missing_vote_reminders(FakeMember(999, name="newcomer"), self.pool.id, enabled=False)

        profile = await DiscordProfile.objects.aget(id=999)
        self.assertEqual(await PoolNotificationPreference.aget_muted_user_ids(self.pool.id), {profile.user_id})


class ReminderStaysInSyncTests(TestCase):
    """Covers the reminder tracking votes as they land.

    The score does not move while the reminder is up, so unless the missing
    voters are part of the render fingerprint the message keeps naming people
    who have since voted. Nobody added back is notified - the message is
    edited, and an edit never pings.
    """

    def setUp(self):
        self.competition = Competition.objects.create(name="NFL")
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        self.stage = Stage.objects.create(season=self.season, name="Regular Season", stage_type=StageType.LEAGUE)
        self.match = Match.objects.create(
            stage=self.stage,
            home_team=Team.objects.create(name="Chiefs", logo_url="https://example.invalid/kc.png"),
            away_team=Team.objects.create(name="Eagles", logo_url="https://example.invalid/phi.png"),
            kickoff=timezone.now() + datetime.timedelta(minutes=30),
        )
        self.pool = PredictionPool.objects.create(name="NFL 2026", season=self.season)
        self.guild = DiscordGuild.objects.create(id=1, name="Test Guild")
        self.channel = DiscordChannel.objects.create(id=10, guild=self.guild, name="general", channel_type="text")
        self.role = DiscordGuildRole.objects.create(id=77, guild=self.guild, name="Pickers")
        DiscordGuildPool.objects.create(
            guild=self.guild, pool=self.pool, channel=self.channel, notification_role=self.role
        )
        ActiveMatchMessage.objects.create(
            match=self.match, guild=self.guild, pool=self.pool, channel=self.channel, poll_message_id=30
        )
        self.voter = User.objects.create_user(username="voter")
        DiscordProfile.objects.create(id=111, user=self.voter, username="voter")

    async def fresh(self):
        return await ActiveMatchMessage.objects.select_related(
            "match", "match__stage", "match__home_team", "match__away_team"
        ).aget(poll_message_id=30)

    def make_cog(self, channel):
        role = FakeRole(role_id=self.role.id, name="Pickers", members=[FakeMember(111), FakeMember(222)])
        guild = FakeGuild(guild_id=self.guild.id, name="Test Guild", roles=[role])
        bot = FakeBot(channel=channel, guilds=[guild])
        return MatchTickerCog(bot=bot)

    async def test_a_vote_removes_that_name_by_editing_the_message(self):
        channel = RecordingChannel(
            channel_id=self.channel.id, messages={30: FakePollMessage(30, FakeClosablePoll([]))}
        )
        cog = self.make_cog(channel)

        await cog.sync_state_message(await self.fresh())
        posted = channel.sent[0]
        self.assertIn("<@111>", " ".join(text_of(posted.kwargs["view"])))
        self.assertIn("<@222>", " ".join(text_of(posted.kwargs["view"])))

        await Prediction.objects.acreate(
            pool=self.pool, match=self.match, user=self.voter, predicted_outcome=MatchOutcome.HOME_WIN
        )
        await cog.sync_state_message(await self.fresh())

        # Edited, not reposted.
        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(len(posted.edits), 1)
        edited = " ".join(text_of(posted.edits[0]["view"]))
        self.assertNotIn("<@111>", edited)
        self.assertIn("<@222>", edited)

    async def test_a_retracted_vote_puts_the_name_back(self):
        await Prediction.objects.acreate(
            pool=self.pool, match=self.match, user=self.voter, predicted_outcome=MatchOutcome.HOME_WIN
        )
        channel = RecordingChannel(
            channel_id=self.channel.id, messages={30: FakePollMessage(30, FakeClosablePoll([]))}
        )
        cog = self.make_cog(channel)

        await cog.sync_state_message(await self.fresh())
        posted = channel.sent[0]
        self.assertNotIn("<@111>", " ".join(text_of(posted.kwargs["view"])))

        await Prediction.objects.filter(pool=self.pool, match=self.match, user=self.voter).adelete()
        await cog.sync_state_message(await self.fresh())

        self.assertIn("<@111>", " ".join(text_of(posted.edits[0]["view"])))

    async def test_an_unchanged_voter_list_costs_no_edit(self):
        channel = RecordingChannel(
            channel_id=self.channel.id, messages={30: FakePollMessage(30, FakeClosablePoll([]))}
        )
        cog = self.make_cog(channel)

        await cog.sync_state_message(await self.fresh())
        await cog.sync_state_message(await self.fresh())

        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(channel.sent[0].edits, [])

    async def test_the_first_post_replies_to_the_poll(self):
        channel = RecordingChannel(
            channel_id=self.channel.id, messages={30: FakePollMessage(30, FakeClosablePoll([]))}
        )
        cog = self.make_cog(channel)

        await cog.sync_state_message(await self.fresh())

        reference = channel.sent[0].kwargs["reference"]
        self.assertEqual(reference.message_id, 30)
        # A deleted poll must not stop the status message going out.
        self.assertFalse(reference.fail_if_not_exists)


class LeaderboardMessageTests(TestCase):
    """Covers the pinned leaderboard embed: it carries each player's hit rate
    next to their points, and reads both off the same generator the website
    does so the two cannot disagree."""

    def setUp(self):
        self.competition = Competition.objects.create(name="World Cup")
        self.season = Season.objects.create(name="2026 World Cup", competition=self.competition, year=2026)
        self.stage = Stage.objects.create(season=self.season, name="Group A", stage_type=StageType.GROUP, level=1)
        self.home_team = Team.objects.create(name="Germany")
        self.away_team = Team.objects.create(name="Brazil")
        self.pool = PredictionPool.objects.create(name="Test Pool", season=self.season)

        self.guild = DiscordGuild.objects.create(id=1, name="Test Guild")
        self.channel = DiscordChannel.objects.create(id=10, guild=self.guild, name="general", channel_type="text")
        self.guild_pool = DiscordGuildPool.objects.create(
            guild=self.guild,
            channel=self.channel,
            pool=self.pool,
            is_active=True,
            leaderboard_msg=900,
        )
        self.message = FakeLeaderboardMessage()

    def player(self, username, global_name, awards):
        """One prediction per award, so a 0 is a pick that was made and missed."""
        user = User.objects.create_user(username=username)
        DiscordProfile.objects.create(
            id=abs(hash(username)) % 10**17, user=user, username=username, global_name=global_name
        )
        for points in awards:
            match = Match.objects.create(
                stage=self.stage,
                home_team=self.home_team,
                away_team=self.away_team,
                kickoff=timezone.now(),
                status=MatchStatus.FINISHED,
                home_score=1,
                away_score=0,
            )
            Prediction.objects.create(
                pool=self.pool,
                match=match,
                user=user,
                predicted_outcome=MatchOutcome.HOME_WIN,
                points_awarded=points,
                is_processed=True,
            )
        return user

    async def render(self):
        """Run the cog against the fakes and hand back the leaderboard embed."""
        bot = FakeBot(channel=FakeChannel(message=self.message), guilds=[FakeGuild(self.guild.id, "Test Guild")])
        cog = LeaderboardSyncCog(bot=bot)
        cog.cog_unload()  # the 30s loop has nothing to do with rendering

        # select_related, as the real loop does: touching guild_pool.pool
        # lazily inside the coroutine would be a sync ORM call on the loop.
        guild_pool = await DiscordGuildPool.objects.select_related("pool").aget(pk=self.guild_pool.pk)
        await cog.update_leaderboard_msg(guild_pool)

        self.assertEqual(len(self.message.edits), 1)
        return self.message.edits[0]["embeds"][0]

    async def test_each_line_carries_points_and_hit_rate(self):
        await sync_to_async(self.player)("alice", "Alice", [4, 4])
        await sync_to_async(self.player)("bob", "Bob", [8, 0, 0])

        embed = await self.render()
        lines = [line for field in embed.fields for line in field.value.splitlines() if line.strip()]

        self.assertIn("**Alice** (8 · 100%)", lines)
        self.assertIn("**Bob** (8 · 33%)", lines)

    async def test_the_footer_says_what_the_two_numbers_are(self):
        await sync_to_async(self.player)("alice", "Alice", [4])

        embed = await self.render()

        self.assertEqual(embed.footer.text, "points · hit rate")

    async def test_accuracy_orders_players_level_on_points(self):
        """Same 8 points, and the embed must put the sharper player first."""
        await sync_to_async(self.player)("bob", "Bob", [8, 0, 0])
        await sync_to_async(self.player)("alice", "Alice", [4, 4])

        embed = await self.render()
        names = [field.value for field in embed.fields]

        self.assertEqual(embed.fields[0].name, "———`1`———")
        self.assertIn("Alice", names[0])
        self.assertIn("Bob", names[1])

    async def test_a_changed_hit_rate_alone_redraws_the_message(self):
        """Accuracy is in the fingerprint, so a pick that moves it but not the
        points total must not be skipped as 'unchanged'."""
        alice = await sync_to_async(self.player)("alice", "Alice", [4, 4])
        await self.render()

        # A missed pick: same points, worse accuracy.
        match = await Match.objects.acreate(
            stage=self.stage,
            home_team=self.home_team,
            away_team=self.away_team,
            kickoff=timezone.now(),
            status=MatchStatus.FINISHED,
            home_score=1,
            away_score=0,
        )
        await Prediction.objects.acreate(
            pool=self.pool,
            match=match,
            user=alice,
            predicted_outcome=MatchOutcome.AWAY_WIN,
            points_awarded=0,
            is_processed=True,
        )

        bot = FakeBot(channel=FakeChannel(message=self.message), guilds=[FakeGuild(self.guild.id, "Test Guild")])
        cog = LeaderboardSyncCog(bot=bot)
        cog.cog_unload()
        guild_pool = await DiscordGuildPool.objects.select_related("pool").aget(pk=self.guild_pool.pk)
        await cog.update_leaderboard_msg(guild_pool)

        self.assertEqual(len(self.message.edits), 2)
        embed = self.message.edits[1]["embeds"][0]
        self.assertIn("**Alice** (8 · 67%)", embed.fields[0].value)


class FakeDiscordGuild:
    """A discord.Guild as far as the sync cogs are concerned."""

    def __init__(self, guild_id, name="Test Guild", channels=(), roles=()):
        self.id = guild_id
        self.name = name
        self.channels = list(channels)
        self.roles = list(roles)


class FakeGuildChannel:
    def __init__(self, channel_id, guild, name="general", position=0):
        self.id = channel_id
        self.guild = guild
        self.name = name
        self.position = position
        self.type = "text"


class FakeGuildRole:
    def __init__(self, role_id, guild, name="Otters", position=0):
        self.id = role_id
        self.guild = guild
        self.name = name
        self.position = position


class GuildSyncCogTests(TestCase):
    """These handlers used `guild_id=` on a model whose primary key is `id`,
    so both raised FieldError - and the cog was never registered, which is why
    nobody noticed."""

    def setUp(self):
        self.cog = GuildSyncCog(bot=FakeBot())

    async def test_joining_a_guild_records_it(self):
        await self.cog.on_guild_join(FakeDiscordGuild(42, "Otter Raft"))

        guild = await DiscordGuild.objects.aget(id=42)
        self.assertEqual(guild.name, "Otter Raft")

    async def test_joining_again_updates_the_name(self):
        await DiscordGuild.objects.acreate(id=42, name="Old")

        await self.cog.on_guild_join(FakeDiscordGuild(42, "New"))

        self.assertEqual((await DiscordGuild.objects.aget(id=42)).name, "New")

    async def test_leaving_deactivates_rather_than_deletes(self):
        """DiscordGuildPool and ActiveMatchMessage both cascade off the guild,
        so deleting the row would take the pool binding and every poll it ever
        posted with it - for what is often a temporary removal."""
        guild = await DiscordGuild.objects.acreate(id=42, name="Otter Raft")
        channel = await DiscordChannel.objects.acreate(id=1, guild=guild, name="c", channel_type="text")
        role = await DiscordGuildRole.objects.acreate(id=2, guild=guild, name="r")
        competition = await Competition.objects.acreate(name="C")
        season = await Season.objects.acreate(name="S", competition=competition, year=2026)
        pool = await PredictionPool.objects.acreate(name="P", season=season)
        binding = await DiscordGuildPool.objects.acreate(guild=guild, pool=pool, channel=channel)

        await self.cog.on_guild_remove(FakeDiscordGuild(42))

        self.assertTrue(await DiscordGuild.objects.filter(id=42).aexists())
        self.assertFalse((await DiscordGuildPool.objects.aget(pk=binding.pk)).is_active)
        self.assertFalse((await DiscordChannel.objects.aget(pk=channel.pk)).is_active)
        self.assertFalse((await DiscordGuildRole.objects.aget(pk=role.pk)).is_active)


class ChannelSyncCogTests(TestCase):
    """on_guild_channel_create wrote the guild's snowflake over the channel's
    own primary key, leaving guild_id null - every new channel raised."""

    def setUp(self):
        self.cog = ChannelSyncCog(bot=FakeBot())
        self.guild_row = DiscordGuild.objects.create(id=42, name="Otter Raft")
        self.guild = FakeDiscordGuild(42)

    async def test_a_new_channel_is_recorded_under_its_own_id(self):
        await self.cog.on_guild_channel_create(FakeGuildChannel(777, self.guild, name="bets", position=3))

        channel = await DiscordChannel.objects.aget(id=777)
        self.assertEqual((channel.guild_id, channel.name, channel.position), (42, "bets", 3))

    async def test_a_channel_in_an_unknown_guild_is_ignored(self):
        await self.cog.on_guild_channel_create(FakeGuildChannel(778, FakeDiscordGuild(999)))

        self.assertFalse(await DiscordChannel.objects.filter(id=778).aexists())

    async def test_deleting_a_channel_deactivates_it(self):
        await DiscordChannel.objects.acreate(id=777, guild=self.guild_row, name="bets", channel_type="text")

        await self.cog.on_guild_channel_delete(FakeGuildChannel(777, self.guild))

        self.assertFalse((await DiscordChannel.objects.aget(id=777)).is_active)


class RoleSyncCogTests(TestCase):
    """on_guild_role_create looked the guild up in the *role* table, so it
    always missed and returned early - new roles were silently never stored."""

    def setUp(self):
        self.cog = RoleSyncCog(bot=FakeBot())
        self.guild_row = DiscordGuild.objects.create(id=42, name="Otter Raft")
        self.guild = FakeDiscordGuild(42)

    async def test_a_new_role_is_recorded(self):
        await self.cog.on_guild_role_create(FakeGuildRole(555, self.guild, name="Otters", position=4))

        role = await DiscordGuildRole.objects.aget(id=555)
        self.assertEqual((role.guild_id, role.name, role.position), (42, "Otters", 4))

    async def test_a_role_in_an_unknown_guild_is_ignored(self):
        await self.cog.on_guild_role_create(FakeGuildRole(556, FakeDiscordGuild(999)))

        self.assertFalse(await DiscordGuildRole.objects.filter(id=556).aexists())


class MatchesNeedingPollsTests(TestCase):
    """The batch used to exclude only matches that had *both* an
    ActiveMatchMessage and a Prediction, so a poll nobody had voted on yet was
    posted a second time on the next run."""

    def setUp(self):
        self.competition = Competition.objects.create(name="World Cup")
        self.season = Season.objects.create(name="2026", competition=self.competition, year=2026)
        self.stage = Stage.objects.create(season=self.season, name="Group A", stage_type=StageType.GROUP)
        self.home = Team.objects.create(name="Germany")
        self.away = Team.objects.create(name="Brazil")
        self.pool = PredictionPool.objects.create(name="Pool", season=self.season)
        self.guild = DiscordGuild.objects.create(id=1, name="g")
        self.channel = DiscordChannel.objects.create(id=2, guild=self.guild, name="c", channel_type="text")
        self.now = timezone.now()

    def make_match(self, days):
        return Match.objects.create(
            stage=self.stage,
            home_team=self.home,
            away_team=self.away,
            kickoff=self.now + datetime.timedelta(days=days),
        )

    def polled(self, match):
        return ActiveMatchMessage.objects.create(
            match=match, guild=self.guild, pool=self.pool, channel=self.channel, poll_message_id=match.id
        )

    def needing(self):
        return set(
            matches_needing_polls(
                pool_id=self.pool.id,
                season_id=self.season.id,
                start=self.now,
                end=self.now + datetime.timedelta(days=7),
            ).values_list("id", flat=True)
        )

    def test_a_match_with_a_poll_but_no_votes_is_not_polled_again(self):
        polled = self.make_match(1)
        self.polled(polled)

        self.assertNotIn(polled.id, self.needing())

    def test_a_match_with_a_poll_and_votes_is_not_polled_again(self):
        polled = self.make_match(1)
        self.polled(polled)
        Prediction.objects.create(
            pool=self.pool,
            match=polled,
            user=User.objects.create(username="voter"),
            predicted_outcome=MatchOutcome.HOME_WIN,
        )

        self.assertNotIn(polled.id, self.needing())

    def test_an_unpolled_match_in_the_window_is_included(self):
        fresh = self.make_match(1)

        self.assertEqual(self.needing(), {fresh.id})

    def test_another_pools_poll_does_not_count(self):
        """Two pools can play the same season and each needs its own poll."""
        other_pool = PredictionPool.objects.create(name="Other", season=self.season)
        match = self.make_match(1)
        ActiveMatchMessage.objects.create(
            match=match, guild=self.guild, pool=other_pool, channel=self.channel, poll_message_id=99
        )

        self.assertEqual(self.needing(), {match.id})

    def test_matches_outside_the_window_are_excluded(self):
        self.make_match(30)
        self.make_match(-1)

        self.assertEqual(self.needing(), set())


class EmojiSyncGuardTests(TestCase):
    """The re-entrancy guard was only ever cleared, never set - so it never
    held, and on_ready fires again on every gateway reconnect."""

    async def test_the_guard_is_armed_while_the_sync_runs(self):
        cog = EmojiSyncCog(bot=FakeBot())
        observed = {}

        async def fetch_application_emojis():
            observed["armed"] = cog._sync_in_progress
            return []

        cog.bot.fetch_application_emojis = fetch_application_emojis

        await cog.on_ready()

        self.assertTrue(observed["armed"], "a second on_ready would have started a concurrent sync")
        self.assertFalse(cog._sync_in_progress, "and it has to be cleared again afterwards")

    async def test_a_second_sync_is_skipped_while_one_is_running(self):
        cog = EmojiSyncCog(bot=FakeBot())
        cog._sync_in_progress = True
        called = False

        async def fetch_application_emojis():
            nonlocal called
            called = True
            return []

        cog.bot.fetch_application_emojis = fetch_application_emojis

        await cog.on_ready()

        self.assertFalse(called)


class SyncPredictionsUserCreationTests(TestCase):
    """sync_predictions_from_poll created users itself, without the collision
    fallback aget_or_create_profile has - so a second voter whose Discord
    display name matched an existing user raised IntegrityError and took the
    whole reconciliation pass down."""

    async def test_two_voters_with_the_same_display_name_both_get_accounts(self):
        await User.objects.acreate_user(username="otter", is_active=True)

        first = await aget_or_create_profile(FakeVoter(111, "otter"))
        second = await aget_or_create_profile(FakeVoter(222, "otter"))

        self.assertNotEqual(first.user_id, second.user_id)
        self.assertEqual(await User.objects.filter(username__startswith="otter").acount(), 3)

    async def test_a_known_profile_is_returned_rather_than_recreated(self):
        user = await User.objects.acreate_user(username="known", is_active=True)
        profile = await DiscordProfile.objects.acreate(id=333, user=user, username="known")

        self.assertEqual((await aget_or_create_profile(FakeVoter(333, "known"))).pk, profile.pk)


class ReconciliationQueryCountTests(TestCase):
    """The "deactivate everything not live" sweep sat inside the per-channel
    loop, so it ran the same UPDATE once per channel.

    Sync tests driving the coroutine with async_to_sync: CaptureQueriesContext
    calls ensure_connection() on entry, which is sync-only.
    """

    @staticmethod
    def deactivations(queries):
        return [q for q in queries.captured_queries if "UPDATE" in q["sql"] and "is_active" in q["sql"]]

    def test_channels_are_deactivated_once_per_guild(self):
        guild = FakeDiscordGuild(42)
        guild.channels = [FakeGuildChannel(100 + n, guild, name=f"c{n}") for n in range(6)]
        cog = ReconciliationCog(bot=FakeBot(guilds=[guild]))

        with CaptureQueriesContext(connection) as queries:
            async_to_sync(cog.reconcile_channels)()

        self.assertEqual(len(self.deactivations(queries)), 1, "one sweep per guild, not one per channel")
        self.assertEqual(DiscordChannel.objects.count(), 6)

    def test_a_channel_that_is_gone_is_deactivated(self):
        guild_row = DiscordGuild.objects.create(id=42, name="Otter Raft")
        stale = DiscordChannel.objects.create(id=999, guild=guild_row, name="old", channel_type="text")
        guild = FakeDiscordGuild(42)
        guild.channels = [FakeGuildChannel(100, guild)]
        cog = ReconciliationCog(bot=FakeBot(guilds=[guild]))

        async_to_sync(cog.reconcile_channels)()

        stale.refresh_from_db()
        self.assertFalse(stale.is_active)

    def test_roles_are_deactivated_once_per_guild(self):
        guild = FakeDiscordGuild(42)
        guild.roles = [FakeGuildRole(200 + n, guild, name=f"r{n}") for n in range(5)]
        cog = ReconciliationCog(bot=FakeBot(guilds=[guild]))

        with CaptureQueriesContext(connection) as queries:
            async_to_sync(cog.reconcile_roles)()

        self.assertEqual(len(self.deactivations(queries)), 1)
        self.assertEqual(DiscordGuildRole.objects.count(), 5)


class FakePostedMessage:
    """A message the bot has just sent: editable, pinnable, identifiable."""

    def __init__(self, message_id, content=""):
        self.id = message_id
        self.content = content
        self.pinned = False
        self.edits = []

    async def pin(self):
        self.pinned = True

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeOnboardingChannel(discord.abc.Messageable):
    """Records every send, and hands sent messages back to fetch_message."""

    def __init__(self, channel_id=10):
        self.id = channel_id
        self.sends = []
        self.messages = {}
        self._next_id = 5000

    async def _get_channel(self):
        return self

    async def send(self, content=None, **kwargs):
        self._next_id += 1
        message = FakePostedMessage(self._next_id, content or "")
        self.messages[message.id] = message
        self.sends.append({"content": content, "message": message, **kwargs})
        return message

    async def fetch_message(self, message_id):
        if message_id not in self.messages:
            raise discord.NotFound(FakeResponse(), "unknown message")
        return self.messages[message_id]


class FakeResponse:
    """Enough of an aiohttp response for discord.NotFound to be constructible."""

    status = 404
    reason = "Not Found"


class PoolOnboardingTests(TestCase):
    """The season opener and the leaderboard message the bot posts for a pool
    it has just been bound to.

    Both exist because standing a pool up happens in the admin or a management
    command, i.e. in a container that cannot talk to Discord at all.
    """

    def setUp(self):
        self.competition = Competition.objects.create(name="NFL")
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        self.regular = Stage.objects.create(
            season=self.season, name="Regular Season", stage_type=StageType.LEAGUE, level=0
        )
        self.final = Stage.objects.create(
            season=self.season, name="Super Bowl", stage_type=StageType.KNOCK_OUT, level=4
        )
        self.pool = PredictionPool.objects.create(name="NFL 2026 Pool", season=self.season)
        PoolConfiguration.objects.update_or_create(
            pool=self.pool,
            defaults={
                "poll_creation_weekdays": [2],
                "poll_creation_time": datetime.time(18, 0),
                "poll_creation_lookahead_days": 7,
                "reminder_lead_minutes": 60,
            },
        )
        PoolStageRule.objects.create(pool=self.pool, stage=self.regular, level=0, points_per_correct=1)
        PoolStageRule.objects.create(pool=self.pool, stage=self.final, level=4, points_per_correct=5)

        self.guild = DiscordGuild.objects.create(id=1, name="Test Guild")
        self.channel = DiscordChannel.objects.create(id=10, guild=self.guild, name="pool", channel_type="text")
        self.role = DiscordGuildRole.objects.create(id=77, guild=self.guild, name="NFL Pool")
        self.guild_pool = DiscordGuildPool.objects.create(
            guild=self.guild,
            pool=self.pool,
            channel=self.channel,
            notification_role=self.role,
            is_active=True,
        )
        self.discord_channel = FakeOnboardingChannel(channel_id=self.channel.id)
        self.discord_role = FakeRole(self.role.id, self.role.name)

    def make_cog(self):
        bot = FakeBot(
            channel=self.discord_channel,
            guilds=[FakeGuild(self.guild.id, "Test Guild", roles=[self.discord_role])],
        )
        cog = PoolOnboardingCog(bot=bot)
        cog.cog_unload()  # the loop has nothing to do with a single pass
        return cog

    async def run_pass(self, cog=None):
        cog = cog or self.make_cog()
        guild_pool = await DiscordGuildPool.objects.select_related(
            "pool", "pool__season", "pool__configuration"
        ).aget(pk=self.guild_pool.pk)
        await cog.onboard(guild_pool)
        return cog

    async def test_a_new_binding_gets_a_welcome_then_a_leaderboard(self):
        await self.run_pass()

        contents = [send["content"] for send in self.discord_channel.sends]
        self.assertEqual(len(contents), 2)
        self.assertIn("Welcome to NFL 2026 Pool", contents[0])
        # Order matters: the welcome introduces the leaderboard below it.
        self.assertEqual(contents[1], LEADERBOARD_PLACEHOLDER)

        guild_pool = await DiscordGuildPool.objects.aget(pk=self.guild_pool.pk)
        self.assertEqual(guild_pool.welcome_msg, self.discord_channel.sends[0]["message"].id)
        self.assertEqual(guild_pool.leaderboard_msg, self.discord_channel.sends[1]["message"].id)

    async def test_only_the_leaderboard_is_pinned(self):
        """Pins are capped at 50 a channel and the polls need them; the
        welcome post is delivered by its role ping instead."""
        await self.run_pass()

        welcome, leaderboard = (send["message"] for send in self.discord_channel.sends)
        self.assertFalse(welcome.pinned)
        self.assertTrue(leaderboard.pinned)

    async def test_the_welcome_pings_the_notification_role(self):
        await self.run_pass()

        send = self.discord_channel.sends[0]
        self.assertIn(self.discord_role.mention, send["content"])
        self.assertTrue(send["allowed_mentions"].roles)
        self.assertFalse(send["allowed_mentions"].everyone)

    async def test_the_welcome_states_the_schedule_and_the_points(self):
        await self.run_pass()

        embed = self.discord_channel.sends[0]["embed"]
        values = {field.name: field.value for field in embed.fields}
        self.assertIn("Wednesday", values["🗳️ When polls appear"])
        self.assertIn("18:00", values["🗳️ When polls appear"])
        self.assertIn("**Regular Season** — 1 point(s) per correct pick", values["🏆 What a pick is worth"])
        self.assertIn("**Super Bowl** — 5 point(s) per correct pick", values["🏆 What a pick is worth"])
        self.assertIn("60 minutes", values["⏰ Reminders"])

    async def test_a_second_pass_posts_nothing(self):
        cog = await self.run_pass()
        await self.run_pass(cog)

        self.assertEqual(len(self.discord_channel.sends), 2)

    async def test_changed_points_edit_the_welcome_rather_than_repost_it(self):
        """A pool is created before its points per round are set, so the first
        render is the flat default. An edit corrects it without pinging again."""
        cog = await self.run_pass()

        await PoolStageRule.objects.filter(pool=self.pool, stage=self.final).aupdate(points_per_correct=9)
        await self.run_pass(cog)

        self.assertEqual(len(self.discord_channel.sends), 2)
        welcome = self.discord_channel.sends[0]["message"]
        self.assertEqual(len(welcome.edits), 1)
        embed = welcome.edits[0]["embed"]
        values = {field.name: field.value for field in embed.fields}
        self.assertIn("**Super Bowl** — 9 point(s) per correct pick", values["🏆 What a pick is worth"])

    async def test_the_flag_is_off_and_only_the_leaderboard_is_posted(self):
        """Every binding that predates this cog has the flag off, so a deploy
        cannot welcome a pool halfway through its season."""
        await DiscordGuildPool.objects.filter(pk=self.guild_pool.pk).aupdate(announce_welcome=False)

        await self.run_pass()

        contents = [send["content"] for send in self.discord_channel.sends]
        self.assertEqual(contents, [LEADERBOARD_PLACEHOLDER])

    async def test_a_deleted_welcome_is_not_reposted(self):
        """Deleting it is a choice; reposting would ping the role again for a
        season already under way."""
        cog = await self.run_pass()
        self.discord_channel.messages.pop(self.discord_channel.sends[0]["message"].id)

        await PoolStageRule.objects.filter(pool=self.pool, stage=self.final).aupdate(points_per_correct=9)
        await self.run_pass(cog)

        self.assertEqual(len(self.discord_channel.sends), 2)

    async def test_the_leaderboard_cog_does_not_create_the_message(self):
        """It renders into a message that exists and nothing more, so the two
        posts cannot race into a new channel in the wrong order."""
        bot = FakeBot(channel=self.discord_channel, guilds=[FakeGuild(self.guild.id, "Test Guild")])
        cog = LeaderboardSyncCog(bot=bot)
        cog.cog_unload()

        guild_pool = await DiscordGuildPool.objects.select_related("pool").aget(pk=self.guild_pool.pk)
        await cog.update_leaderboard_msg(guild_pool)

        self.assertEqual(self.discord_channel.sends, [])

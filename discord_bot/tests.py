from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from discord_bot.cogs.reconciliation import ReconciliationCog
from discord_bot.models import ActiveMatchMessage, DiscordChannel, DiscordGuild, DiscordGuildRole, DiscordProfile
from predictions.models import Prediction, PredictionPool
from sports.models import Competition, Match, MatchOutcome, MatchStatus, Season, Stage, StageType, Team

User = get_user_model()


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


class FakeThread:
    def __init__(self, message):
        self._message = message

    async def fetch_message(self, message_id):
        return self._message


class FakeChannel:
    def __init__(self, thread):
        self._thread = thread

    def get_thread(self, thread_id):
        return self._thread


class FakeRole:
    def __init__(self, role_id, name, position=0):
        self.id = role_id
        self.name = name
        self.position = position


class FakeGuild:
    def __init__(self, guild_id, name, roles=None):
        self.id = guild_id
        self.name = name
        self.roles = roles or []


class FakeBot:
    def __init__(self, channel=None, guilds=None):
        self._channel = channel
        self.guilds = guilds or []

    def get_channel(self, channel_id):
        return self._channel

    async def fetch_channel(self, channel_id):
        return self._channel


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
            thread_id=20,
            poll_message_id=30,
            is_poll_finalized=False,
        )

        self.alice = User.objects.create_user(username="alice")
        self.alice_profile = DiscordProfile.objects.create(id=111, user=self.alice, username="alice")
        self.bob = User.objects.create_user(username="bob")
        self.bob_profile = DiscordProfile.objects.create(id=222, user=self.bob, username="bob")

    def make_cog(self, answers):
        message = FakeMessage(poll=FakePoll(answers=answers))
        thread = FakeThread(message=message)
        channel = FakeChannel(thread=thread)
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

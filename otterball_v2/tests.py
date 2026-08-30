import importlib.util
import os
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import Client, SimpleTestCase, TestCase, override_settings


class PendingMigrationsTests(TestCase):
    def test_no_pending_migrations(self):
        # No migrations pending
        # See: https://adamj.eu/tech/2024/06/23/django-test-pending-migrations/
        out = StringIO()
        try:
            call_command(
                "makemigrations",
                "--check",
                stdout=out,
                stderr=StringIO(),
            )
        except SystemExit:  # pragma: no cover
            raise AssertionError("Pending migrations:\n" + out.getvalue()) from None


class MediaServingTests(TestCase):
    """`/media/` has to work with DEBUG off - that is the whole point.

    Each test builds its own Client *inside* the override, because the
    middleware chain is loaded on a handler's first request and reads these
    settings once.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.media_root = Path(self.tmp.name)

    def write(self, name, content=b"crest-bytes"):
        path = self.media_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_media_is_served_with_debug_off(self):
        self.write("team_logos/germany.png")

        with override_settings(DEBUG=False, MEDIA_ROOT=self.media_root):
            response = Client().get("/media/team_logos/germany.png")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"crest-bytes")

    def test_media_is_served_with_debug_on(self):
        """Development and production take the same path now; that is the fix."""
        self.write("team_logos/germany.png")

        with override_settings(DEBUG=True, MEDIA_ROOT=self.media_root):
            response = Client().get("/media/team_logos/germany.png")

        self.assertEqual(response.status_code, 200)

    def test_crest_written_after_startup_is_served(self):
        """Ingestion runs in the worker long after `web` booted.

        WhiteNoise indexes files once at startup, so without
        WHITENOISE_AUTOREFRESH a crest downloaded later 404s until the web
        container restarts.
        """
        with override_settings(DEBUG=False, MEDIA_ROOT=self.media_root):
            client = Client()
            self.assertEqual(client.get("/media/team_logos/new_team.png").status_code, 404)

            self.write("team_logos/new_team.png")

            self.assertEqual(client.get("/media/team_logos/new_team.png").status_code, 200)

    def test_missing_media_is_a_404_not_an_error(self):
        with override_settings(DEBUG=False, MEDIA_ROOT=self.media_root):
            response = Client().get("/media/team_logos/nope.png")

        self.assertEqual(response.status_code, 404)

    def test_media_serving_stays_inside_media_root(self):
        with override_settings(DEBUG=False, MEDIA_ROOT=self.media_root):
            response = Client().get("/media/../otterball_v2/settings.py")

        self.assertEqual(response.status_code, 404)

    def test_static_is_still_served(self):
        """The subclass must not cost us what WhiteNoise was already doing."""
        with tempfile.TemporaryDirectory() as static_root:
            (Path(static_root) / "site.css").write_bytes(b"body{}")

            with override_settings(DEBUG=False, STATIC_ROOT=static_root, MEDIA_ROOT=self.media_root):
                response = Client().get("/static/site.css")

        self.assertEqual(response.status_code, 200)


class EnvironmentFallbackTests(SimpleTestCase):
    """A variable declared in .env but left empty must not beat its default.

    compose passes every name in a service's environment block through, so
    `CELERY_RESULT_BACKEND=` in .env arrives as an empty string rather than as
    absent - and `os.getenv(name, default)` returns the empty string, which is
    how the worker came up with no result backend at all. Readiness reads the
    newest TaskResult to tell a *failing* scheduled task from one that merely
    has not run, so that silently cost a signal.
    """

    def load_settings(self, **environ):
        """Import a fresh copy of the settings module under its own name.

        Not importlib.reload: that would rebind the module django.conf.settings
        is holding, for the rest of the suite.
        """
        path = Path(__file__).resolve().parent / "settings.py"
        spec = importlib.util.spec_from_file_location("otterball_v2._settings_under_test", path)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, environ):
            spec.loader.exec_module(module)
        return module

    def test_empty_variables_fall_back_to_their_defaults(self):
        loaded = self.load_settings(
            DJANGO_SECRET_KEY="",
            ALLOWED_HOSTS="",
            REDIS_URL="",
            CELERY_BROKER_URL="",
            CELERY_RESULT_BACKEND="",
            TZ="",
        )

        self.assertTrue(loaded.SECRET_KEY)
        self.assertEqual(loaded.ALLOWED_HOSTS, ["*"])
        self.assertEqual(loaded.REDIS_URL, "redis://127.0.0.1:6379/1")
        self.assertEqual(loaded.CELERY_BROKER_URL, loaded.REDIS_URL)
        self.assertEqual(loaded.CELERY_RESULT_BACKEND, "django-db")
        self.assertEqual(loaded.TIME_ZONE, "Europe/Berlin")

    def test_a_set_variable_still_wins(self):
        loaded = self.load_settings(
            ALLOWED_HOSTS="example.com,127.0.0.1",
            REDIS_URL="redis://valkey:6379/0",
            CELERY_RESULT_BACKEND="rpc://",
            TZ="UTC",
        )

        self.assertEqual(loaded.ALLOWED_HOSTS, ["example.com", "127.0.0.1"])
        self.assertEqual(loaded.CELERY_BROKER_URL, "redis://valkey:6379/0")
        self.assertEqual(loaded.CELERY_RESULT_BACKEND, "rpc://")
        self.assertEqual(loaded.TIME_ZONE, "UTC")

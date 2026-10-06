from unittest import mock

from django.test import SimpleTestCase

from sports import tasks


class RunHelperConnectionTests(SimpleTestCase):
    """``sports.tasks._run`` cleans up the async ORM's connection around every ingestion call.

    Celery's Django fixup only closes connections of the task's own thread; the ingest functions query
    from asgiref's executor thread, whose connection stayed dead after a database restart and made every
    live sync fail with "connection already closed" until the worker was restarted.
    """

    def test_ingestion_runs_between_two_cleanups(self):
        events = []

        async def ingest():
            events.append("ingest")
            return "done"

        async def cleanup():
            events.append("cleanup")

        with mock.patch.object(tasks, "aclose_old_connections", cleanup):
            result = tasks._run(ingest())

        self.assertEqual(result, "done")
        self.assertEqual(events, ["cleanup", "ingest", "cleanup"])

    def test_cleanup_also_runs_when_ingestion_fails(self):
        events = []

        async def ingest():
            events.append("ingest")
            raise RuntimeError("API down")

        async def cleanup():
            events.append("cleanup")

        with mock.patch.object(tasks, "aclose_old_connections", cleanup), self.assertRaises(RuntimeError):
            tasks._run(ingest())

        self.assertEqual(events, ["cleanup", "ingest", "cleanup"])

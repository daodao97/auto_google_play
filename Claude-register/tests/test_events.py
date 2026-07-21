from __future__ import annotations

import unittest


class TestRunEventBus(unittest.TestCase):
    def test_replays_only_events_after_the_client_cursor(self):
        from claude_register.orchestration.events import RunEventBus

        bus = RunEventBus(max_history=4)
        first = bus.publish("task_updated", {"task": {"task_id": "one", "version": 1}})
        second = bus.publish("summary_updated", {"summary": {"running": 1}})

        replay = bus.events_after(first.event_id)

        self.assertIsNotNone(replay)
        self.assertEqual([event.event_id for event in replay or []], [second.event_id])
        self.assertEqual([event.event_type for event in replay or []], ["summary_updated"])

    def test_reports_a_history_gap_instead_of_silently_losing_updates(self):
        from claude_register.orchestration.events import RunEventBus

        bus = RunEventBus(max_history=2)
        cursor = bus.cursor
        bus.publish("task_updated", {"task": {"task_id": "one", "version": 1}})
        bus.publish("task_updated", {"task": {"task_id": "one", "version": 2}})
        bus.publish("task_updated", {"task": {"task_id": "one", "version": 3}})

        self.assertIsNone(bus.events_after(cursor))

    def test_published_payload_is_immutable_from_the_callers_perspective(self):
        from claude_register.orchestration.events import RunEventBus

        bus = RunEventBus()
        payload = {"task": {"task_id": "one", "version": 1}}
        event = bus.publish("task_updated", payload)
        payload["task"]["version"] = 99

        self.assertEqual(event.data["task"]["version"], 1)

    def test_summary_tracker_updates_one_task_without_recounting_the_run(self):
        from claude_register.orchestration.events import RunSummaryTracker

        tracker = RunSummaryTracker()
        tracker.reset(
            [
                {
                    "task_id": "one",
                    "status": "pending",
                    "kyc_status": "",
                    "has_session": False,
                },
                {
                    "task_id": "two",
                    "status": "running",
                    "kyc_status": "",
                    "has_session": False,
                },
            ]
        )

        summary = tracker.update(
            {
                "task_id": "one",
                "status": "success",
                "kyc_status": "not_required",
                "has_session": True,
            }
        )

        self.assertEqual(
            summary,
            {
                "total": 2,
                "success": 1,
                "partial": 0,
                "failed": 0,
                "running": 1,
                "pending": 0,
                "kyc_pass": 1,
                "kyc_required": 0,
                "kyc_dead": 0,
                "kyc_unknown": 0,
            },
        )
        self.assertEqual(tracker.update({
            "task_id": "one",
            "status": "success",
            "kyc_status": "not_required",
            "has_session": True,
        }), summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)

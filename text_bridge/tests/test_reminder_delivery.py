import unittest

from text_bridge.bridge.delivery_journal import DeliveryJournal
from text_bridge.bridge.session_state_machine import BridgeState, InteractionStateMachine


class _Client:
    async def occurrence(self, _id):
        return {
            "occurrence_id": "occ-1", "elder_id": "E001", "plan_id": "plan-1",
            "plan_version": 1, "intake_status": "unconfirmed",
            "interactions": [{"interaction_id": "i-1", "status": "open", "expires_at": "2099-01-01T00:00:00Z"}],
        }

    async def plans(self, _id):
        return [{"plan_id": "plan-1", "version": 1, "status": "active"}]


class ReminderDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_notification_is_queued_without_overwriting_current(self):
        sent = []

        async def send(message):
            sent.append(message)

        async def ack(_message):
            return True

        machine = InteractionStateMachine(
            session_id="s", elder_id="E001", device_sn="d",
            medication=_Client(), journal=DeliveryJournal(":memory:"),
            send=send, wait_ack=ack,
        )
        first = {"interaction_id": "i-1", "elder_id": "E001", "device_sn": "d", "occurrence_id": "occ-1",
                 "expires_at": "2099-01-01T00:00:00Z", "text": "第一条", "opened_at": "2026-01-01T00:00:00Z"}
        second = dict(first, interaction_id="i-2", text="第二条", opened_at="2026-01-01T00:00:01Z")
        await machine.enqueue(first)
        await machine.enqueue(second)
        self.assertEqual(machine.current_interaction_id, "i-1")
        self.assertEqual(machine.snapshot()["queue_interaction_ids"], ["i-2"])


if __name__ == "__main__":
    unittest.main()

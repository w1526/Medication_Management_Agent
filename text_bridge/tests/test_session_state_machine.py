import unittest

from text_bridge.bridge.delivery_journal import DeliveryJournal
from text_bridge.bridge.medication_client import MedicationClientError
from text_bridge.bridge.protocol import make_message
from text_bridge.bridge.session_state_machine import BridgeState, InteractionStateMachine


class FakeMedication:
    def __init__(self, *, fail_response=False):
        self.fail_response = fail_response
        self.response_calls = []
        self.device_calls = []
        self.notification = {
            "interaction_id": "interaction-1",
            "elder_id": "E001",
            "device_sn": "device-1",
            "occurrence_id": "occurrence-1",
            "plan_id": "plan-1",
            "plan_version": 1,
            "opened_at": "2026-09-18T08:00:00+00:00",
            "expires_at": "2099-09-18T08:30:00+00:00",
            "text": "该吃药了。",
            "attempt_id": "attempt-1",
        }

    async def notifications(self, elder_id):
        return [dict(self.notification)]

    async def occurrence(self, occurrence_id):
        return {
            "occurrence_id": occurrence_id,
            "elder_id": "E001",
            "plan_id": "plan-1",
            "plan_version": 1,
            "intake_status": "unconfirmed",
            "interactions": [
                {
                    "interaction_id": "interaction-1",
                    "status": "open",
                    "expires_at": "2099-09-18T08:30:00+00:00",
                }
            ],
        }

    async def plans(self, plan_id):
        return [{"plan_id": "plan-1", "version": 1, "status": "active"}]

    async def response(self, data):
        self.response_calls.append(dict(data))
        if self.fail_response:
            raise MedicationClientError("temporary outage")
        status = {
            "CONFIRM_TAKEN": "confirmed_taken",
            "SKIP": "skipped",
        }.get(data["action"])
        return {"occurrence": {"intake_status": status or "unconfirmed"}}

    async def device_event(self, data):
        self.device_calls.append(dict(data))
        return {"duplicate": False}


class StateMachineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent = []
        self.medication = FakeMedication()
        self.journal = DeliveryJournal(":memory:")

        async def send(message):
            self.sent.append(message)

        async def wait_ack(_message):
            return True

        self.machine = InteractionStateMachine(
            session_id="session-1",
            elder_id="E001",
            device_sn="device-1",
            medication=self.medication,
            journal=self.journal,
            send=send,
            wait_ack=wait_ack,
            retry_delays=(0.0,),
        )

    async def asyncTearDown(self):
        self.journal.close()

    async def _finish_reminder_playback(self):
        speak = self.sent[-1]
        status = make_message(
            "playback_status",
            "session-1",
            {"playback_id": speak["payload"]["playback_id"], "status": "started"},
            interaction_id="interaction-1",
            binding_revision=self.machine.binding_revision,
            reply_to=speak["message_id"],
        )
        await self.machine.handle_playback_status(status)
        complete = dict(status)
        complete["message_id"] = "playback-complete"
        complete["sent_at"] = "2099-09-18T08:01:00Z"
        complete["payload"] = dict(status["payload"], status="completed")
        await self.machine.handle_playback_status(complete)

    async def test_queue_binds_then_waits_and_confirms_current_interaction(self):
        await self.machine.enqueue(self.medication.notification)
        self.assertEqual(self.machine.state, BridgeState.REMINDER_PENDING)
        self.assertEqual([item["type"] for item in self.sent], ["interaction.bind", "speak"])
        await self._finish_reminder_playback()
        self.assertEqual(self.machine.state, BridgeState.AWAITING_RESPONSE)

        user = make_message(
            "user_text",
            "session-1",
            {"turn_id": "turn-1", "text": "吃了"},
            interaction_id="interaction-1",
            binding_revision=self.machine.binding_revision,
        )
        await self.machine.handle_user_text(user)
        self.assertEqual(len(self.medication.response_calls), 1)
        self.assertEqual(self.medication.response_calls[0]["event_id"], "bridge:session-1:turn-1")
        self.assertEqual(self.machine.state, BridgeState.PLAYING)
        self.assertTrue(any(item["type"] == "interaction.clear" for item in self.sent))

    async def test_negative_text_is_clarified_without_business_request(self):
        await self.machine.enqueue(self.medication.notification)
        await self._finish_reminder_playback()
        user = make_message(
            "user_text",
            "session-1",
            {"turn_id": "turn-1", "text": "不吃了"},
            interaction_id="interaction-1",
            binding_revision=self.machine.binding_revision,
        )
        await self.machine.handle_user_text(user)
        self.assertEqual(self.medication.response_calls, [])
        self.assertEqual(self.sent[-2]["type"], "turn.result")
        self.assertEqual(self.sent[-2]["payload"]["decision"], "clarify")


if __name__ == "__main__":
    unittest.main()

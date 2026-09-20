import unittest

from text_bridge.bridge.delivery_journal import DeliveryJournal
from text_bridge.bridge.medication_client import MedicationClientError
from text_bridge.bridge.protocol import make_message
from text_bridge.bridge.session_state_machine import BridgeState, InteractionStateMachine


class _UncertainClient:
    async def occurrence(self, _id):
        return {"occurrence_id": "o", "elder_id": "E001", "plan_id": "p", "plan_version": 1,
                "intake_status": "unconfirmed", "interactions": [{"interaction_id": "i", "status": "open",
                "expires_at": "2099-01-01T00:00:00Z"}]}

    async def plans(self, _id):
        return [{"plan_id": "p", "version": 1, "status": "active"}]

    async def response(self, _data):
        raise MedicationClientError("timeout")


class RetryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_response_keeps_original_turn_and_blocks_next_item(self):
        sent = []

        async def send(message):
            sent.append(message)

        async def ack(_message):
            return True

        journal = DeliveryJournal(":memory:")
        machine = InteractionStateMachine(
            session_id="s", elder_id="E001", device_sn="d", medication=_UncertainClient(),
            journal=journal, send=send, wait_ack=ack, retry_delays=(0.0,),
        )
        notification = {"interaction_id": "i", "elder_id": "E001", "device_sn": "d", "occurrence_id": "o",
                        "expires_at": "2099-01-01T00:00:00Z", "text": "提醒", "opened_at": "2026-01-01T00:00:00Z"}
        await machine.enqueue(notification)
        speak = sent[-1]
        started = make_message("playback_status", "s", {"playback_id": speak["payload"]["playback_id"], "status": "started"},
                               interaction_id="i", binding_revision=machine.binding_revision, reply_to=speak["message_id"])
        await machine.handle_playback_status(started)
        completed = dict(started, message_id="done", sent_at="2099-01-01T00:00:01Z",
                         payload=dict(started["payload"], status="completed"))
        await machine.handle_playback_status(completed)
        user = make_message("user_text", "s", {"turn_id": "turn-1", "text": "吃了"},
                            interaction_id="i", binding_revision=machine.binding_revision)
        await machine.handle_user_text(user)
        self.assertEqual(machine.state, BridgeState.PROCESSING_RESPONSE)
        self.assertEqual(machine.current_turn_id, "turn-1")
        self.assertTrue(machine.snapshot()["recovery_blocked"])
        pending = [item for item in sent if item["type"] == "turn.result"][-1]
        self.assertEqual(pending["payload"]["decision"], "pending")
        self.assertEqual(journal.get_turn("s", "turn-1")["event_id"], "bridge:s:turn-1")
        journal.close()


if __name__ == "__main__":
    unittest.main()

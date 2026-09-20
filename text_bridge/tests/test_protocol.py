import unittest

from text_bridge.bridge.delivery_journal import DeliveryJournal, JournalConflict
from text_bridge.bridge.protocol import (
    ProtocolError,
    decode_message,
    encode_message,
    make_message,
)


class ProtocolTests(unittest.TestCase):
    def test_register_and_related_messages_are_valid(self):
        register = make_message(
            "session.register",
            "session-1",
            {
                "tenant_id": "test_tenant",
                "elder_id": "E001",
                "device_sn": "test_device_001",
                "room_name": "test_room_001",
            },
        )
        self.assertEqual(decode_message(encode_message(register))["version"], "1")
        user = make_message(
            "user_text",
            "session-1",
            {"turn_id": "turn-1", "text": "吃了"},
            interaction_id="interaction-1",
            binding_revision=1,
        )
        self.assertEqual(user["payload"]["turn_id"], "turn-1")

    def test_invalid_revision_and_oversize_are_rejected(self):
        with self.assertRaises(ProtocolError):
            make_message(
                "user_text",
                "session-1",
                {"turn_id": "turn-1", "text": "吃了"},
                interaction_id="interaction-1",
                binding_revision=0,
            )
        with self.assertRaises(ProtocolError):
            decode_message(("{" + "x" * (32 * 1024) + "}").encode("utf-8"))

    def test_same_message_id_with_different_content_is_a_conflict(self):
        journal = DeliveryJournal(":memory:")
        try:
            message = make_message("session.ready", "session-1", {"status": "ready"}, message_id="m-1")
            self.assertEqual(journal.record_message(message, "bridge"), "new")
            self.assertEqual(journal.record_message(message, "bridge"), "duplicate")
            changed = dict(message)
            changed["payload"] = {"status": "changed"}
            with self.assertRaises(JournalConflict):
                journal.record_message(changed, "bridge")
        finally:
            journal.close()


if __name__ == "__main__":
    unittest.main()

from app.history_sync import attachment_placeholder, fetch_message_history, message_direction


class FakeHistoryClient:
    def __init__(self):
        self.before_values = []

    def get_messages(self, _conversation_id, before=None):
        self.before_values.append(before)
        if before is None:
            return {
                "payload": {"messages": [{"id": value} for value in range(21, 41)]},
                "meta": {"contact": {"name": "Customer"}},
            }
        if before == 21:
            return {"payload": {"messages": [{"id": value} for value in range(1, 21)]}}
        return {"payload": {"messages": []}}


def test_message_history_uses_before_cursor_until_empty():
    client = FakeHistoryClient()
    messages, metadata = fetch_message_history(client, 55)
    assert [item["id"] for item in messages] == list(range(1, 41))
    assert client.before_values == [None, 21, 1]
    assert metadata["contact"]["name"] == "Customer"


def test_remote_message_types_are_normalized():
    assert message_direction(0) == "incoming"
    assert message_direction(1) == "outgoing"
    assert message_direction(2) == "activity"


def test_attachment_without_text_gets_visible_placeholder():
    assert attachment_placeholder([{"file_type": "image"}]) == ("[图片]", "image")
    assert attachment_placeholder([{"extension": "pdf"}]) == ("[文件]", "file")

"""发往听感模型的请求。"""

import base64

import pytest

from perception.listen_client import SiliconFlowListenClient


class RecordingTransport:
    def __init__(self, response: dict | None = None, error: Exception | None = None):
        self.response = response or {}
        self.error = error
        self.calls: list[dict] = []

    def post(self, url: str, headers: dict, body: dict, timeout: float) -> dict:
        self.calls.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        if self.error:
            raise self.error
        return self.response


def test_request_sends_audio_and_the_question_only():
    transport = RecordingTransport(
        {
            "choices": [{"message": {"content": '{"犹豫": "是", "说完": "否"}'}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        }
    )
    client = SiliconFlowListenClient(
        api_key="sk-test",
        base_url="https://api.siliconflow.cn/v1",
        transport=transport,
    )

    reply = client.listen(b"RIFFfake", "turn.wav")

    call = transport.calls[0]
    assert call["url"] == "https://api.siliconflow.cn/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    assert call["timeout"] == 120
    body = call["body"]
    assert body["model"] == "Qwen/Qwen3-Omni-30B-A3B-Instruct"
    assert body["temperature"] == 0
    assert body["stream"] is False
    content = body["messages"][0]["content"]
    assert [part["type"] for part in content] == ["audio_url", "text"]
    encoded = content[0]["audio_url"]["url"]
    assert encoded == "data:audio/wav;base64," + base64.b64encode(b"RIFFfake").decode()
    question = content[1]["text"]
    assert "犹豫" in question
    assert "说完" in question
    assert "是" in question and "否" in question
    assert "不要" in question and "情绪" in question
    assert "转写" not in body["messages"][0]
    assert reply.text == '{"犹豫": "是", "说完": "否"}'
    assert reply.usage == {"prompt_tokens": 4, "completion_tokens": 2}


def test_missing_usage_stays_absent():
    transport = RecordingTransport(
        {"choices": [{"message": {"content": '{"犹豫": "否", "说完": "是"}'}}]}
    )
    client = SiliconFlowListenClient(api_key="sk-test", base_url="https://api.example/v1", transport=transport)

    reply = client.listen(b"abc", "turn.mp3")

    assert reply.usage is None
    assert transport.calls[0]["body"]["messages"][0]["content"][0]["audio_url"]["url"].startswith(
        "data:audio/mpeg;base64,"
    )


def test_timeout_is_not_turned_into_a_call_error():
    transport = RecordingTransport(error=TimeoutError("hung"))
    client = SiliconFlowListenClient(api_key="sk-test", base_url="https://api.example/v1", transport=transport)

    with pytest.raises(TimeoutError):
        client.listen(b"abc", "turn.wav")


def test_transport_failure_is_a_call_error():
    from perception.listen_bakeoff import ListenCallError

    transport = RecordingTransport(error=RuntimeError("500"))
    client = SiliconFlowListenClient(api_key="sk-test", base_url="https://api.example/v1", transport=transport)

    with pytest.raises(ListenCallError):
        client.listen(b"abc", "turn.wav")

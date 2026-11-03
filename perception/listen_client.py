"""把一轮录音发给 SiliconFlow 上的听感模型，只要文字。"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol

from perception.listen_bakeoff import ListenCallError, ListenReply

_MODEL = "Qwen/Qwen3-Omni-30B-A3B-Instruct"
_WAIT_SECONDS = 120.0
_MIME = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
}
_QUESTION = (
    "听这一轮考生发言，只返回 JSON，不要其他文字。"
    "犹豫和说完只能是「是」或「否」。"
    "犹豫指说法里有明显的停顿、重复或改口，不是情绪。"
    "说完指这一轮回答讲到了收束，不是面试结束。"
    "不要判断情绪、性格或能力。"
    '格式：{"犹豫":"是","说完":"否"}'
)


class ListenTransport(Protocol):
    def post(self, url: str, headers: dict, body: dict, timeout: float) -> dict:
        """发出一次 JSON 请求。超时抛出 TimeoutError。"""


class UrllibListenTransport:
    def post(self, url: str, headers: dict, body: dict, timeout: float) -> dict:
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode())
        except TimeoutError:
            raise
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError(str(exc.reason)) from exc
            raise


class SiliconFlowListenClient:
    def __init__(self, api_key: str, base_url: str, transport: ListenTransport | None = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.transport = transport or UrllibListenTransport()

    def listen(self, audio: bytes, filename: str) -> ListenReply:
        mime = _MIME.get(Path(filename).suffix.lower(), "application/octet-stream")
        encoded = base64.b64encode(audio).decode()
        body = {
            "model": _MODEL,
            "temperature": 0,
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "audio_url",
                            "audio_url": {"url": f"data:{mime};base64,{encoded}"},
                        },
                        {"type": "text", "text": _QUESTION},
                    ],
                }
            ],
        }
        try:
            payload = self.transport.post(
                f"{self.base_url}/chat/completions",
                {"Authorization": f"Bearer {self.api_key}"},
                body,
                _WAIT_SECONDS,
            )
        except TimeoutError:
            raise
        except Exception as exc:
            raise ListenCallError(str(exc)) from exc
        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ListenCallError("听感模型没有返回文字") from exc
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            usage = None
        return ListenReply(str(text), usage)

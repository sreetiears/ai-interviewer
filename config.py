import os
from dataclasses import dataclass
from dotenv import load_dotenv

# Automatically load environment variables from .env if present
load_dotenv()


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name, default)
    return value.strip() if isinstance(value, str) else default


def _env_int(name: str, default: int) -> int:
    try:
        val = os.getenv(name)
        return int(val) if val is not None else default
    except ValueError:
        return default


@dataclass(frozen=True)
class InterviewConfig:
    # LiveKit
    livekit_url: str = _env("LIVEKIT_URL", "ws://127.0.0.1:7880")
    livekit_api_key: str = _env("LIVEKIT_API_KEY", "devkey")
    livekit_api_secret: str = _env("LIVEKIT_API_SECRET", "secret")

    # Backend selection: "langgraph" | "dify"
    llm_backend: str = _env("LLM_BACKEND", "langgraph")

    # LLM Settings (for LangGraph, OpenAI-compatible)
    llm_api_key: str = _env("LLM_API_KEY", _env("SILICONFLOW_API_KEY", ""))
    llm_base_url: str = _env("LLM_BASE_URL", _env("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"))
    llm_model: str = _env("LLM_MODEL", "Qwen/Qwen2.5-7B-Instruct")

    # Dify Settings (legacy fallback)
    dify_api_key: str = _env("DIFY_API_KEY", "")
    dify_base_url: str = _env("DIFY_BASE_URL", "http://127.0.0.1/v1")

    # Voice / SiliconFlow STT & TTS
    siliconflow_api_key: str = _env("SILICONFLOW_API_KEY", "")
    siliconflow_base_url: str = _env("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
    stt_model: str = _env("STT_MODEL", "FunAudioLLM/SenseVoiceSmall")
    stt_language: str = _env("STT_LANGUAGE", "zh")
    tts_model: str = _env("TTS_MODEL", "FunAudioLLM/CosyVoice2-0.5B")
    tts_voice: str = _env("TTS_VOICE", "FunAudioLLM/CosyVoice2-0.5B:alex")
    tts_response_format: str = _env("TTS_RESPONSE_FORMAT", "mp3")

    # Prompts & Rules
    agent_instructions: str = _env(
        "AGENT_INSTRUCTIONS",
        "你是一名中文考研复试 AI 面试官。语气自然、专业、严谨且温和。严格按面试阶段引导考生。",
    )
    opening_prompt: str = _env(
        "OPENING_PROMPT",
        "请先向考生打招呼，并邀请对方开始自我介绍。",
    )
    max_follow_ups: int = _env_int("MAX_FOLLOW_UPS", 1)


config = InterviewConfig()

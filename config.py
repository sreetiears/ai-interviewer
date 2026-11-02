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


def _env_float(name: str, default: float) -> float:
    try:
        val = os.getenv(name)
        return float(val) if val is not None else default
    except ValueError:
        return default


@dataclass(frozen=True)
class InterviewConfig:
    # LiveKit
    livekit_url: str = _env("LIVEKIT_URL", "ws://127.0.0.1:7880")
    livekit_api_key: str = _env("LIVEKIT_API_KEY", "devkey")
    livekit_api_secret: str = _env("LIVEKIT_API_SECRET", "secret")

    # LLM Settings (LangGraph, OpenAI-compatible)
    llm_api_key: str = _env("LLM_API_KEY", _env("SILICONFLOW_API_KEY", ""))
    llm_base_url: str = _env("LLM_BASE_URL", _env("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"))
    llm_model: str = _env("LLM_MODEL", "Qwen/Qwen2.5-7B-Instruct")
    llm_timeout_seconds: float = _env_float("LLM_TIMEOUT_SECONDS", 30.0)
    llm_max_retries: int = _env_int("LLM_MAX_RETRIES", 2)

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
    # Spoken verbatim by the agent when the room opens.  This used to be an
    # instruction passed through the LLM, which forced the adapter to recognise
    # the instruction by matching the words "打招呼" — changing this text broke
    # the opening.  It is now a plain line of speech.
    greeting_text: str = _env(
        "GREETING_TEXT",
        "同学你好，欢迎参加考研复试模拟面试，请先进行1分钟左右的自我介绍。",
    )
    # Self-introduction gate: a too-short intro is sent back for more detail, but
    # a bounded number of times so the interview can never stall.
    min_self_intro_chars: int = _env_int("MIN_INTRO_CHARS", 50)

    # Interview mode: "quick" (short run, for demo recordings) or "real" (a
    # full-length interview with several questions per phase). The browser can
    # override the mode per session through the token endpoint.
    interview_mode: str = _env("INTERVIEW_MODE", "quick")
    quick_questions_per_phase: int = _env_int("QUICK_QUESTIONS_PER_PHASE", 1)
    real_questions_per_phase: int = _env_int("REAL_QUESTIONS_PER_PHASE", 3)
    quick_max_follow_ups: int = _env_int("QUICK_MAX_FOLLOW_UPS", _env_int("MAX_FOLLOW_UPS", 1))
    real_max_follow_ups: int = _env_int("REAL_MAX_FOLLOW_UPS", 2)
    quick_max_intro_attempts: int = _env_int(
        "QUICK_MAX_INTRO_ATTEMPTS", _env_int("MAX_INTRO_ATTEMPTS", 1)
    )
    real_max_intro_attempts: int = _env_int("REAL_MAX_INTRO_ATTEMPTS", 3)


config = InterviewConfig()

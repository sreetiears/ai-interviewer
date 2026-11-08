import os
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv

# 若存在 .env，则自动加载其中的环境变量
load_dotenv()

# 仅允许在测试（ALLOW_DUMMY_KEYS=1）中使用；真实演示启动必须拒绝。
DUMMY_API_KEY = "sk-dummy"


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


def allow_dummy_keys() -> bool:
    """测试/离线装配时可显式允许占位 Key。"""
    raw = os.getenv("ALLOW_DUMMY_KEYS", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def is_usable_api_key(value: str | None) -> bool:
    key = (value or "").strip()
    return bool(key) and key != DUMMY_API_KEY


def resolve_api_key(value: str | None) -> str:
    """返回可用 API Key；仅当 ALLOW_DUMMY_KEYS 打开时才回退到占位值。"""
    key = (value or "").strip()
    if is_usable_api_key(key):
        return key
    if allow_dummy_keys():
        return DUMMY_API_KEY
    return key


@dataclass(frozen=True)
class InterviewConfig:
    # LiveKit 连接
    livekit_url: str = _env("LIVEKIT_URL", "ws://127.0.0.1:7880")
    livekit_api_key: str = _env("LIVEKIT_API_KEY", "devkey")
    livekit_api_secret: str = _env("LIVEKIT_API_SECRET", "secret")

    # 大模型设置（LangGraph，兼容 OpenAI 接口）
    llm_api_key: str = _env("LLM_API_KEY", _env("SILICONFLOW_API_KEY", ""))
    llm_base_url: str = _env("LLM_BASE_URL", _env("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"))
    llm_model: str = _env("LLM_MODEL", "Qwen/Qwen2.5-7B-Instruct")
    llm_timeout_seconds: float = _env_float("LLM_TIMEOUT_SECONDS", 30.0)
    llm_max_retries: int = _env_int("LLM_MAX_RETRIES", 2)

    # 语音 / SiliconFlow 语音识别与语音合成
    siliconflow_api_key: str = _env("SILICONFLOW_API_KEY", "")
    siliconflow_base_url: str = _env("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
    stt_model: str = _env("STT_MODEL", "FunAudioLLM/SenseVoiceSmall")
    stt_language: str = _env("STT_LANGUAGE", "zh")
    tts_model: str = _env("TTS_MODEL", "FunAudioLLM/CosyVoice2-0.5B")
    tts_voice: str = _env("TTS_VOICE", "FunAudioLLM/CosyVoice2-0.5B:alex")
    tts_response_format: str = _env("TTS_RESPONSE_FORMAT", "mp3")

    # 提示词与规则
    agent_instructions: str = _env(
        "AGENT_INSTRUCTIONS",
        "你是一名中文考研复试 AI 面试官。语气自然、专业、严谨且温和。严格按面试阶段引导考生。",
    )
    # 房间打开时由智能体逐字说出。以前这是一条送进大模型的指令，适配器必须
    # 靠匹配“打招呼”这几个字来识别它——一改这段文字，开场就会坏掉。
    # 现在它只是一句普通的开场白。
    greeting_text: str = _env(
        "GREETING_TEXT",
        "同学你好，欢迎参加考研复试模拟面试，请先进行1分钟左右的自我介绍。",
    )
    # 自我介绍门槛：过短的介绍会被退回要求补充，但次数有上限，面试不会因此卡住。
    min_self_intro_chars: int = _env_int("MIN_INTRO_CHARS", 50)

    # 面试模式："quick"（短流程，适合演示录制）或 "real"（每个阶段多道题的
    # 完整面试）。浏览器可以在每次会话通过令牌接口覆盖该模式。
    interview_mode: str = _env("INTERVIEW_MODE", "quick")
    quick_questions_per_phase: int = _env_int("QUICK_QUESTIONS_PER_PHASE", 1)
    real_questions_per_phase: int = _env_int("REAL_QUESTIONS_PER_PHASE", 3)
    quick_max_follow_ups: int = _env_int("QUICK_MAX_FOLLOW_UPS", _env_int("MAX_FOLLOW_UPS", 1))
    real_max_follow_ups: int = _env_int("REAL_MAX_FOLLOW_UPS", 2)
    quick_max_intro_attempts: int = _env_int(
        "QUICK_MAX_INTRO_ATTEMPTS", _env_int("MAX_INTRO_ATTEMPTS", 1)
    )
    real_max_intro_attempts: int = _env_int("REAL_MAX_INTRO_ATTEMPTS", 3)


def listen_bakeoff_dirs() -> tuple[str, str]:
    """离线横评的录音目录和记录目录。都由本机配置给出，不指向仓库内。"""
    return _env("LISTEN_BAKEOFF_AUDIO_DIR"), _env("LISTEN_BAKEOFF_RECORD_DIR")


def delivery_metrics_enabled() -> bool:
    """成绩单是否读入表达特征。本地默认打开，评测时可关掉。"""
    raw = os.getenv("DELIVERY_METRICS", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def check_runtime_readiness(
    *,
    llm_api_key: str | None = None,
    siliconflow_api_key: str | None = None,
) -> dict[str, Any]:
    """检查演示运行所需的 Key 是否就绪（每次实时读 env，便于 health/预检）。"""
    llm_key = llm_api_key if llm_api_key is not None else _env(
        "LLM_API_KEY", _env("SILICONFLOW_API_KEY", "")
    )
    speech_key = (
        siliconflow_api_key
        if siliconflow_api_key is not None
        else _env("SILICONFLOW_API_KEY", "")
    )
    llm_configured = is_usable_api_key(llm_key)
    speech_configured = is_usable_api_key(speech_key)
    issues: list[str] = []
    if not llm_configured:
        issues.append("缺少可用的 LLM_API_KEY（也可复用 SILICONFLOW_API_KEY）")
    if not speech_configured:
        issues.append("缺少可用的 SILICONFLOW_API_KEY（STT/TTS 需要）")
    status = "ok" if not issues else "degraded"
    return {
        "status": status,
        "llm_configured": llm_configured,
        "speech_configured": speech_configured,
        "issues": issues,
    }


def health_payload(*, default_mode: str = "quick") -> dict[str, Any]:
    """``GET /api/health`` 用的结构化载荷（camelCase，给前端）。"""
    readiness = check_runtime_readiness()
    return {
        "status": readiness["status"],
        "deliveryMetrics": delivery_metrics_enabled(),
        "llmConfigured": readiness["llm_configured"],
        "speechConfigured": readiness["speech_configured"],
        "defaultMode": default_mode,
        "issues": list(readiness["issues"]),
    }


def assert_runtime_ready() -> None:
    """Agent 启动前调用：未就绪则抛出带中文说明的 RuntimeError。"""
    if allow_dummy_keys():
        return
    readiness = check_runtime_readiness()
    if readiness["status"] == "ok":
        return
    detail = "；".join(readiness["issues"]) or "配置不完整"
    raise RuntimeError(
        f"{detail}。请检查 .env 后重试；无 Key 时可用浏览器 ?demo=1 演示界面。"
    )


config = InterviewConfig()

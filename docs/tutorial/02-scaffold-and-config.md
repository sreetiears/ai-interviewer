# 第 2 章：搭骨架与配置

这一章从空目录开始，建立所有后续章节都依赖的底座：依赖、环境变量、配置对象、模式常量。

---

## 2.1 创建项目与虚拟环境

```bash
mkdir ai-interviewer
cd ai-interviewer
python -m venv .venv
```

激活：

```bash
# Windows (Git Bash / PowerShell)
source .venv/Scripts/activate
# 或 CMD
.venv\Scripts\activate.bat
```

```bash
# Linux / WSL / macOS
source .venv/bin/activate
```

为什么要虚拟环境：这个项目同时依赖 `livekit-agents`、`langgraph`、`langchain-openai`，它们的版本组合比较敏感；全局安装很容易和别的项目打架。

---

## 2.2 依赖清单

`requirements.txt`：

```ini
livekit==1.1.5
livekit-api==1.1.0
livekit-agents==1.5.2
livekit-plugins-openai==1.5.2
livekit-plugins-silero==1.5.2
openai==3.36.1
python-dotenv>=1.0.0
langgraph>=1.2.0
langchain-openai>=0.3.0
langchain-core>=0.3.0
pytest>=8.0.0
pytest-asyncio>=0.23.0
```

安装：

```bash
pip install -r requirements.txt
```

每个包的角色：

| 包 | 作用 | 不用它会怎样 |
| --- | --- | --- |
| `livekit` | LiveKit Python SDK（房间、轨道） | 无法连接房间 |
| `livekit-api` | 服务端 API，签发 AccessToken | 无法签发 JWT |
| `livekit-agents` | AgentSession、VAD/STT/LLM/TTS 抽象、worker CLI | 要自己写音频循环 |
| `livekit-plugins-openai` | OpenAI 兼容的 STT/TTS 适配 | 要自己实现接口 |
| `livekit-plugins-silero` | Silero VAD | 要自己判断说话起止 |
| `openai` | 被插件使用的 HTTP 客户端 | 插件导入失败 |
| `langgraph` | 状态图、checkpointer、流式 | 要自己写状态机 |
| `langchain-openai` | `ChatOpenAI`，调用兼容接口 | 要自己写 LLM 客户端 |
| `pytest` / `pytest-asyncio` | 离线测试 | 无法验证多轮流程 |

> 版本说明：`livekit-agents` 与插件的小版本需要匹配。升级时一起升，别只升一个。

---

## 2.3 环境变量与 `.env`

复制模板：

```bash
cp .env.example .env
```

最小可跑配置：

```ini
LIVEKIT_URL=ws://127.0.0.1:7880
LIVEKIT_API_KEY=devkey
LIVEKIT_API_SECRET=secret

LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.siliconflow.cn/v1
LLM_MODEL=Qwen/Qwen2.5-7B-Instruct

SILICONFLOW_API_KEY=sk-...
SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
STT_MODEL=FunAudioLLM/SenseVoiceSmall
STT_LANGUAGE=zh
TTS_MODEL=FunAudioLLM/CosyVoice2-0.5B
TTS_VOICE=FunAudioLLM/CosyVoice2-0.5B:alex
TTS_RESPONSE_FORMAT=mp3
```

注意：

- `LLM_API_KEY` 与 `SILICONFLOW_API_KEY` 可以是同一个 Key；如果只填后者，`config.py` 会把它当作 LLM Key 的回退。
- `LIVEKIT_API_KEY/SECRET` 默认 `devkey/secret` 只配 `livekit-server --dev` 使用。生产必须换强随机值。
- `.env` 不要提交到 Git。

---

## 2.4 `config.py`：把所有配置收进一个对象

完整文件不长，但设计点不少：

```python
import os
from dataclasses import dataclass
from dotenv import load_dotenv

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
    livekit_url: str = _env("LIVEKIT_URL", "ws://127.0.0.1:7880")
    ...
    llm_timeout_seconds: float = _env_float("LLM_TIMEOUT_SECONDS", 30.0)
    llm_max_retries: int = _env_int("LLM_MAX_RETRIES", 2)
    ...


config = InterviewConfig()
```

### 为什么用 `dataclass(frozen=True)`

- **只读**：配置在进程启动后不应该被随意改，避免某个节点偷偷改温度/模型。
- **可打印**：`repr(config)` 一眼看到全部设置（注意别把 Key 打进日志）。
- **无框架依赖**：不需要 pydantic；这个项目规模用标准库足够。

### 为什么默认值在类定义时求值

```python
class InterviewConfig:
    livekit_url: str = _env("LIVEKIT_URL", ...)
```

`_env(...)` 在**类定义时**执行一次，`config = InterviewConfig()` 时用的是那一批值。这意味着：

- 进程启动后改环境变量不会生效；
- 测试里 `monkeypatch.setenv` 之后需要重新构造 `InterviewConfig()`，而不是改 `config`。

对生产是好事（配置稳定），对测试要注意。

### 配置分组

| 组 | 字段 | 说明 |
| --- | --- | --- |
| LiveKit | `livekit_url/api_key/api_secret` | 房间连接与 token 签名 |
| LLM | `llm_api_key/base_url/model/timeout/max_retries` | 出题、判分、报告 |
| STT/TTS | `siliconflow_*`、`stt_*`、`tts_*` | 语音链路 |
| 提示词 | `agent_instructions`、`greeting_text` | 人设与开场白 |
| 流程 | `min_self_intro_chars`、各模式题量/追问/重试 | 状态机预算 |

> `greeting_text` 是**直接说出来的台词**，不是给模型的指令。早期版本把它叫 `opening_prompt`，并把“请先向考生打招呼”送进 LLM，再在适配器里用字符串匹配拦截；改一个字就坏。现在开场由 `session.say()` 直接说，配置也改成台词语义。

---

## 2.5 `interview_modes.py`：模式的唯一事实来源

```python
MODE_NAMES: tuple[str, ...] = ("quick", "real")
DEFAULT_MODE = "quick"

MODE_LABELS: dict[str, str] = {
    "quick": "简单模式",
    "real": "实战模式",
}


def normalize_mode(value: str | None) -> str:
    candidate = (value or "").strip().lower()
    return candidate if candidate in MODE_NAMES else DEFAULT_MODE


def is_valid_mode(value: str | None) -> bool:
    return (value or "").strip().lower() in MODE_NAMES
```

为什么单独一个文件，而不是放在 `config.py` 或 `server.py`：

- `server.py` 只想校验 `mode=quick|real`，不应该为了一个字符串导入 LangChain/LiveKit；
- `interview_graph.py` 需要模式名和默认值，但不应该导入 HTTP 服务；
- 两边各自写一份，迟早出现“token 接受 `turbo`、图不认识 `turbo`”的漂移。

这个文件**零第三方依赖**，谁都可以安全导入。

### `normalize_mode` vs `is_valid_mode`

- `normalize_mode("REAL")` → `"real"`；未知值 → `"quick"`。用于“坏输入也要继续跑”。
- `is_valid_mode("turbo")` → `False`。用于“明确拒绝”。

HTTP 端点对显式传入的非法 mode 返回 400；环境变量里的非法 mode 则静默回退到 quick。这是有意的：环境配置错误不应让服务起不来，但客户端参数错误应该被告知。

---

## 2.6 配置检查命令

启动前先确认 Key 是否读到（不要打印完整 Key）：

```bash
.venv/Scripts/python.exe -c "from config import config; print(config.llm_base_url, config.llm_model, bool(config.llm_api_key), bool(config.siliconflow_api_key))"
```

期望输出类似：

```text
https://api.siliconflow.cn/v1 Qwen/Qwen2.5-7B-Instruct True True
```

如果两个 `False`，检查 `.env` 是否在项目根目录、是否复制成了 `.env`（不是 `.env.example`）。

---

## 2.7 常见坑

| 现象 | 原因 | 解决 |
| --- | --- | --- |
| `ModuleNotFoundError: livekit` | 用了系统 Python | 用 `.venv/Scripts/python.exe -m pytest`，或先激活虚拟环境 |
| 配置改了不生效 | 进程没重启，或 `.env` 不在 cwd | 重启；确认 `load_dotenv()` 找到文件 |
| `LLM_API_KEY` 为空但请求仍发出 | 代码用了 `or "sk-dummy"` 兜底 | 这是为了本地导入/测试不崩；真实调用会 401，请补 Key |
| Windows 控制台中文乱码 | 终端编码不是 UTF-8 | 本项目日志已用 `backslashreplace`；仍建议 `chcp 65001` |
| 端口 8080 被占用 | 另一个服务在跑 | `FRONTEND_PORT=8090 python server.py` |

---

## 2.8 检查点

1. 不看文件，写出 `config.py` 里 `_env_int` 的作用。
2. 为什么 `interview_modes.py` 不能导入 `config.py`？
3. 如果想把 `real` 模式每阶段题量改成 4，应该改哪一层？会不会影响 `quick`？
4. `normalize_mode(None)` 返回什么？为什么服务端对非法 `mode` 参数却返回 400？

下一章：写一个能签发 token、能安全提供静态文件的小 HTTP 服务。

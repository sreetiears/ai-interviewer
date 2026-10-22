# AI Interviewer

This project runs a LiveKit voice interviewer backed by:

- Dify chat API: `https://api.dify.ai/v1`
- Dify app key: `app-HUkTRAEEjj9GKTC1r7xk7YJf`
- SiliconFlow STT/TTS

## What `agent.py` does

- Connects to a LiveKit room
- Waits for a participant
- Transcribes speech with SiliconFlow STT
- Sends the latest user text to Dify `/chat-messages`
- Streams Dify text back to the console
- Speaks the reply with SiliconFlow TTS

## Important startup note

`python3 agent.py dev` only starts a LiveKit worker.
It does not force the interviewer to enter a room by itself.

For local testing where you want the interviewer to join `ai-room` directly, use:

```bash
python3 agent.py connect --room ai-room
```

## WSL setup

Run everything inside WSL.

If the Python environment is broken, rebuild it:

```bash
bash rebuild_venv.sh
```

If `livekit-server` or `livekit-cli` is missing, install them:

```bash
bash install_livekit_tools.sh
```

## Local run flow

### Terminal 1

```bash
livekit-server --dev
```

### Terminal 2

```bash
cd ~/ai-interviewer
source venv/bin/activate

export LIVEKIT_URL="http://localhost:7880"
export LIVEKIT_API_KEY="devkey"
export LIVEKIT_API_SECRET="secret"

python3 agent.py connect --room ai-room
```

If you intentionally want worker mode only, you can still run:

```bash
python3 agent.py dev
```

But in that case the agent will wait until LiveKit dispatches a job to it.

### Terminal 3

```bash
livekit-cli create-token \
  --api-key devkey \
  --api-secret secret \
  --join \
  --room ai-room \
  --identity candidate-1 \
  --valid-for 24h
```

### Browser

Open [meet.livekit.io](https://meet.livekit.io/), choose custom, then paste:

- server URL
- token

Use the same room name: `ai-room`.

## Default model settings

- `DIFY_API_KEY=app-HUkTRAEEjj9GKTC1r7xk7YJf`
- `DIFY_BASE_URL=https://api.dify.ai/v1`
- `STT_MODEL=FunAudioLLM/SenseVoiceSmall`
- `TTS_MODEL=FunAudioLLM/CosyVoice2-0.5B`
- `TTS_VOICE=FunAudioLLM/CosyVoice2-0.5B:alex`

Optional overrides:

```bash
export OPENING_PROMPT="请先向考生打招呼，并邀请对方开始自我介绍。"
export AGENT_INSTRUCTIONS="你是一名中文复试 AI 面试官。语气自然、专业、友好。每次只问一个问题。"
export TTS_MODEL="FunAudioLLM/CosyVoice2-0.5B"
export TTS_VOICE="FunAudioLLM/CosyVoice2-0.5B:alex"
```

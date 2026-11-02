# 第 3 章：Token 服务与静态文件服务

`server.py` 看起来只是一个“发 token 的小脚本”，但它承担了三个边界职责：

1. 把 `frontend/` 安全地提供给浏览器；
2. 用 LiveKit 密钥签发**带面试模式和会话 id**的 JWT；
3. 在错误输入、路径穿越、端口占用、Windows 编码这些地方不崩。

这一章逐段拆。

---

## 3.1 它不做什么

先划清边界，避免把职责混进来：

- **不处理音频**。音频走 LiveKit，不经过这个 HTTP 服务。
- **不调用 LLM**。LLM 在 agent 进程里。
- **不做用户登录**。当前 `/api/token` 匿名签发，只适合本机/内网演示；公网部署前要加鉴权、限流、房间白名单。
- **不持久化成绩单**。成绩单只在会话内通过 data channel 送达浏览器。

这也是它能保持几百行、无框架的原因。

---

## 3.2 启动路径

```text
python server.py
  └─ main()
       └─ configure_logging()
       └─ run_server()
            ├─ create_server(host, port)
            │    ├─ _port_is_available()
            │    └─ InterviewHTTPServer((host, port), InterviewHttpHandler)
            └─ httpd.serve_forever()
```

### `configure_logging()`

```python
def configure_logging(level=None):
    ...
    handler = logging.StreamHandler(_make_stream_encoding_safe(sys.stdout))
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
```

两个细节：

- **只配置一次**：`configure_logging._configured` 防重复，测试或嵌入时不重复加 handler。
- **编码安全**：`_make_stream_encoding_safe` 对 `sys.stdout` 调 `reconfigure(errors="backslashreplace")`。中文 Windows 控制台是 cp936，遇到 emoji 或生僻字符会抛 `UnicodeEncodeError`；访问日志里又可能包含客户端传来的任意路径，所以必须让流本身容错。

### `_port_is_available(host, port)`

Windows 上 `SO_REUSEADDR` 允许绑定一个已被占用的端口，导致第二个 `server.py` “看起来启动成功”但实际在抢端口。这里用一个短命探针 socket：

```python
if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
elif os.name != "nt":
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
probe.bind(sockaddr)
```

- Windows：独占绑定，能真正发现占用。
- POSIX：容忍 `TIME_WAIT` 残留，和真实服务器的行为一致。
- `port == 0`：交给系统分配，直接返回 True。

`_log_bind_failure` 会把 `EADDRINUSE` / WinError 10048 翻译成“端口被占用，用 netstat 查或改 FRONTEND_PORT”。

---

## 3.3 静态文件服务

### 路由

```python
def _handle_get(self):
    ...
    path = parsed.path or "/"
    if path == "/api/health":
        self._handle_health()
    elif path == "/api/token":
        self._handle_token(parsed)
    else:
        self._handle_static(path)
```

只有 GET。其他方法通过 `__getattr__` 统一回 405：

```python
def __getattr__(self, name):
    if name.startswith("do_"):
        return self._method_not_allowed
    raise AttributeError(name)
```

`BaseHTTPRequestHandler` 在处理请求时会 `getattr(self, "do_POST")`，找不到就 501。这里把它变成明确的 405 + `Allow: GET`。

### 路径解析：先解码，再规范化，再比较 realpath

```python
def _resolve_static_path(request_path):
    decoded = unquote(request_path or "/")
    if "\x00" in decoded or len(decoded) > MAX_PATH_LENGTH:
        return None

    relative = decoded.lstrip("/") or "index.html"
    relative = relative.replace("\\", "/")
    candidate = os.path.normpath(os.path.join(FRONTEND_DIR, relative.replace("/", os.sep)))

    root = os.path.realpath(FRONTEND_DIR)
    real = os.path.realpath(candidate)
    if not _is_within(root, real):
        return None
    return real
```

为什么不能只写 `startswith`：

- `%2e%2e%2f` 解码后是 `../`；
- Windows 的 `..\` 也要拦；
- 兄弟目录 `frontend_evil/` 以 `frontend` 开头，`startswith` 会误放行。

`_is_within` 用 `os.path.commonpath([root, target])` 判断，能处理“不同盘符/混合绝对相对路径”的 `ValueError`，并用 `normcase` 兼容 Windows 大小写。

### MIME 与缓存

```python
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": ..., ".js": ...}
DEFAULT_CONTENT_TYPE = "application/octet-stream"
```

缓存策略：

| 文件 | Cache-Control | 原因 |
| --- | --- | --- |
| `.html` | `no-store` | HTML 引用最新资源，不能拿旧的 |
| `.css` / `.js` | `public, max-age=300` | 5 分钟，开发友好 |
| 其他 | `no-cache` | 允许缓存但每次校验 |

> 生产环境如果给 JS/CSS 文件名加内容哈希，可以把 `max-age` 拉长到一年。

### 响应写法

```python
def _respond(self, status, body=b"", content_type="text/plain; charset=utf-8",
             cache_control="no-store", extra_headers=None):
    ...
    self.send_response(status)
    self.send_header("Content-Type", content_type)
    self.send_header("Content-Length", str(len(body)))
    if cache_control:
        self.send_header("Cache-Control", cache_control)
    ...
    if self.command != "HEAD" and body:
        self.wfile.write(body)
```

- 始终显式 `Content-Length`，HTTP/1.1 才能正确复用连接。
- 客户端提前断开时捕获 `BrokenPipeError` / `ConnectionResetError`。
- `_response_started` 防止重复发响应（例如异常处理里再写一次）。

### 安全响应头

每个响应都加：

```python
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
X-Frame-Options: DENY
```

CORS 默认**不发**任何头：同源部署不需要。只有 `ALLOWED_ORIGIN` 显式配置且请求 Origin 命中时，才发 `Access-Control-Allow-Origin` 和 `Vary: Origin`。这样 `/api/token` 不会被任意网站读取。

---

## 3.4 Token 端点

### 请求

```http
GET /api/token?room=ai-room&identity=candidate-1&mode=real
```

参数：

| 参数 | 必填 | 规则 | 默认 |
| --- | --- | --- | --- |
| `room` | 否 | `[A-Za-z0-9_.@-]{1,64}` | `ai-room` |
| `identity` | 否 | 同上 | `candidate-<4 hex>` |
| `mode` | 否 | `quick` / `real` | 环境变量或 `quick` |

约束：

- query 长度 ≤ 2048；
- 字段数 ≤ 20；
- 非法 mode → 400，并返回允许值；
- 非法 room/identity → 400。

### 签发

```python
def create_token(room_name, identity, mode=DEFAULT_INTERVIEW_MODE,
                 session_id=None, ttl_seconds=None):
    resolved_session = session_id or uuid.uuid4().hex
    metadata = json.dumps({
        "interview_mode": mode,
        "session_id": resolved_session,
    }, ensure_ascii=False)
    ttl = TOKEN_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
    return (
        AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_grants(VideoGrants(room_join=True, room=room_name))
        .with_metadata(metadata)
        .with_ttl(datetime.timedelta(seconds=ttl))
        .to_jwt()
    )
```

三个关键字段：

1. **`roomJoin` grant**：允许加入指定房间。不要给 `roomAdmin` 或 `roomCreate`。
2. **`metadata`**：agent 从 `participant.metadata` 读它。放两样东西：
   - `interview_mode`：选择 quick/real；
   - `session_id`：给 LangGraph 做 `thread_id`，保证每场面试独立。
3. **`ttl`**：默认 3600 秒。浏览器每次进入考场都会重新取 token，没有理由发 6 小时。

`session_id` 必须由**服务端**生成，不能信客户端传的值；否则两个考生可以故意共用 id，互相污染 checkpoint。

### 响应

```json
{
  "serverUrl": "ws://127.0.0.1:7880",
  "roomName": "ai-room",
  "identity": "candidate-1",
  "token": "eyJ...",
  "interviewMode": "real"
}
```

前端用这四个字段连接：

```js
const connection = await (await fetch('/api/token?room=ai-room&mode=real')).json();
await room.connect(connection.serverUrl, connection.token);
```

`interviewMode` 是服务端校准后的值：如果环境变量把默认设成 real，而页面本地以为 quick，前端会以服务端为准并提示。

### `/api/health`

```json
{"status": "ok"}
```

给探针/反代用。它不检查 LiveKit 或 LLM 是否可达，只表示 HTTP 进程活着。

---

## 3.5 线程模型

```python
class InterviewHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32
```

每个请求一个线程。为什么不用单线程 `HTTPServer`：一个慢客户端会阻塞 token 端点，进而让所有进入考场的人卡住。`daemon_threads=True` 保证退出时不被工作线程拖住。

请求处理是纯 I/O（读文件、签发 JWT），没有共享可变状态，所以不需要锁。

---

## 3.6 手动验证

启动：

```bash
python server.py
```

另开终端：

```bash
curl -i http://localhost:8080/api/health
```

```bash
curl -s "http://localhost:8080/api/token?room=ai-room&mode=real" | python -m json.tool
```

```bash
curl -i "http://localhost:8080/api/token?mode=turbo"
```

期望：400，`error=invalid mode`。

```bash
curl -i "http://localhost:8080/%2e%2e%2fREADME.md"
```

期望：403，且响应里没有 README 内容。

```bash
curl -i "http://localhost:8080/nope.css"
```

期望：404。

解码 token（不验证签名，只看 claims）：

```bash
python - <<'PY'
import json, jwt
token = json.load(open("token.json"))["token"]
claims = jwt.decode(token, options={"verify_signature": False})
print(claims)
print(json.loads(claims["metadata"]))
print("ttl:", claims["exp"] - claims["nbf"])
PY
```

---

## 3.7 函数索引

| 函数 | 作用 | 关键点 |
| --- | --- | --- |
| `_env_int` | 读整数环境变量 | 非法值回退默认 |
| `_parse_allowed_origins` | 解析 CORS 白名单 | 逗号分隔、去尾斜杠 |
| `_make_stream_encoding_safe` | 让 stdout 不因编码崩 | `backslashreplace` |
| `configure_logging` | 配置 logger | 只配置一次 |
| `create_token` | 签发 JWT | metadata + TTL + grants |
| `_is_valid_identifier` | 校验 room/identity | 窄字符集 |
| `_normalize_interview_mode` | 空值返回 None | 与 `normalize_mode` 分工 |
| `_default_interview_mode` | 环境默认值 | 非法回退 quick |
| `_is_within` | 判断路径包含 | `commonpath` + `normcase` |
| `_resolve_static_path` | URL → 安全文件路径 | 解码、规范化、realpath |
| `_cache_control_for` | 缓存策略 | html no-store |
| `_content_type_for` | MIME | 扩展名映射 |
| `InterviewHttpHandler._respond` | 统一响应 | Content-Length、异常兜底 |
| `_handle_token` | token 端点 | 参数校验、模式校验 |
| `_handle_static` | 静态文件 | 404/403 |
| `_match_allowed_origin` | CORS 命中 | 默认不发头 |
| `_port_is_available` | 端口探针 | Windows 独占 |
| `create_server` | 构造服务器 | 先探测再绑定 |
| `run_server` | 阻塞运行 | 友好错误、优雅退出 |

---

## 3.8 检查点

1. 为什么 `_resolve_static_path` 要同时做 `unquote`、`normpath`、`realpath`？
2. 为什么 `session_id` 不能由客户端提供？
3. 如果把 token TTL 改成 10 分钟，前端需要在什么时候重新取 token？
4. 为什么 CORS 默认完全不发头，而不是发 `*`？
5. `__getattr__` 里的 `do_` 分支解决了什么框架默认行为？

下一章：面试状态机。

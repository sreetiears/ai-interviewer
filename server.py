"""自定义面试前端的 HTTP 服务。

从 ``frontend/`` 提供静态前端，并为浏览器签发 LiveKit 访问令牌
（``GET /api/token``）。同时暴露 ``GET /api/health`` 供存活探测。

运行方式::

    python server.py

环境变量：
    FRONTEND_HOST   绑定地址（默认 ``0.0.0.0``）
    FRONTEND_PORT   监听端口（默认 ``8080``）
    INTERVIEW_MODE  默认面试模式 ``quick`` | ``real``（默认 ``quick``）
    DELIVERY_METRICS  成绩单是否使用表达特征（默认打开，``0`` 关闭）
    ALLOWED_ORIGIN  可选，逗号分隔的 CORS 来源白名单。未设置时
                    完全不发送跨域响应头（同源使用不需要 CORS）。
    LOG_LEVEL       日志级别（默认 ``INFO``）
"""

from __future__ import annotations

import datetime
import errno
import json
import logging
import os
import re
import socket
import sys
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from dotenv import load_dotenv
from livekit.api import AccessToken, VideoGrants

from config import config as app_config, health_payload
from interview_modes import DEFAULT_MODE, MODE_NAMES, is_valid_mode, normalize_mode
from learner_store import get_store, parse_focus_topics

load_dotenv()

LIVEKIT_API_KEY = app_config.livekit_api_key
LIVEKIT_API_SECRET = app_config.livekit_api_secret
LIVEKIT_URL = app_config.livekit_url


def _env_int(name: str, default: int) -> int:
    """读取整数环境变量，失败时回退到 *default*。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


HOST = os.getenv("FRONTEND_HOST", "0.0.0.0")
PORT = _env_int("FRONTEND_PORT", 8080)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")

# --- 面试模式 ----------------------------------------------------------------
# quick：短演示面试；real：完整练习面试。选中的模式写进令牌元数据，
# 智能体据此选择对应的面试图。模式名放在 ``interview_modes.py``，
# 这样图和本服务不会对“有哪些模式”产生分歧。
INTERVIEW_MODES = MODE_NAMES  # 向后兼容的别名
DEFAULT_INTERVIEW_MODE = DEFAULT_MODE
INTERVIEW_MODE_ENV = "INTERVIEW_MODE"

# 访问令牌是短时的：浏览器每次进入都会重新领取，
# 没有必要发放 LiveKit 默认的六小时有效期。
TOKEN_TTL_SECONDS = _env_int("TOKEN_TTL_SECONDS", 3600)

# --- 令牌接口约束 -------------------------------------------------------------
DEFAULT_ROOM = "ai-room"
MAX_IDENTIFIER_LENGTH = 64
# 愿意签名的 LiveKit 房间名 / 身份。故意收窄：
# 这个值会进入已签名的 JWT，也会出现在服务日志里。
IDENTIFIER_RE = re.compile(
    r"^[A-Za-z0-9_.@-]{1,%d}$" % MAX_IDENTIFIER_LENGTH
)
MAX_QUERY_LENGTH = 2048
MAX_QUERY_FIELDS = 20

# --- 静态文件 ----------------------------------------------------------------
MAX_PATH_LENGTH = 2048
STATIC_CACHE_SECONDS = 300
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".webmanifest": "application/manifest+json",
}
DEFAULT_CONTENT_TYPE = "application/octet-stream"


def _parse_allowed_origins(raw: str) -> tuple[str, ...]:
    """把 ``ALLOWED_ORIGIN``（逗号分隔）解析成来源元组。"""
    return tuple(part.strip().rstrip("/") for part in raw.split(",") if part.strip())


ALLOWED_ORIGINS = _parse_allowed_origins(os.getenv("ALLOWED_ORIGIN", ""))


logger = logging.getLogger("interview.server")


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def _make_stream_encoding_safe(stream):
    """返回配置好的 *stream*，遇到无法编码的字符也不抛异常。

    在 Windows 上，cp936（GBK）控制台遇到表情等字符会抛出
    ``UnicodeEncodeError``。日志记录本身只有 ASCII，但访问日志行可能包含
    客户端任意提供的路径，因此让流本身能够容忍。
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            pass
    return stream


def configure_logging(level: str | int | None = None) -> logging.Logger:
    """只配置一次模块日志器（编码安全的标准输出处理器）。"""
    if getattr(configure_logging, "_configured", False):
        return logger

    raw_level = LOG_LEVEL if level is None else level
    if isinstance(raw_level, str):
        resolved = getattr(logging, raw_level.strip().upper(), None)
        level_value = resolved if isinstance(resolved, int) else logging.INFO
    else:
        try:
            level_value = int(raw_level)
        except (TypeError, ValueError):
            level_value = logging.INFO

    logger.setLevel(level_value)
    # 被嵌入时（例如测试或其他入口）避免日志打两遍。
    logger.propagate = False

    handler = logging.StreamHandler(_make_stream_encoding_safe(sys.stdout))
    handler.setLevel(level_value)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(handler)

    configure_logging._configured = True
    return logger


# ---------------------------------------------------------------------------
# 令牌辅助函数
# ---------------------------------------------------------------------------
def create_token(
    room_name: str,
    identity: str,
    mode: str = DEFAULT_INTERVIEW_MODE,
    session_id: str | None = None,
    ttl_seconds: int | None = None,
    focus_topics: list[str] | None = None,
) -> str:
    """创建携带面试模式、会话 id 与考生档案键的 LiveKit JWT。

    ``mode``、``session_id``、``learner_id`` 和可选 ``focus_topics`` 以 JSON
    元数据存放，智能体可从 ``participant.metadata`` 读到。``learner_id``
    与 LiveKit identity 对齐（前端用本机持久化的考生档案 id 进房）。
    即使同一身份重新加入同一房间，会话 id 也让每场面试拥有自己的
    LangGraph 检查点。两参数调用形式仍可供旧调用方使用。
    """
    resolved_session = session_id or uuid.uuid4().hex
    payload: dict = {
        "interview_mode": mode,
        "session_id": resolved_session,
        "learner_id": identity,
    }
    focuses = [t for t in (focus_topics or []) if t]
    if focuses:
        payload["focus_topics"] = focuses
    metadata = json.dumps(payload, ensure_ascii=False)
    ttl = TOKEN_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
    token = (
        AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_grants(VideoGrants(room_join=True, room=room_name))
        .with_metadata(metadata)
        .with_ttl(datetime.timedelta(seconds=ttl))
        .to_jwt()
    )
    return token


def _is_valid_identifier(value: str) -> bool:
    return bool(IDENTIFIER_RE.fullmatch(value))


def _normalize_interview_mode(raw: str | None) -> str | None:
    """规范化模式值；缺失或空白时返回 ``None``。"""
    if raw is None:
        return None
    mode = raw.strip().lower()
    return mode or None


def _default_interview_mode() -> str:
    """返回合法的 ``INTERVIEW_MODE`` 值，否则返回默认模式。"""
    return normalize_mode(os.getenv(INTERVIEW_MODE_ENV))


# ---------------------------------------------------------------------------
# 静态路径解析
# ---------------------------------------------------------------------------
def _is_within(root: str, target: str) -> bool:
    """当 *target* 就是 *root* 或位于其内部时返回 True（防止前缀欺骗）。"""
    try:
        common = os.path.commonpath([root, target])
    except ValueError:  # 不同盘符，或绝对路径与相对路径混用
        return False
    return os.path.normcase(common) == os.path.normcase(root)


def _resolve_static_path(request_path: str) -> str | None:
    """把 URL 路径映射到 ``FRONTEND_DIR`` 内的绝对文件路径。

    请求试图逃出前端目录，或路径本身非法时，返回 ``None``。先解码百分号编码，
    这样 ``%2e%2e%2f`` 就不能把 ``../`` 混过检查。
    """
    decoded = unquote(request_path or "/")
    if "\x00" in decoded or len(decoded) > MAX_PATH_LENGTH:
        return None

    relative = decoded.lstrip("/") or "index.html"
    # 在所有平台上都把反斜杠当作分隔符，这样 Windows 风格的
    # 穿越（``..\\``）会被一致拒绝。
    relative = relative.replace("\\", "/")
    candidate = os.path.normpath(os.path.join(FRONTEND_DIR, relative.replace("/", os.sep)))

    root = os.path.realpath(FRONTEND_DIR)
    real = os.path.realpath(candidate)
    if not _is_within(root, real):
        return None
    return real


def _cache_control_for(path: str) -> str:
    name = os.path.basename(path).lower()
    extension = os.path.splitext(name)[1]
    if name.endswith(".html"):
        # HTML 绝不能用旧缓存：它引用的是已更新的资源。
        return "no-store"
    if extension in (".css", ".js", ".mjs"):
        return "public, max-age=%d" % STATIC_CACHE_SECONDS
    return "no-cache"


def _content_type_for(path: str) -> str:
    return CONTENT_TYPES.get(os.path.splitext(path)[1].lower(), DEFAULT_CONTENT_TYPE)


# ---------------------------------------------------------------------------
# HTTP 处理器
# ---------------------------------------------------------------------------
class InterviewHttpHandler(BaseHTTPRequestHandler):
    server_version = "InterviewFrontend/1.0"
    protocol_version = "HTTP/1.1"

    # -- 响应管线 -------------------------------------------------------------
    def end_headers(self):
        # 基线加固响应头，应用到每一次响应。
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")

        # 同源部署不需要 CORS。只有显式加入白名单才开启，
        # 避免任意网站读取 /api/token。
        origin = self.headers.get("Origin") if self.headers is not None else None
        allowed = _match_allowed_origin(origin)
        if allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Vary", "Origin")

        super().end_headers()

    def _respond(
        self,
        status,
        body: bytes | str = b"",
        content_type: str = "text/plain; charset=utf-8",
        cache_control: str | None = "no-store",
        extra_headers: dict | None = None,
    ) -> None:
        status = int(status)
        if isinstance(body, str):
            body = body.encode("utf-8")
        if getattr(self, "_response_started", False):
            # 已经来不及发送格式正确的响应；断开连接。
            self.close_connection = True
            return
        self._response_started = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if cache_control:
                self.send_header("Cache-Control", cache_control)
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _respond_json(self, status, payload: dict, cache_control: str = "no-store") -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._respond(status, body, "application/json; charset=utf-8", cache_control)

    def _respond_status(self, status) -> None:
        phrase = HTTPStatus(int(status)).phrase
        self._respond(int(status), "%d %s" % (int(status), phrase), cache_control="no-store")

    # -- 日志 -----------------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: A003 - 标准库方法签名
        try:
            message = fmt % args
        except (TypeError, ValueError):
            message = str(fmt)
        # 避免客户端提供的请求行伪造日志条目。
        message = message.replace("\r", "\\r").replace("\n", "\\n")
        logger.info("%s - %s", self.address_string(), message)

    # -- 路由 -----------------------------------------------------------------
    def do_GET(self):
        try:
            self._handle_get()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:  # 防御性兜底：始终给客户端一个响应
            logger.exception("Unhandled error while serving %s", self.path)
            self._respond_status(HTTPStatus.INTERNAL_SERVER_ERROR)

    def __getattr__(self, name):
        # GET 以外的方法（POST/PUT/DELETE/OPTIONS/...）一律返回 405。
        if name.startswith("do_"):
            return self._method_not_allowed
        raise AttributeError(name)

    def _method_not_allowed(self):
        # 我们不读取请求体，因此不复用这条连接。
        self.close_connection = True
        self._respond(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "405 Method Not Allowed",
            cache_control="no-store",
            extra_headers={"Allow": "GET"},
        )

    def _handle_get(self) -> None:
        if self.headers.get("Content-Length") or self.headers.get("Transfer-Encoding"):
            # GET 请求不会带上我们要读的正文；直接关闭，
            # 避免未读字节被当成下一次请求解析。
            self.close_connection = True
        try:
            parsed = urlparse(self.path)
        except ValueError:
            self._respond_json(HTTPStatus.BAD_REQUEST, {"error": "invalid request target"})
            return

        path = parsed.path or "/"
        if path == "/api/health":
            self._handle_health()
        elif path == "/api/token":
            self._handle_token(parsed)
        elif path == "/api/learners/me/profile":
            self._handle_learner_profile(parsed)
        elif path == "/api/learners/me/sessions":
            self._handle_learner_sessions(parsed)
        else:
            self._handle_static(path)

    # -- 接口 -----------------------------------------------------------------
    def _handle_health(self) -> None:
        self._respond_json(
            HTTPStatus.OK,
            health_payload(default_mode=_default_interview_mode()),
        )

    def _handle_token(self, parsed) -> None:
        if len(parsed.query) > MAX_QUERY_LENGTH:
            self._respond_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "query string too long", "limit": MAX_QUERY_LENGTH},
            )
            return
        try:
            params = parse_qs(
                parsed.query,
                keep_blank_values=True,
                max_num_fields=MAX_QUERY_FIELDS,
            )
        except ValueError:
            self._respond_json(HTTPStatus.BAD_REQUEST, {"error": "invalid query string"})
            return

        room_name = (params.get("room", [""])[0] or "").strip() or DEFAULT_ROOM
        identity = (params.get("identity", [""])[0] or "").strip()
        if not identity:
            identity = "candidate-%s" % uuid.uuid4().hex[:4]

        requested_mode = _normalize_interview_mode(params.get("mode", [""])[0])
        mode = requested_mode or _default_interview_mode()
        if not is_valid_mode(mode):
            self._respond_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "invalid mode",
                    "detail": "mode must be one of: %s" % ", ".join(MODE_NAMES),
                    "allowed": list(MODE_NAMES),
                },
            )
            return

        if not _is_valid_identifier(room_name):
            self._respond_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "invalid room",
                    "detail": "room must match %s" % IDENTIFIER_RE.pattern,
                },
            )
            return
        if not _is_valid_identifier(identity):
            self._respond_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "invalid identity",
                    "detail": "identity must match %s" % IDENTIFIER_RE.pattern,
                },
            )
            return

        focus_raw = (params.get("focus", [""])[0] or "").strip()
        focus_topics = parse_focus_topics(focus_raw)
        try:
            get_store().ensure_learner(identity)
        except Exception:
            logger.warning("Failed to ensure learner %s", identity, exc_info=True)

        jwt_token = create_token(
            room_name,
            identity,
            mode,
            focus_topics=focus_topics or None,
        )
        self._respond_json(
            HTTPStatus.OK,
            {
                "serverUrl": LIVEKIT_URL,
                "roomName": room_name,
                "identity": identity,
                "learnerId": identity,
                "token": jwt_token,
                "interviewMode": mode,
                "focusTopics": focus_topics,
            },
        )

    def _parse_learner_query(self, parsed) -> tuple[str | None, dict | None]:
        """解析 learner_id；失败时返回 (None, 已写出的错误响应 payload 无需再写)。

        第二个返回值非空表示已响应该错误。
        """
        if len(parsed.query) > MAX_QUERY_LENGTH:
            self._respond_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "query string too long", "limit": MAX_QUERY_LENGTH},
            )
            return None, {"written": True}
        try:
            params = parse_qs(
                parsed.query,
                keep_blank_values=True,
                max_num_fields=MAX_QUERY_FIELDS,
            )
        except ValueError:
            self._respond_json(HTTPStatus.BAD_REQUEST, {"error": "invalid query string"})
            return None, {"written": True}

        learner_id = (params.get("learner_id", [""])[0] or "").strip()
        if not learner_id:
            self._respond_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "missing learner_id"},
            )
            return None, {"written": True}
        if not _is_valid_identifier(learner_id):
            self._respond_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "invalid learner_id",
                    "detail": "learner_id must match %s" % IDENTIFIER_RE.pattern,
                },
            )
            return None, {"written": True}
        return learner_id, None

    def _handle_learner_profile(self, parsed) -> None:
        learner_id, err = self._parse_learner_query(parsed)
        if err:
            return
        assert learner_id is not None
        try:
            store = get_store()
            focus_raw = ""
            try:
                params = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=MAX_QUERY_FIELDS)
                focus_raw = (params.get("focus", [""])[0] or "").strip()
            except ValueError:
                focus_raw = ""
            snapshot = store.build_snapshot(
                learner_id,
                focus_topics=parse_focus_topics(focus_raw),
            )
            self._respond_json(HTTPStatus.OK, snapshot.to_dict())
        except Exception:
            logger.warning("Failed to load profile for %s", learner_id, exc_info=True)
            self._respond_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "profile unavailable"},
            )

    def _handle_learner_sessions(self, parsed) -> None:
        learner_id, err = self._parse_learner_query(parsed)
        if err:
            return
        assert learner_id is not None
        limit = 10
        try:
            params = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=MAX_QUERY_FIELDS)
            raw_limit = (params.get("limit", ["10"])[0] or "10").strip()
            limit = max(1, min(50, int(raw_limit)))
        except (ValueError, TypeError):
            limit = 10
        try:
            sessions = get_store().list_sessions(learner_id, limit=limit)
            self._respond_json(
                HTTPStatus.OK,
                {"learnerId": learner_id, "sessions": sessions},
            )
        except Exception:
            logger.warning("Failed to list sessions for %s", learner_id, exc_info=True)
            self._respond_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "sessions unavailable"},
            )

    # -- 静态文件 -------------------------------------------------------------
    def _handle_static(self, request_path: str) -> None:
        target = _resolve_static_path(request_path)
        if target is None:
            self._respond_status(HTTPStatus.FORBIDDEN)
            return
        if not os.path.isfile(target):
            self._respond_status(HTTPStatus.NOT_FOUND)
            return
        try:
            with open(target, "rb") as handle:
                body = handle.read()
        except OSError:
            logger.warning("Could not read static file %s", target)
            self._respond_status(HTTPStatus.NOT_FOUND)
            return

        self._respond(
            HTTPStatus.OK,
            body,
            content_type=_content_type_for(target),
            cache_control=_cache_control_for(target),
        )


def _match_allowed_origin(origin: str | None) -> str | None:
    """*origin* 在白名单中时返回它，否则返回 ``None``（不发送 CORS 头）。"""
    if not origin:
        return None
    origin = origin.strip().rstrip("/")
    if origin in ALLOWED_ORIGINS:
        return origin
    return None


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------
class InterviewHTTPServer(ThreadingHTTPServer):
    """多线程 HTTP 服务，避免慢客户端堵住令牌接口。"""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32


def _port_is_available(host: str, port: int) -> bool:
    """当 *host*:*port* 已被其他进程占用时返回 False。

    在 Windows 上，``SO_REUSEADDR``（由 ``allow_reuse_address`` 开启）也允许
    绑定一个已经在用的端口，于是第二个服务会悄悄共享或抢走端口，而不是报错。
    用带 ``SO_EXCLUSIVEADDRUSE`` 的短命探测套接字可以发现这种情况。
    """
    if port == 0:
        return True
    try:
        addresses = socket.getaddrinfo(
            host, port, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
    except OSError:
        # 把权威错误留给下面真正的 bind。
        return True
    family, sock_type, protocol, _, sockaddr = addresses[0]

    probe = socket.socket(family, sock_type, protocol)
    try:
        try:
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            elif os.name != "nt":
                # 与真正的服务一样，在 POSIX 上容忍残留的 TIME_WAIT。
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass
        probe.bind(sockaddr)
        return True
    except OSError:
        return False
    finally:
        probe.close()


def create_server(host: str = HOST, port: int = PORT) -> InterviewHTTPServer:
    if not _port_is_available(host, port):
        raise OSError(errno.EADDRINUSE, "Address already in use")
    return InterviewHTTPServer((host, port), InterviewHttpHandler)


def _log_bind_failure(host: str, port: int, exc: OSError) -> None:
    if exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048:
        logger.error(
            "Port %s is already in use on %s. Stop the process using it "
            "(e.g. netstat -ano | findstr :%s) or set FRONTEND_PORT to a free port.",
            port,
            host,
            port,
        )
    else:
        logger.error("Could not bind %s:%s (%s).", host, port, exc)


def run_server(port: int | None = None, host: str | None = None) -> InterviewHTTPServer:
    """启动 HTTP 服务，并阻塞到被中断。"""
    configure_logging()
    bind_host = HOST if host is None else host
    bind_port = PORT if port is None else int(port)

    try:
        httpd = create_server(bind_host, bind_port)
    except OSError as exc:
        _log_bind_failure(bind_host, bind_port, exc)
        raise SystemExit(1) from exc

    actual_port = httpd.server_address[1]
    logger.info("Custom Interview Frontend running at http://localhost:%d", actual_port)
    logger.info("Serving static files from %s", FRONTEND_DIR)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received, shutting down ...")
    finally:
        httpd.server_close()
        logger.info("Server stopped.")
    return httpd


def main() -> int:
    configure_logging()
    run_server()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

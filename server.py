"""HTTP server for the custom interview frontend.

Serves the static frontend from ``frontend/`` and issues LiveKit access tokens
for the browser (``GET /api/token``).  Also exposes ``GET /api/health`` for
liveness probes.

Run with::

    python server.py

Environment variables:
    FRONTEND_HOST   bind address (default ``0.0.0.0``)
    FRONTEND_PORT   port to listen on (default ``8080``)
    INTERVIEW_MODE  default interview mode ``quick`` | ``real`` (default ``quick``)
    ALLOWED_ORIGIN  optional comma-separated CORS origin whitelist.  When unset
                    no cross-origin headers are emitted at all (same-origin use
                    does not need CORS).
    LOG_LEVEL       logging level (default ``INFO``)
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

from interview_modes import DEFAULT_MODE, MODE_NAMES, is_valid_mode, normalize_mode

load_dotenv()

LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "devkey")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "secret")
LIVEKIT_URL = os.getenv("LIVEKIT_URL", "ws://127.0.0.1:7880")


def _env_int(name: str, default: int) -> int:
    """Read an integer environment variable, falling back to *default*."""
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

# --- interview modes --------------------------------------------------------------
# quick: short demo interview; real: full practice interview.  The selected mode is
# baked into the token metadata so the agent can pick the matching interview graph.
# The names live in ``interview_modes.py`` so the graph and this server cannot
# disagree about which modes exist.
INTERVIEW_MODES = MODE_NAMES  # backward-compatible alias
DEFAULT_INTERVIEW_MODE = DEFAULT_MODE
INTERVIEW_MODE_ENV = "INTERVIEW_MODE"

# Access tokens are short-lived: the browser fetches a fresh one for every entry,
# so there is no reason to hand out the LiveKit default of six hours.
TOKEN_TTL_SECONDS = _env_int("TOKEN_TTL_SECONDS", 3600)

# --- token endpoint constraints -------------------------------------------------
DEFAULT_ROOM = "ai-room"
MAX_IDENTIFIER_LENGTH = 64
# LiveKit room names / identities we are willing to sign.  Keep this deliberately
# narrow: the value ends up in a signed JWT and in server logs.
IDENTIFIER_RE = re.compile(
    r"^[A-Za-z0-9_.@-]{1,%d}$" % MAX_IDENTIFIER_LENGTH
)
MAX_QUERY_LENGTH = 2048
MAX_QUERY_FIELDS = 20

# --- static file serving --------------------------------------------------------
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
    """Parse ``ALLOWED_ORIGIN`` (comma separated) into a tuple of origins."""
    return tuple(part.strip().rstrip("/") for part in raw.split(",") if part.strip())


ALLOWED_ORIGINS = _parse_allowed_origins(os.getenv("ALLOWED_ORIGIN", ""))


logger = logging.getLogger("interview.server")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _make_stream_encoding_safe(stream):
    """Return *stream* configured so unencodable characters never raise.

    On Windows a cp936 (GBK) console raises ``UnicodeEncodeError`` for characters
    such as emoji.  Log records are ASCII-only, but access log lines can contain
    arbitrary client-supplied paths, so make the stream itself tolerant.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            pass
    return stream


def configure_logging(level: str | int | None = None) -> logging.Logger:
    """Configure the module logger once (UTF-8/encoding-safe stdout handler)."""
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
    # Avoid double logging when embedded (e.g. by tests or another entry point).
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
# Token helpers
# ---------------------------------------------------------------------------
def create_token(
    room_name: str,
    identity: str,
    mode: str = DEFAULT_INTERVIEW_MODE,
    session_id: str | None = None,
    ttl_seconds: int | None = None,
) -> str:
    """Create a LiveKit JWT carrying the interview mode and session id.

    ``mode`` and ``session_id`` are stored as JSON metadata so the agent can read
    them from ``participant.metadata``.  The session id gives every interview its
    own LangGraph checkpoint even when the same identity rejoins the same room.
    The two-argument form keeps working for older callers.
    """
    resolved_session = session_id or uuid.uuid4().hex
    metadata = json.dumps(
        {"interview_mode": mode, "session_id": resolved_session},
        ensure_ascii=False,
    )
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
    """Normalize a mode value; return ``None`` when it is missing/blank."""
    if raw is None:
        return None
    mode = raw.strip().lower()
    return mode or None


def _default_interview_mode() -> str:
    """Return a valid ``INTERVIEW_MODE`` value, else the default mode."""
    return normalize_mode(os.getenv(INTERVIEW_MODE_ENV))


# ---------------------------------------------------------------------------
# Static path resolution
# ---------------------------------------------------------------------------
def _is_within(root: str, target: str) -> bool:
    """True when *target* is *root* or lives inside it (no prefix tricks)."""
    try:
        common = os.path.commonpath([root, target])
    except ValueError:  # different drives / mixed absolute-relative paths
        return False
    return os.path.normcase(common) == os.path.normcase(root)


def _resolve_static_path(request_path: str) -> str | None:
    """Map a URL path to an absolute file path inside ``FRONTEND_DIR``.

    Returns ``None`` when the request tries to escape the frontend directory or
    is otherwise invalid.  Percent-encoding is decoded first so that
    ``%2e%2e%2f`` cannot smuggle ``../`` past the check.
    """
    decoded = unquote(request_path or "/")
    if "\x00" in decoded or len(decoded) > MAX_PATH_LENGTH:
        return None

    relative = decoded.lstrip("/") or "index.html"
    # Treat backslashes as separators on every platform so Windows-style
    # traversal (``..\\``) is rejected consistently.
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
        # HTML must never be stale: it references the hashed/updated assets.
        return "no-store"
    if extension in (".css", ".js", ".mjs"):
        return "public, max-age=%d" % STATIC_CACHE_SECONDS
    return "no-cache"


def _content_type_for(path: str) -> str:
    return CONTENT_TYPES.get(os.path.splitext(path)[1].lower(), DEFAULT_CONTENT_TYPE)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class InterviewHttpHandler(BaseHTTPRequestHandler):
    server_version = "InterviewFrontend/1.0"
    protocol_version = "HTTP/1.1"

    # -- response plumbing ---------------------------------------------------
    def end_headers(self):
        # Baseline hardening headers, applied to every response.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")

        # Same-origin deployments need no CORS.  Only opt in with an explicit
        # whitelist so /api/token is not readable by arbitrary websites.
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
            # Too late to send a well-formed response; drop the connection.
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

    # -- logging -------------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
        try:
            message = fmt % args
        except (TypeError, ValueError):
            message = str(fmt)
        # Keep client-supplied request lines from forging log entries.
        message = message.replace("\r", "\\r").replace("\n", "\\n")
        logger.info("%s - %s", self.address_string(), message)

    # -- routing -------------------------------------------------------------
    def do_GET(self):
        try:
            self._handle_get()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:  # defensive catch-all: always answer the client
            logger.exception("Unhandled error while serving %s", self.path)
            self._respond_status(HTTPStatus.INTERNAL_SERVER_ERROR)

    def __getattr__(self, name):
        # Any method other than GET (POST/PUT/DELETE/OPTIONS/...) -> 405.
        if name.startswith("do_"):
            return self._method_not_allowed
        raise AttributeError(name)

    def _method_not_allowed(self):
        # We do not consume request bodies, so do not reuse the connection.
        self.close_connection = True
        self._respond(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "405 Method Not Allowed",
            cache_control="no-store",
            extra_headers={"Allow": "GET"},
        )

    def _handle_get(self) -> None:
        if self.headers.get("Content-Length") or self.headers.get("Transfer-Encoding"):
            # GET requests never carry a body we read; close instead of letting
            # unread bytes be parsed as a follow-up request.
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
        else:
            self._handle_static(path)

    # -- endpoints -----------------------------------------------------------
    def _handle_health(self) -> None:
        self._respond_json(HTTPStatus.OK, {"status": "ok"})

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

        jwt_token = create_token(room_name, identity, mode)
        self._respond_json(
            HTTPStatus.OK,
            {
                "serverUrl": LIVEKIT_URL,
                "roomName": room_name,
                "identity": identity,
                "token": jwt_token,
                "interviewMode": mode,
            },
        )

    # -- static files --------------------------------------------------------
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
    """Return *origin* when it is whitelisted, else ``None`` (no CORS header)."""
    if not origin:
        return None
    origin = origin.strip().rstrip("/")
    if origin in ALLOWED_ORIGINS:
        return origin
    return None


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
class InterviewHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server so slow clients cannot block the token endpoint."""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32


def _port_is_available(host: str, port: int) -> bool:
    """Return False when *host*:*port* is already bound by another process.

    On Windows ``SO_REUSEADDR`` (which ``allow_reuse_address`` enables) also
    allows binding a port that is already in use, so a plain second server
    would silently share/hijack the port instead of reporting an error.  A
    short-lived probe socket with ``SO_EXCLUSIVEADDRUSE`` detects that case.
    """
    if port == 0:
        return True
    try:
        addresses = socket.getaddrinfo(
            host, port, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
    except OSError:
        # Let the real bind below produce the authoritative error.
        return True
    family, sock_type, protocol, _, sockaddr = addresses[0]

    probe = socket.socket(family, sock_type, protocol)
    try:
        try:
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            elif os.name != "nt":
                # Tolerate TIME_WAIT leftovers on POSIX, like the real server.
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
    """Start the HTTP server and block until interrupted."""
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

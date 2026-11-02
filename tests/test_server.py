"""Tests for the custom interview frontend HTTP server.

The tests spin up a real ``ThreadingHTTPServer`` on port 0 and point
``server.FRONTEND_DIR`` at a temporary directory, so no network access and no
real ``frontend/`` files are required.
"""

import errno
import http.client
import io
import json
import threading

import jwt
import pytest

import server
from server import create_token


SECRET_MARKER = b"TOP-SECRET-DO-NOT-LEAK"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------
def http_request(port, method="GET", path="/", headers=None):
    """Perform one HTTP request and return (status, lowercase headers, body)."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        response_headers = {
            name.lower(): value for name, value in response.getheaders()
        }
        return response.status, response_headers, body
    finally:
        connection.close()


def decode_claims(token):
    """Decode JWT claims without verifying the signature (no network needed)."""
    return jwt.decode(token, options={"verify_signature": False})


def token_metadata(token):
    """Return the parsed ``metadata`` claim of a LiveKit token."""
    return json.loads(decode_claims(token)["metadata"])


@pytest.fixture()
def frontend_root(tmp_path, monkeypatch):
    """Create a temporary frontend tree and make the server serve from it."""
    root = tmp_path / "frontend"
    root.mkdir()
    (root / "index.html").write_text(
        "<!doctype html><title>AI Interviewer</title>", encoding="utf-8"
    )
    (root / "styles.css").write_text("body { color: #fff; }", encoding="utf-8")
    (root / "app.js").write_text("console.log('interviewer');", encoding="utf-8")

    binary_assets = {
        "logo.svg": b"<svg/>",
        "logo.png": b"\x89PNG\r\n\x1a\n",
        "favicon.ico": b"\x00\x00\x01\x00",
        "data.json": b'{"ok": true}',
        "font.woff2": b"wOF2",
        "app.js.map": b'{"version": 3}',
        "blob.bin": b"\x00\x01\x02",
    }
    for name, content in binary_assets.items():
        (root / name).write_bytes(content)

    nested = root / "assets"
    nested.mkdir()
    (nested / "theme.css").write_text("/* theme */", encoding="utf-8")

    # Sibling directory whose name merely *starts with* the frontend directory
    # name: the old startswith() check would happily serve files from here.
    evil_sibling = tmp_path / "frontend_evil"
    evil_sibling.mkdir()
    (evil_sibling / "secret.txt").write_bytes(SECRET_MARKER)
    (tmp_path / "secret.txt").write_bytes(SECRET_MARKER)

    monkeypatch.setattr(server, "FRONTEND_DIR", str(root))
    return root


@pytest.fixture()
def http_port(frontend_root):
    """Run a real ThreadingHTTPServer on an ephemeral port for one test."""
    httpd = server.create_server(host="127.0.0.1", port=0)
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Token unit test (pre-existing behaviour)
# ---------------------------------------------------------------------------
def test_create_token():
    token = create_token("test-room", "test-user")
    assert isinstance(token, str)
    assert len(token) > 20
    assert token.startswith("ey")  # JWT prefix


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------
def test_index_is_served_as_html(http_port):
    status, headers, body = http_request(http_port, "GET", "/")
    assert status == 200
    assert headers["content-type"].startswith("text/html")
    assert headers["cache-control"] == "no-store"
    assert b"AI Interviewer" in body


@pytest.mark.parametrize(
    "filename, expected_type",
    [
        ("index.html", "text/html"),
        ("styles.css", "text/css"),
        ("app.js", "application/javascript"),
        ("logo.svg", "image/svg+xml"),
        ("logo.png", "image/png"),
        ("favicon.ico", "image/x-icon"),
        ("data.json", "application/json"),
        ("font.woff2", "font/woff2"),
        ("app.js.map", "application/json"),
        ("blob.bin", "application/octet-stream"),
    ],
)
def test_static_content_types(http_port, filename, expected_type):
    status, headers, body = http_request(http_port, "GET", "/" + filename)
    assert status == 200, filename
    assert headers["content-type"].startswith(expected_type), filename
    assert body  # non-empty asset


def test_css_and_js_use_short_cache(http_port):
    for path in ("/styles.css", "/app.js"):
        _, headers, _ = http_request(http_port, "GET", path)
        assert "max-age=300" in headers["cache-control"], path


def test_nested_static_file_is_served(http_port):
    status, headers, _ = http_request(http_port, "GET", "/assets/theme.css")
    assert status == 200
    assert headers["content-type"].startswith("text/css")


def test_percent_encoded_benign_path_is_decoded(http_port):
    status, headers, _ = http_request(http_port, "GET", "/%61pp.js")
    assert status == 200
    assert headers["content-type"].startswith("application/javascript")


def test_query_string_does_not_break_static_serving(http_port):
    status, _, _ = http_request(http_port, "GET", "/?v=123")
    assert status == 200


def test_missing_file_returns_404(http_port):
    status, headers, body = http_request(http_port, "GET", "/does-not-exist.css")
    assert status == 404
    assert headers["content-type"].startswith("text/plain")
    assert int(headers["content-length"]) == len(body)
    assert headers["cache-control"] == "no-store"


def test_directory_path_returns_404(http_port):
    status, _, _ = http_request(http_port, "GET", "/assets/")
    assert status == 404


# ---------------------------------------------------------------------------
# Path traversal
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    [
        "/../secret.txt",
        "/%2e%2e%2fsecret.txt",  # percent-encoded ../
        "/..%2fsecret.txt",
        "/../frontend_evil/secret.txt",  # prefix-similar sibling directory
        "/%2e%2e/frontend_evil/secret.txt",
        "/..\\secret.txt",  # Windows-style separator
        "/..%5csecret.txt",
    ],
)
def test_path_traversal_is_blocked(http_port, path):
    status, _, body = http_request(http_port, "GET", path)
    assert status == 403, path
    assert SECRET_MARKER not in body, path


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------
def test_token_endpoint_contract(http_port, monkeypatch):
    monkeypatch.delenv("INTERVIEW_MODE", raising=False)
    status, headers, body = http_request(
        http_port, "GET", "/api/token?room=secure-room&identity=candidate-1"
    )
    assert status == 200
    assert headers["content-type"].startswith("application/json")
    payload = json.loads(body)
    assert set(payload) == {
        "serverUrl",
        "roomName",
        "identity",
        "token",
        "interviewMode",
    }
    assert payload["roomName"] == "secure-room"
    assert payload["identity"] == "candidate-1"
    assert payload["serverUrl"] == server.LIVEKIT_URL
    assert payload["interviewMode"] == "quick"

    claims = decode_claims(payload["token"])
    assert claims["sub"] == "candidate-1"
    assert claims["video"]["room"] == "secure-room"
    assert claims["video"]["roomJoin"] is True
    metadata = json.loads(claims["metadata"])
    assert metadata["interview_mode"] == "quick"
    assert metadata["session_id"]


def test_token_endpoint_defaults(http_port, monkeypatch):
    monkeypatch.delenv("INTERVIEW_MODE", raising=False)
    status, _, body = http_request(http_port, "GET", "/api/token")
    assert status == 200
    payload = json.loads(body)
    assert set(payload) == {
        "serverUrl",
        "roomName",
        "identity",
        "token",
        "interviewMode",
    }
    assert payload["roomName"] == "ai-room"
    assert payload["identity"].startswith("candidate-")
    assert payload["interviewMode"] == "quick"
    metadata = token_metadata(payload["token"])
    assert metadata["interview_mode"] == "quick"
    assert metadata["session_id"]


# ---------------------------------------------------------------------------
# Interview mode (quick / real) -> token metadata
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["quick", "real"])
def test_token_mode_is_written_to_metadata(http_port, monkeypatch, mode):
    monkeypatch.delenv("INTERVIEW_MODE", raising=False)
    status, _, body = http_request(
        http_port,
        "GET",
        "/api/token?room=mode-room&identity=mode-user&mode=%s" % mode,
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["interviewMode"] == mode
    assert token_metadata(payload["token"])["interview_mode"] == mode


def test_token_mode_uses_environment_default(http_port, monkeypatch):
    monkeypatch.setenv("INTERVIEW_MODE", "real")
    _, _, body = http_request(http_port, "GET", "/api/token")
    payload = json.loads(body)
    assert payload["interviewMode"] == "real"
    assert token_metadata(payload["token"])["interview_mode"] == "real"

    # An explicit query parameter overrides the environment default.
    _, _, body = http_request(http_port, "GET", "/api/token?mode=quick")
    payload = json.loads(body)
    assert payload["interviewMode"] == "quick"
    assert token_metadata(payload["token"])["interview_mode"] == "quick"


def test_token_mode_environment_default_is_normalized(http_port, monkeypatch):
    monkeypatch.setenv("INTERVIEW_MODE", "REAL")
    _, _, body = http_request(http_port, "GET", "/api/token")
    assert json.loads(body)["interviewMode"] == "real"


def test_invalid_environment_mode_falls_back_to_quick(http_port, monkeypatch):
    monkeypatch.setenv("INTERVIEW_MODE", "turbo")
    _, _, body = http_request(http_port, "GET", "/api/token")
    assert json.loads(body)["interviewMode"] == "quick"


@pytest.mark.parametrize("query", ["mode=", "mode=+", "mode=%20"])
def test_blank_mode_falls_back_to_default(http_port, monkeypatch, query):
    monkeypatch.delenv("INTERVIEW_MODE", raising=False)
    status, _, body = http_request(http_port, "GET", "/api/token?" + query)
    assert status == 200
    assert json.loads(body)["interviewMode"] == "quick"


@pytest.mark.parametrize("mode", ["turbo", "quickk", "hard", "0"])
def test_token_invalid_mode_returns_400(http_port, mode):
    status, headers, body = http_request(
        http_port, "GET", "/api/token?mode=%s" % mode
    )
    assert status == 400, mode
    payload = json.loads(body)
    assert payload["error"] == "invalid mode"
    detail = payload["detail"]
    assert "quick" in detail and "real" in detail
    assert int(headers["content-length"]) == len(body)


def test_create_token_defaults_to_quick_mode():
    token = create_token("default-room", "default-user")
    metadata = token_metadata(token)
    assert metadata["interview_mode"] == "quick"
    assert metadata["session_id"]


def test_create_token_mode_parameter():
    token = create_token("real-room", "real-user", "real")
    metadata = token_metadata(token)
    assert metadata["interview_mode"] == "real"
    assert metadata["session_id"]
    claims = decode_claims(token)
    assert claims["sub"] == "real-user"
    assert claims["video"]["room"] == "real-room"


def test_create_token_gets_a_fresh_session_id_per_token():
    first = token_metadata(create_token("room", "same-user"))
    second = token_metadata(create_token("room", "same-user"))
    assert first["session_id"] != second["session_id"]


def test_create_token_ttl_is_bounded(monkeypatch):
    monkeypatch.setattr(server, "TOKEN_TTL_SECONDS", 3600)
    claims = decode_claims(create_token("room", "user"))
    assert 3500 <= claims["exp"] - claims["nbf"] <= 3700
    assert create_token("room", "user", ttl_seconds=60)
    short = decode_claims(create_token("room", "user", ttl_seconds=60))
    assert 55 <= short["exp"] - short["nbf"] <= 65


@pytest.mark.parametrize(
    "path",
    [
        "/api/token?room=bad%20room",  # space
        "/api/token?room=%3Cscript%3E",  # HTML/JS characters
        "/api/token?identity=" + "a" * 65,  # over-long identity
        "/api/token?room=" + "r" * 65,  # over-long room
    ],
)
def test_token_endpoint_rejects_invalid_identifiers(http_port, path):
    status, headers, body = http_request(http_port, "GET", path)
    assert status == 400, path
    assert "error" in json.loads(body)
    assert int(headers["content-length"]) == len(body)


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------
def test_health_endpoint(http_port):
    status, headers, body = http_request(http_port, "GET", "/api/health")
    assert status == 200
    assert headers["content-type"].startswith("application/json")
    assert json.loads(body) == {"status": "ok"}
    assert headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# Headers / response correctness
# ---------------------------------------------------------------------------
def test_content_length_matches_body(http_port):
    for path in ("/", "/styles.css", "/missing.js", "/api/health", "/api/token"):
        status, headers, body = http_request(http_port, "GET", path)
        assert status in (200, 404), path
        assert "content-length" in headers, path
        assert int(headers["content-length"]) == len(body), path


def test_security_headers_present(http_port):
    for path in ("/", "/api/health", "/missing.js"):
        _, headers, _ = http_request(http_port, "GET", path)
        assert headers["x-content-type-options"] == "nosniff", path
        assert headers["referrer-policy"] == "no-referrer", path
        assert headers["x-frame-options"] == "DENY", path


# ---------------------------------------------------------------------------
# Method handling
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS", "FOO"])
def test_non_get_methods_return_405(http_port, method):
    status, headers, body = http_request(http_port, method, "/")
    assert status == 405, method
    assert headers["allow"] == "GET"
    assert int(headers["content-length"]) == len(body)


def test_head_is_not_served(http_port):
    status, headers, body = http_request(http_port, "HEAD", "/")
    assert status == 405
    assert headers["allow"] == "GET"
    assert body == b""  # HEAD responses carry no body


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
def test_cors_is_not_wildcard(http_port):
    for path in ("/", "/api/health", "/api/token"):
        _, headers, _ = http_request(
            http_port, "GET", path, {"Origin": "https://evil.example"}
        )
        assert headers.get("access-control-allow-origin") is None, path


def test_allowed_origin_whitelist(http_port, monkeypatch):
    monkeypatch.setattr(server, "ALLOWED_ORIGINS", ("http://localhost:8080",))

    _, headers, _ = http_request(
        http_port, "GET", "/api/health", {"Origin": "http://localhost:8080"}
    )
    assert headers["access-control-allow-origin"] == "http://localhost:8080"

    _, headers, _ = http_request(
        http_port, "GET", "/api/health", {"Origin": "https://evil.example"}
    )
    assert headers.get("access-control-allow-origin") is None


# ---------------------------------------------------------------------------
# Error handling / server configuration
# ---------------------------------------------------------------------------
def test_unhandled_error_returns_500(http_port, monkeypatch):
    def boom(room_name, identity, mode):
        raise RuntimeError("token generation backend unavailable")

    monkeypatch.setattr(server, "create_token", boom)
    status, headers, body = http_request(http_port, "GET", "/api/token")
    assert status == 500
    assert int(headers["content-length"]) == len(body)
    assert headers["x-content-type-options"] == "nosniff"


def test_server_is_threaded_and_reusable():
    httpd = server.create_server(host="127.0.0.1", port=0)
    try:
        from http.server import ThreadingHTTPServer

        assert isinstance(httpd, ThreadingHTTPServer)
        assert httpd.daemon_threads is True
        assert httpd.allow_reuse_address is True
    finally:
        httpd.server_close()


def test_create_server_detects_busy_port():
    import socket

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy_port = blocker.getsockname()[1]
        with pytest.raises(OSError) as excinfo:
            server.create_server(host="127.0.0.1", port=busy_port)
        assert excinfo.value.errno == errno.EADDRINUSE
    finally:
        blocker.close()


def test_port_in_use_exits_with_friendly_error(monkeypatch):
    def failing_create_server(host, port):
        raise OSError(errno.EADDRINUSE, "Address already in use")

    messages = []
    monkeypatch.setattr(server, "create_server", failing_create_server)
    monkeypatch.setattr(server, "configure_logging", lambda *args, **kwargs: server.logger)
    monkeypatch.setattr(
        server.logger,
        "error",
        lambda fmt, *args, **kwargs: messages.append(fmt % args),
    )

    with pytest.raises(SystemExit) as excinfo:
        server.run_server(port=8092)
    assert excinfo.value.code == 1
    assert messages, "expected a friendly bind error to be logged"
    assert "already in use" in messages[0]
    assert "8092" in messages[0]


def test_run_server_closes_on_keyboard_interrupt(monkeypatch):
    events = []

    class FakeServer:
        server_address = ("127.0.0.1", 9999)

        def serve_forever(self, poll_interval=0.5):
            events.append("serve_forever")
            raise KeyboardInterrupt

        def server_close(self):
            events.append("server_close")

    monkeypatch.setattr(server, "create_server", lambda host, port: FakeServer())
    monkeypatch.setattr(server, "configure_logging", lambda *args, **kwargs: server.logger)

    server.run_server(port=9999)
    assert events == ["serve_forever", "server_close"]


def test_console_stream_encoding_is_safe():
    """Regression test: cp936 consoles must not raise on emoji in log output."""
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp936", errors="strict")
    server._make_stream_encoding_safe(stream)
    stream.write("startup ok \U0001F680")
    stream.write(" 启动")
    stream.flush()

    encoded = raw.getvalue()
    assert b"startup ok" in encoded
    assert "启动".encode("cp936") in encoded
    # The emoji was escaped instead of raising UnicodeEncodeError.
    assert "\U0001F680".encode("utf-8") not in encoded

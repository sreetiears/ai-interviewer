import json
import logging
import os
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from livekit.api import AccessToken, VideoGrants

load_dotenv()

LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "devkey")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "secret")
LIVEKIT_URL = os.getenv("LIVEKIT_URL", "ws://127.0.0.1:7880")
PORT = int(os.getenv("FRONTEND_PORT", 8080))

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "frontend")


def create_token(room_name: str, identity: str) -> str:
    token = (
        AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_grants(VideoGrants(room_join=True, room=room_name))
        .to_jwt()
    )
    return token


class InterviewHttpHandler(BaseHTTPRequestHandler):
    def end_headers(self):
        # Enable CORS for convenience
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)

        # Token Generation API Endpoint
        if parsed.path == "/api/token":
            params = parse_qs(parsed.query)
            room_name = params.get("room", ["ai-room"])[0]
            identity = params.get("identity", [f"candidate-{uuid.uuid4().hex[:4]}"])[0]

            jwt_token = create_token(room_name, identity)
            response_payload = {
                "serverUrl": LIVEKIT_URL,
                "roomName": room_name,
                "identity": identity,
                "token": jwt_token,
            }

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(response_payload).encode("utf-8"))
            return

        # Serve static frontend files
        req_path = parsed.path.lstrip("/") or "index.html"
        safe_path = os.path.normpath(os.path.join(FRONTEND_DIR, req_path))

        if not safe_path.startswith(FRONTEND_DIR) or not os.path.isfile(safe_path):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"404 Not Found")
            return

        self.send_response(200)
        if safe_path.endswith(".html"):
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif safe_path.endswith(".css"):
            self.send_header("Content-Type", "text/css; charset=utf-8")
        elif safe_path.endswith(".js"):
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.end_headers()

        with open(safe_path, "rb") as f:
            self.wfile.write(f.read())


def run_server(port: int = PORT):
    server = HTTPServer(("0.0.0.0", port), InterviewHttpHandler)
    print(f"🚀 Custom Interview Frontend running at http://localhost:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run_server()

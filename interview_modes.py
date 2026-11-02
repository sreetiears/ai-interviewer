"""Interview-mode vocabulary shared by the graph, the agent and the HTTP server.

This module deliberately imports nothing from the rest of the project and has no
third-party dependencies.  The static-file server can therefore share the mode
list without importing LiveKit or LangChain, and the graph can share it without
importing the server.  Keeping the names in one place means a new mode cannot be
half-added (for example accepted by ``/api/token`` but rejected by the graph).
"""

from __future__ import annotations

MODE_NAMES: tuple[str, ...] = ("quick", "real")
DEFAULT_MODE = "quick"

MODE_LABELS: dict[str, str] = {
    "quick": "简单模式",
    "real": "实战模式",
}


def normalize_mode(value: str | None) -> str:
    """Map arbitrary user/config input onto a known mode name."""
    candidate = (value or "").strip().lower()
    return candidate if candidate in MODE_NAMES else DEFAULT_MODE


def is_valid_mode(value: str | None) -> bool:
    """True only for an exact, known mode name (after trimming/case folding)."""
    return (value or "").strip().lower() in MODE_NAMES

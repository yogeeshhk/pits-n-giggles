# MIT License
#
# Copyright (c) [2025] [Ashwin Natarajan]
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# -------------------------------------- IMPORTS -----------------------------------------------------------------------

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import apps.web.save_viewer_state as SaveViewerState
from apps.web.session_discovery import build_session_list, load_session_json

# -------------------------------------- CONSTANTS ---------------------------------------------------------------------

SAVED_SESSIONS_LIST_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "available": {"type": "boolean"},
        "error": {"type": ["string", "null"]},
        "session_dir": {"type": "string"},
        "count": {"type": "integer"},
        "sessions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": True,
            },
        },
    },
    "required": ["ok", "available", "count", "sessions"],
    "additionalProperties": True,
}

SAVED_SESSION_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "available": {"type": "boolean"},
        "error": {"type": ["string", "null"]},
        "slug": {"type": ["string", "null"]},
        "session": {
            "type": ["object", "null"],
            "additionalProperties": True,
        },
        "data": {
            "type": ["object", "null"],
            "additionalProperties": True,
        },
    },
    "required": ["ok", "available"],
    "additionalProperties": True,
}

# -------------------------------------- FUNCTIONS ---------------------------------------------------------------------

async def list_saved_sessions(
    session_dir: Path,
    logger: logging.Logger,
    app_version: str,
    *,
    limit: int = 20,
    offset: int = 0,
) -> Dict[str, Any]:
    """List saved session summaries from disk."""

    sessions, _ = await _load_session_index(session_dir, logger, app_version)
    if sessions is None:
        return _session_dir_unavailable(session_dir)

    safe_offset = max(0, offset)
    safe_limit = max(1, min(limit, 100))
    window = sessions[safe_offset:safe_offset + safe_limit]
    return {
        "ok": True,
        "available": True,
        "error": None,
        "session_dir": str(session_dir),
        "count": len(sessions),
        "offset": safe_offset,
        "limit": safe_limit,
        "sessions": window,
    }


async def get_saved_session_summary(
    session_dir: Path,
    logger: logging.Logger,
    app_version: str,
    slug: str,
) -> Dict[str, Any]:
    """Get session-level saved telemetry data for a saved session slug."""

    loaded = await _load_saved_session(session_dir, logger, app_version, slug)
    if not loaded["ok"]:
        return loaded

    data = loaded["raw_data"]
    summary = {
        "telemetry": SaveViewerState.getTelemetryInfoFrom(data),
        "race": SaveViewerState.getRaceInfoFrom(data),
    }
    return _saved_session_success(slug, loaded["session"], summary)


async def get_saved_session_driver_info(
    session_dir: Path,
    logger: logging.Logger,
    app_version: str,
    slug: str,
    driver_index: int,
) -> Dict[str, Any]:
    """Get detailed per-driver data from a saved session slug."""

    loaded = await _load_saved_session(session_dir, logger, app_version, slug)
    if not loaded["ok"]:
        return loaded

    driver_info = SaveViewerState.getDriverInfoFrom(loaded["raw_data"], driver_index)
    if not driver_info:
        return {
            "ok": False,
            "available": True,
            "error": "driver_not_found",
            "slug": slug,
            "session": loaded["session"],
            "data": None,
        }

    return _saved_session_success(slug, loaded["session"], driver_info)


async def _load_saved_session(
    session_dir: Path,
    logger: logging.Logger,
    app_version: str,
    slug: str,
    *,
    recompute: bool = True,
) -> Dict[str, Any]:
    sessions, slug_map = await _load_session_index(session_dir, logger, app_version)
    if sessions is None:
        return _session_dir_unavailable(session_dir)

    session = next((entry for entry in sessions if entry.get("slug") == slug), None)
    if session is None:
        return {
            "ok": False,
            "available": True,
            "error": "session_not_found",
            "slug": slug,
            "session": None,
            "data": None,
        }

    data = await load_session_json(session_dir, slug_map, slug, recompute=recompute)
    if data is None:
        return {
            "ok": False,
            "available": True,
            "error": "session_load_failed",
            "slug": slug,
            "session": session,
            "data": None,
        }

    return {
        "ok": True,
        "session": session,
        "raw_data": data,
    }


async def _load_session_index(
    session_dir: Path,
    logger: logging.Logger,
    app_version: str,
) -> Tuple[Optional[List[Dict[str, Any]]], Dict[str, str]]:
    if not session_dir.exists():
        logger.debug("Saved session directory does not exist: %s", session_dir)
        return None, {}

    sessions: List[Dict[str, Any]] = []
    slug_map: Dict[str, str] = {}
    async for current_sessions, current_slug_map in build_session_list(session_dir, logger, app_version):
        sessions = current_sessions
        slug_map = current_slug_map
    return sessions, slug_map


def _session_dir_unavailable(session_dir: Path) -> Dict[str, Any]:
    return {
        "ok": False,
        "available": False,
        "error": "session_dir_not_found",
        "session_dir": str(session_dir),
        "count": 0,
        "sessions": [],
    }


def _saved_session_success(slug: str, session: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ok": True,
        "available": True,
        "error": None,
        "slug": slug,
        "session": session,
        "data": data,
    }

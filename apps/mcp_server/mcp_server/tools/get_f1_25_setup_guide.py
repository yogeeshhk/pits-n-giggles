# MIT License
#
# Copyright (c) [2026] [Ashwin Natarajan]
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

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

from apps.mcp_server.state import get_state_data

# -------------------------------------- CONSTANTS ---------------------------------------------------------------------

F1_SETUP_GUIDE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "available": {"type": "boolean"},
        "error": {"type": ["string", "null"]},
        "game": {"type": ["string", "null"]},
        "game_year": {"type": ["integer", "null"]},
        "source": {"type": ["string", "null"]},
        "requested_circuit": {"type": ["string", "null"]},
        "resolved_circuit": {"type": ["string", "null"]},
        "matched_from_live_session": {"type": "boolean"},
        "setup": {
            "type": ["object", "null"],
            "additionalProperties": True,
        },
        "unavailable_fields": {
            "type": ["array", "null"],
            "items": {"type": "string"},
        },
        "setups": {
            "type": ["array", "null"],
            "items": {"type": "object", "additionalProperties": True},
        },
        "tyreTemps": {
            "type": ["array", "null"],
            "items": {"type": "object", "additionalProperties": True},
        },
        "fixes": {
            "type": ["array", "null"],
            "items": {"type": "object", "additionalProperties": True},
        },
        "faq": {
            "type": ["array", "null"],
            "items": {"type": "object", "additionalProperties": True},
        },
    },
    "required": ["ok", "available", "matched_from_live_session"],
    "additionalProperties": True,
}

_DATA_DIR = Path(__file__).resolve().parents[4] / "apps" / "frontend" / "data"
_SETUP_GUIDE_PATHS = {
    2025: _DATA_DIR / "f1_25_setups.json",
    2026: _DATA_DIR / "f1_26_setups.json",
}

_SETUP_FIELDS = (
    "aero",
    "differential",
    "suspension-geometry",
    "suspension",
    "brakes",
    "tyres-q",
    "tyres-r",
    "compounds",
    "strategy-50",
    "laps-50",
    "creation-date",
    "notes",
)

_CIRCUIT_ALIASES = {
    "melbourne": "australia",
    "shanghai": "china",
    "sakhir": "bahrain",
    "sakhir bahrain": "bahrain",
    "catalunya": "spain",
    "montreal": "canada",
    "silverstone": "britain",
    "silverstone reverse": "britain",
    "great britain": "britain",
    "hungaroring": "hungary",
    "spa": "belgium",
    "suzuka": "japan",
    "baku": "azerbaijan",
    "baku azerbaijan": "azerbaijan",
    "zandvoort": "netherlands",
    "zandvoort reverse": "netherlands",
    "texas": "united states",
    "jeddah": "saudi arabia",
    "losail": "qatar",
    "monza": "italy",
}

# -------------------------------------- FUNCTIONS ---------------------------------------------------------------------

def get_f1_setup_guide(
    logger: logging.Logger,
    circuit: Optional[str] = None,
    game_year: Optional[int] = None,
    include_all: bool = False,
) -> Dict[str, Any]:
    """Return F1 setup guide data from the bundled setup sheet exports."""

    resolved_game_year = _resolve_game_year(game_year)
    try:
        guide = _load_setup_guide(resolved_game_year)
    except OSError as exc:
        logger.error("Failed to load F1 setup guide: %s", exc)
        return _error_response("setup_guide_load_failed")
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse F1 setup guide: %s", exc)
        return _error_response("setup_guide_parse_failed")

    requested_circuit = circuit
    matched_from_live_session = False
    if not requested_circuit:
        requested_circuit = _get_live_circuit()
        matched_from_live_session = bool(requested_circuit)

    setup = _find_setup(guide, requested_circuit) if requested_circuit else None
    response: Dict[str, Any] = {
        "ok": True,
        "available": True,
        "error": None,
        "game": guide.get("game"),
        "game_year": resolved_game_year,
        "source": guide.get("source"),
        "requested_circuit": requested_circuit,
        "resolved_circuit": setup.get("circuit") if setup else None,
        "matched_from_live_session": matched_from_live_session,
        "setup": setup,
        "unavailable_fields": _get_unavailable_fields(setup),
        "setups": guide.get("setups") if include_all else None,
        "tyreTemps": guide.get("tyreTemps"),
        "fixes": guide.get("fixes"),
        "faq": guide.get("faq"),
    }

    if requested_circuit and setup is None:
        response["ok"] = False
        response["error"] = "setup_not_found"

    return response


def get_f1_25_setup_guide(
    logger: logging.Logger,
    circuit: Optional[str] = None,
    include_all: bool = False,
) -> Dict[str, Any]:
    """Backward-compatible wrapper for callers that still use the F1 25 helper name."""

    return get_f1_setup_guide(logger, circuit=circuit, game_year=2025, include_all=include_all)


@lru_cache(maxsize=2)
def _load_setup_guide(game_year: int) -> Dict[str, Any]:
    with _SETUP_GUIDE_PATHS[game_year].open("r", encoding="utf-8") as setup_file:
        return json.load(setup_file)


def _resolve_game_year(game_year: Optional[int]) -> int:
    parsed_game_year = _parse_game_year(game_year)
    if parsed_game_year in _SETUP_GUIDE_PATHS:
        return parsed_game_year

    telemetry_update_entry = get_state_data("race-table-update")
    if telemetry_update_entry is not None and isinstance(telemetry_update_entry.data, dict):
        telemetry_game_year = _parse_game_year(telemetry_update_entry.data.get("f1-game-year"))
        if telemetry_game_year in _SETUP_GUIDE_PATHS:
            return telemetry_game_year

    return 2025


def _parse_game_year(game_year: Any) -> Optional[int]:
    try:
        return int(game_year)
    except (TypeError, ValueError):
        return None


def _get_live_circuit() -> Optional[str]:
    telemetry_update_entry = get_state_data("race-table-update")
    if telemetry_update_entry is None:
        return None
    telemetry_update = telemetry_update_entry.data
    circuit = telemetry_update.get("circuit") if isinstance(telemetry_update, dict) else None
    if circuit == "---":
        return None
    return circuit


def _find_setup(guide: Dict[str, Any], circuit: Optional[str]) -> Optional[Dict[str, Any]]:
    normalized_circuit = _normalize_circuit(circuit)
    if not normalized_circuit:
        return None

    for setup in guide.get("setups", []):
        if _normalize_circuit(setup.get("circuit")) == normalized_circuit:
            return setup
    return None


def _get_unavailable_fields(setup: Optional[Dict[str, Any]]) -> Optional[list[str]]:
    if setup is None:
        return None
    return [
        field
        for field in _SETUP_FIELDS
        if setup.get(field) is None or setup.get(field) == ""
    ]


def _normalize_circuit(circuit: Optional[str]) -> str:
    if not circuit:
        return ""

    normalized = (
        circuit
        .replace("_Reverse", " Reverse")
        .replace("_", " ")
        .strip()
        .lower()
    )
    return _CIRCUIT_ALIASES.get(normalized, normalized)


def _error_response(error: str) -> Dict[str, Any]:
    return {
        "ok": False,
        "available": False,
        "error": error,
        "game": None,
        "game_year": None,
        "source": None,
        "requested_circuit": None,
        "resolved_circuit": None,
        "matched_from_live_session": False,
        "setup": None,
        "unavailable_fields": None,
        "setups": None,
        "tyreTemps": None,
        "fixes": None,
        "faq": None,
    }


F1_25_SETUP_GUIDE_OUTPUT_SCHEMA = F1_SETUP_GUIDE_OUTPUT_SCHEMA

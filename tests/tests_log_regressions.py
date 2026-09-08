"""Regression coverage for the September 8 telemetry and discovery failures."""
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from apps.web.session_discovery import (
    build_session_list, find_json_files, parse_filename, resolve_session_meta,
)
from lib.f1_types import F1PacketType
from lib.telemetry_manager.factory import PacketParserFactory
from tests.f1_types.tests_parser_base import F1TypesTest


@pytest.mark.parametrize("code", [b"ZZZZ", b"NONE", b"\xffABC", b"SS"])
def test_bad_event_is_dropped_and_next_event_is_parsed(code):
    logger = Mock()
    factory = PacketParserFactory({F1PacketType.EVENT}, logger)
    header = F1TypesTest.getRandomHeader(F1PacketType.EVENT, 24, 22).to_bytes()
    assert factory.parse(header + code) is None
    assert factory.last_failure_reason
    if len(code) == 4:
        assert repr(code) in factory.last_failure_reason
    logger.error.assert_called_once()
    assert factory.parse(header + b"SSTA" + bytes(12)) is not None
    assert factory.last_failure_reason is None


def test_discovery_excludes_cache_files_and_directories(tmp_path):
    for name in ("png_session_cache.json", "PNG_SESSION_CACHE.JSON",
                 ".png_session_cache.json", "race.json"):
        (tmp_path / name).write_text("{}")
    (tmp_path / "directory.json").mkdir()
    assert find_json_files(tmp_path) == [Path("race.json")]


@pytest.mark.parametrize("name", ["race.json", "png_session_cache.json",
    "Race_Shanghai_2026_99_08_19_03_49.json"])
def test_invalid_filename_has_safe_metadata(name):
    assert parse_filename(Path(name)) == {"sessionType": "", "track": "", "date": ""}
    meta = resolve_session_meta(Path(name), {"track-id": "Shanghai", "session-type": "Race"})
    assert meta["track"] == "Shanghai"
    assert meta["sessionType"] == "Race"


def test_valid_filename_metadata():
    assert parse_filename(Path("Short_Qualifying_Shanghai_2026_09_08_18_23_25.json")) == {
        "sessionType": "Short Qualifying", "track": "Shanghai", "date": "2026-09-08T18:23:25",
    }


@pytest.mark.asyncio
async def test_unreadable_short_filename_does_not_abort_rebuild(tmp_path):
    (tmp_path / "unreadable.json").write_text("{}")
    with patch("apps.web.session_discovery._parse_session_metadata", side_effect=PermissionError("denied")):
        snapshots = [snapshot async for snapshot in build_session_list(tmp_path, Mock(), "test")]
    assert len(snapshots) == 1
    sessions, slug_map = snapshots[0]
    assert len(sessions) == 1
    assert "unreadable" in slug_map

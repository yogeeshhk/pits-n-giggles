"""Regression tests for cache-file discovery and malformed session filenames."""
from pathlib import Path
from unittest.mock import Mock, patch

import orjson
import pytest

from apps.web import session_discovery as discovery


@pytest.mark.parametrize("name", ["png_session_cache.json", "PNG_SESSION_CACHE.JSON",
                                  ".png_session_cache.json"])
def test_cache_files_are_excluded_recursively(tmp_path, name):
    nested = tmp_path / "sessions"
    nested.mkdir()
    (nested / name).write_text("{}")
    (nested / "directory.json").mkdir()
    (nested / "race.json").write_text("{}")
    assert discovery.find_json_files(tmp_path) == [Path("sessions/race.json")]


@pytest.mark.parametrize("name", ["race.json", "png_session_cache.json",
                                  "Race_Shanghai_2026_99_08_19_03_49.json"])
def test_invalid_filename_preserves_json_metadata(name):
    assert discovery.parse_filename(Path(name)) == {
        "sessionType": "", "track": Path(name).stem, "date": "",
    }
    assert discovery.resolve_session_meta(Path(name), {
        "track-id": "Shanghai", "session-type": "Race",
    }) == {"track": "Shanghai", "sessionType": "Race", "date": ""}


def test_valid_filename_metadata():
    assert discovery.parse_filename(Path("Short_Qualifying_Shanghai_2026_09_08_18_23_25.json")) == {
        "sessionType": "Short Qualifying", "track": "Shanghai", "date": "2026-09-08T18:23:25",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("unreadable", [False, True])
async def test_rebuild_survives_bad_json_and_excludes_cache(tmp_path, unreadable):
    valid_name = "Race_Shanghai_2026_09_08_18_23_25.json"
    (tmp_path / valid_name).write_bytes(orjson.dumps({
        "session-info": {"track-id": "Shanghai", "session-type": "Race"},
    }))
    (tmp_path / "bad.json").write_text("not json")
    (tmp_path / "png_session_cache.json").write_text("not a session")
    parse_metadata = discovery._parse_session_metadata

    def parse(path, logger):
        if path.name == "png_session_cache.json":
            pytest.fail("The cache must never be read as a session")
        if unreadable and path.name == "bad.json":
            raise PermissionError("denied")
        return parse_metadata(path, logger)

    with patch.object(discovery, "_parse_session_metadata", side_effect=parse):
        snapshots = [s async for s in discovery.build_session_list(tmp_path, Mock(), "test")]
    sessions, slugs = snapshots[-1]
    assert len(sessions) == 2
    assert next(s for s in sessions if s["slug"] == discovery.to_slug(Path(valid_name)))["track"] == "Shanghai"
    assert set(slugs.values()) == {valid_name, "bad.json"}
    assert (tmp_path / discovery.CACHE_FILE).is_file()
    # The persisted cache must not reintroduce internal files on the next rebuild.
    snapshots = [s async for s in discovery.build_session_list(tmp_path, Mock(), "test")]
    assert snapshots[-1] == (sessions, slugs)

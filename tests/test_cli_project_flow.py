import json
import sqlite3
from pathlib import Path

from helpers import make_mp4, make_wav


PROJECT_DIRS = [
    "media",
    "media/proxies",
    "media/thumbnails",
    "media/frame_strips",
    "analysis",
    "analysis/runs",
    "analysis/gemini_cache",
    "analysis/transcripts",
    "vectors",
    "timelines",
    "renders",
    "directives",
    "logs",
]


def _asset_table_count(db_path):
    with sqlite3.connect(db_path) as connection:
        return connection.execute("select count(*) from assets").fetchone()[0]


def _table_names(db_path):
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "select name from sqlite_master where type = 'table'"
        ).fetchall()
    return {row[0] for row in rows}


def _asset_for_path(assets, filename):
    for asset in assets:
        if any(
            isinstance(value, str) and Path(value).name == filename
            for key, value in asset.items()
            if "path" in key or key in {"uri", "source"}
        ):
            return asset
    raise AssertionError(f"No asset listed for {filename!r}: {assets!r}")


def _has_any_key(asset, keys):
    return any(key in asset and asset[key] is not None for key in keys)


def test_init_creates_project_structure_config_and_database(tmp_path, run_ave):
    project_dir = tmp_path / "family-edit"

    run_ave("init", project_dir)

    assert project_dir.is_dir()
    for relative_dir in PROJECT_DIRS:
        assert (project_dir / relative_dir).is_dir(), relative_dir

    config_path = project_dir / "ave_project.json"
    assert config_path.is_file()
    config = json.loads(config_path.read_text())
    assert isinstance(config, dict)
    assert _has_any_key(config, {"project_id", "id"})
    assert _has_any_key(config, {"schema_version", "version"})

    db_path = project_dir / "library.sqlite"
    assert db_path.is_file()
    tables = _table_names(db_path)
    assert "assets" in tables
    assert "projects" in tables


def test_ingest_folder_is_idempotent_and_ignores_non_media(tmp_path, run_ave):
    project_dir = tmp_path / "project"
    source_dir = tmp_path / "source"
    nested_dir = source_dir / "nested"
    nested_dir.mkdir(parents=True)
    make_mp4(source_dir / "clip_a.mp4")
    make_wav(nested_dir / "voice_note.wav")
    (source_dir / "ignore-me.txt").write_text("not a supported media asset")

    run_ave("init", project_dir)
    run_ave("ingest", project_dir, source_dir)
    first_count = _asset_table_count(project_dir / "library.sqlite")

    run_ave("ingest", project_dir, source_dir)
    second_count = _asset_table_count(project_dir / "library.sqlite")

    assert first_count == 2
    assert second_count == first_count


def test_assets_lists_ingested_media_with_probe_metadata(
    tmp_path, run_ave, list_assets_json
):
    project_dir = tmp_path / "project"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    make_mp4(source_dir / "clip_a.mp4")
    make_wav(source_dir / "voice_note.wav")

    run_ave("init", project_dir)
    run_ave("ingest", project_dir, source_dir)

    assets = list_assets_json(project_dir)

    assert len(assets) == 2
    video = _asset_for_path(assets, "clip_a.mp4")
    audio = _asset_for_path(assets, "voice_note.wav")

    assert _has_any_key(video, {"duration", "duration_seconds"})
    assert _has_any_key(video, {"codec", "video_codec"})
    assert _has_any_key(video, {"resolution", "width"})
    assert _has_any_key(video, {"audio", "has_audio", "audio_status"})

    assert _has_any_key(audio, {"duration", "duration_seconds"})
    assert _has_any_key(audio, {"codec", "audio_codec"})
    assert _has_any_key(audio, {"audio", "has_audio", "audio_status"})

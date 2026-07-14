import json
import sqlite3

from helpers import make_mp4


def _table_count(db_path, table_name):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(f"select count(*) from {table_name}").fetchone()[0]


def _json_stdout(result):
    return json.loads(result.stdout)


def test_analyze_creates_windows_labels_and_artifacts(tmp_path, run_ave):
    project_dir = tmp_path / "project"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    make_mp4(source_dir / "clip_a.mp4", seconds=2)

    run_ave("init", project_dir)
    run_ave("ingest", project_dir, source_dir)
    result = run_ave("analyze", project_dir, "--window-sec", "1", "--json", timeout=60)
    payload = _json_stdout(result)

    assert payload["assets_requested"] == 1
    assert payload["assets_completed"] == 1
    assert payload["windows_created"] >= 1
    assert payload["labels_created"] >= 1
    assert payload["artifacts_created"] >= 1

    db_path = project_dir / "library.sqlite"
    assert _table_count(db_path, "windows") >= 1
    assert _table_count(db_path, "activity_labels") >= 1
    assert _table_count(db_path, "media_artifacts") >= 1

    summary = _json_stdout(run_ave("analysis-summary", project_dir, "--json"))
    assert summary["assets"] == 1
    assert summary["windows"] >= 1
    assert summary["artifacts"]

import json
from pathlib import Path

from helpers import make_mp4


def _json(result):
    return json.loads(result.stdout)


def test_mock_agentic_pipeline_creates_timeline_and_render(tmp_path, run_ave):
    project_dir = tmp_path / "project"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    make_mp4(source_dir / "clip_a.mp4", seconds=2)

    run_ave("init", project_dir)
    run_ave("ingest", project_dir, source_dir)
    run_ave("analyze", project_dir, "--window-sec", "1", "--json", timeout=60)

    transcript = _json(run_ave("transcribe", project_dir, "--provider", "mock", "--json"))
    semantic = _json(run_ave("semantic-analyze", project_dir, "--provider", "mock", "--json"))
    timeline = _json(
        run_ave(
            "timeline",
            project_dir,
            "--directive",
            "make a short performance montage",
            "--duration-sec",
            "2",
            "--max-clip-sec",
            "2",
            "--json",
        )
    )
    render = _json(run_ave("render", project_dir, "--timeline-id", "latest", "--json", timeout=60))

    assert transcript["spans_created"] >= 1
    assert semantic["segments_created"] >= 1
    assert semantic["selects_created"] >= 1
    assert timeline["items_created"] >= 1
    assert render["status"] == "complete"
    assert Path(render["path"]).is_file()

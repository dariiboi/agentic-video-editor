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


def test_mock_context_build_search_plan_and_context_timeline(tmp_path, run_ave):
    project_dir = tmp_path / "project"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    make_mp4(source_dir / "clip_a.mp4", seconds=3)
    make_mp4(source_dir / "clip_b.mp4", seconds=3)

    run_ave("init", project_dir)
    run_ave("ingest", project_dir, source_dir)
    run_ave("analyze", project_dir, "--window-sec", "1", "--json", timeout=60)
    run_ave("semantic-analyze", project_dir, "--provider", "mock", "--json")

    context = _json(run_ave("context-build", project_dir, "--provider", "mock", "--json"))
    search = _json(run_ave("context-search", project_dir, "performance payoff", "--json"))
    plan = _json(
        run_ave(
            "edit-plan",
            project_dir,
            "--directive",
            "make a short performance montage with a payoff",
            "--duration-sec",
            "4",
            "--json",
        )
    )
    timeline = _json(
        run_ave(
            "timeline",
            project_dir,
            "--directive",
            "make a short performance montage with a payoff",
            "--duration-sec",
            "4",
            "--max-clip-sec",
            "2",
            "--context-aware",
            "--json",
        )
    )
    shown = _json(run_ave("timelines", project_dir, "--show", "--json"))

    assert context["context_cards"] == context["segments_seen"]
    assert context["caption_options"] >= context["context_cards"]
    assert search["packets"]
    assert search["packets"][0]["why_matches"]
    assert "warnings" in search["packets"][0]
    assert plan["beat_sheet"]
    assert plan["selected_sequence"]
    assert plan["caption_plan"]
    assert timeline["items_created"] >= 1
    first_item = shown["tracks"][0]["items"][0]
    assert first_item["why_here"]
    assert "caption_text" in first_item
    assert shown["context_aware"] is True


def test_context_aware_timeline_falls_back_without_context_cards(tmp_path, run_ave):
    project_dir = tmp_path / "project"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    make_mp4(source_dir / "clip_a.mp4", seconds=2)

    run_ave("init", project_dir)
    run_ave("ingest", project_dir, source_dir)
    run_ave("analyze", project_dir, "--window-sec", "1", "--json", timeout=60)
    run_ave("semantic-analyze", project_dir, "--provider", "mock", "--json")

    search = _json(run_ave("context-search", project_dir, "performance", "--json"))
    timeline = _json(
        run_ave(
            "timeline",
            project_dir,
            "--directive",
            "make a short performance montage",
            "--duration-sec",
            "2",
            "--context-aware",
            "--json",
        )
    )

    assert search["packets"]
    assert "weak_context" in search["packets"][0]["warnings"]
    assert timeline["items_created"] >= 1

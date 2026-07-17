import json

from helpers import make_mp4

from agentic_video_editor.project import load_project
from agentic_video_editor.qmd_bridge import export_cards
from agentic_video_editor.retrieval import context_search


def _json(result):
    return json.loads(result.stdout)


def _make_faceted_project(tmp_path, run_ave):
    project_dir = tmp_path / "project"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    make_mp4(source_dir / "clip_0.mp4", seconds=2)
    run_ave("init", project_dir)
    run_ave("ingest", project_dir, source_dir)
    run_ave("facets", project_dir, "--provider", "mock", "--json")
    return project_dir


def test_facet_search_finds_casting_attributes(tmp_path, run_ave):
    project_dir = _make_faceted_project(tmp_path, run_ave)

    results = _json(run_ave("facet-search", project_dir, "green t-shirt", "--json"))["results"]

    assert results
    hit = results[0]
    assert hit["observation_type"] == "people_appearance"
    assert hit["asset_id"]
    assert hit["end_sec"] > hit["start_sec"]
    flattened = json.dumps(hit["value"]).lower()
    assert "green" in flattened and "t-shirt" in flattened


def test_facet_search_survives_force_rerun_without_duplicates(tmp_path, run_ave):
    project_dir = _make_faceted_project(tmp_path, run_ave)

    before = _json(run_ave("facet-search", project_dir, "green", "--limit", "50", "--json"))["results"]
    run_ave("facets", project_dir, "--provider", "mock", "--force", "--json")
    after = _json(run_ave("facet-search", project_dir, "green", "--limit", "50", "--json"))["results"]

    assert before
    assert len(after) == len(before)


def test_qmd_card_carries_overlapping_facet_sections(tmp_path, run_ave):
    project_dir = _make_faceted_project(tmp_path, run_ave)
    run_ave("semantic-analyze", project_dir, "--provider", "mock", "--json")

    summary = export_cards(load_project(project_dir))

    assert summary.cards_written >= 1
    cards_dir = load_project(project_dir).root / "qmd_cards"
    card_texts = [path.read_text(encoding="utf-8") for path in sorted(cards_dir.glob("*.md"))]
    faceted = [text for text in card_texts if "## Facets" in text]
    assert faceted, "expected at least one card with a Facets section"
    assert any("green" in text and "t-shirt" in text for text in faceted)
    assert any("people_appearance" in text for text in faceted)


def test_context_search_cites_facet_evidence(tmp_path, run_ave):
    project_dir = _make_faceted_project(tmp_path, run_ave)
    run_ave("semantic-analyze", project_dir, "--provider", "mock", "--json")

    search = context_search(load_project(project_dir), "battle between green t-shirts and blue t-shirts")

    packets = search["packets"]
    assert packets
    top = packets[0]
    facets = top["source_evidence"]["facets"]
    assert facets, "expected facet observations in packet source evidence"
    facet_text = json.dumps(facets).lower()
    assert "green" in facet_text
    assert any("facet observations match" in reason for reason in top["why_matches"])

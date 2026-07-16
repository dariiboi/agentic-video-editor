import json
import sys
from pathlib import Path

from helpers import make_mp4

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentic_video_editor.align import _word_boundary_points, align_project  # noqa: E402
from agentic_video_editor.db import connect_db  # noqa: E402
from agentic_video_editor.project import load_project  # noqa: E402
from agentic_video_editor.qmd_bridge import export_cards, relate_from_qmd  # noqa: E402


def _project_with_asset(tmp_path, run_ave, clips=1):
    project_dir = tmp_path / "project"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    for index in range(clips):
        make_mp4(source_dir / f"clip_{index}.mp4", seconds=3)
    run_ave("init", project_dir)
    run_ave("ingest", project_dir, source_dir)
    return load_project(project_dir)


def test_word_boundary_points_keep_only_pause_adjacent_boundaries():
    words = [
        {"start_sec": 0.5, "end_sec": 0.9},
        {"start_sec": 0.95, "end_sec": 1.4},   # continuous speech: no boundary kept
        {"start_sec": 2.0, "end_sec": 2.5},    # 0.6s pause before: word_start kept
    ]
    points = _word_boundary_points(words, duration_sec=10.0)
    reasons = [(p["time_sec"], p["reason"]) for p in points]
    assert (0.5, "word_start") in reasons
    assert (1.4, "word_end") in reasons
    assert (2.0, "word_start") in reasons
    assert (2.5, "word_end") in reasons
    assert (0.9, "word_end") not in reasons
    assert (0.95, "word_start") not in reasons


def test_align_project_stores_spans_words_and_cut_points(tmp_path, run_ave):
    project = _project_with_asset(tmp_path, run_ave)

    def fake_transcriber(wav_path):
        assert wav_path.exists()
        return [
            {
                "start_sec": 0.4,
                "end_sec": 2.2,
                "text": "I only know my part.",
                "confidence": 0.93,
                "words": [
                    {"text": "I", "start_sec": 0.4, "end_sec": 0.5, "confidence": 0.95},
                    {"text": "only", "start_sec": 0.52, "end_sec": 0.8, "confidence": 0.94},
                    {"text": "know", "start_sec": 0.82, "end_sec": 1.1, "confidence": 0.92},
                    {"text": "my", "start_sec": 1.5, "end_sec": 1.7, "confidence": 0.9},
                    {"text": "part.", "start_sec": 1.72, "end_sec": 2.2, "confidence": 0.91},
                ],
            }
        ]

    summary = align_project(project, transcriber=fake_transcriber)

    assert summary.assets_completed == 1
    assert summary.spans_created == 1
    assert summary.words_created == 5

    with connect_db(project.db_path) as conn:
        span = conn.execute(
            "select text, kind, source from transcript_spans where source = 'local_asr'"
        ).fetchone()
        words = conn.execute("select count(*) as count from word_alignments").fetchone()
        points = conn.execute(
            "select time_sec, reason from scene_boundaries where detector = 'asr_word' order by time_sec"
        ).fetchall()

    assert span["text"] == "I only know my part."
    assert words["count"] == 5
    point_pairs = {(row["time_sec"], row["reason"]) for row in points}
    # pause between "know" (1.1) and "my" (1.5) creates an out/in pair
    assert (1.1, "word_end") in point_pairs
    assert (1.5, "word_start") in point_pairs
    # continuous boundaries inside "I only know" are not snap targets
    assert (0.5, "word_end") not in point_pairs


def test_export_cards_writes_markdown_per_segment(tmp_path, run_ave):
    project = _project_with_asset(tmp_path, run_ave)
    run_ave("semantic-analyze", tmp_path / "project", "--provider", "mock", "--json")

    summary = export_cards(project)

    cards = sorted(Path(summary.cards_dir).glob("seg_*.md"))
    assert summary.cards_written == len(cards) == 1
    content = cards[0].read_text(encoding="utf-8")
    assert "## Summary" in content
    assert "mock opening line" in content
    assert "segment_id: " in content


def test_relate_from_qmd_creates_relationships(tmp_path, run_ave):
    project = _project_with_asset(tmp_path, run_ave, clips=2)
    run_ave("semantic-analyze", tmp_path / "project", "--provider", "mock", "--json")
    export_cards(project)

    with connect_db(project.db_path) as conn:
        segment_ids = [row["id"] for row in conn.execute("select id from segments order by id")]
    assert len(segment_ids) == 2

    def fake_runner(args):
        assert args[0] == "vsearch"
        # every query "finds" all cards with high similarity; self-matches are skipped
        return json.dumps(
            [{"file": f"qmd://ave-test/{sid}.md", "score": 0.9} for sid in segment_ids]
        )

    summary = relate_from_qmd(project, collection="ave-test", runner=fake_runner)

    assert summary.segments_queried == 2
    assert summary.relationships_created == 1  # deduped pair
    with connect_db(project.db_path) as conn:
        rel = conn.execute("select relationship_type, confidence, source from relationships where source = 'qmd'").fetchone()
    assert rel["relationship_type"] == "duplicates"
    assert rel["confidence"] == 0.9

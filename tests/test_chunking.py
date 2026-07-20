import json
import sqlite3

from helpers import make_mp4

from agentic_video_editor.chunking import (
    ChunkSpec,
    VideoUnit,
    chunk_plan,
    generate_chunks,
    generate_chunks_for_project,
    remap_flat_items,
    remap_semantic_payload,
)
from agentic_video_editor.facets import facet_analyze_project
from agentic_video_editor.project import load_project
from agentic_video_editor.semantics import semantic_analyze_project
from agentic_video_editor.transcript import transcribe_project


def _json(result):
    return json.loads(result.stdout)


def _make_project(tmp_path, run_ave, *, seconds=1):
    project_dir = tmp_path / "project"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    make_mp4(source_dir / "clip_0.mp4", seconds=seconds)
    run_ave("init", project_dir)
    run_ave("ingest", project_dir, source_dir)
    return project_dir


def _rows(project_dir, query, params=()):
    db_path = load_project(project_dir).db_path
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(query, params).fetchall()]


# ---------------------------------------------------------------------------
# chunk_plan: pure function
# ---------------------------------------------------------------------------


def test_chunk_plan_short_asset_is_a_single_no_op_chunk():
    plan = chunk_plan(600.0, chunk_length_sec=900.0, min_asset_sec=1200.0)
    assert plan == [ChunkSpec(index=0, start_sec=0.0, end_sec=600.0)]


def test_chunk_plan_splits_contiguous_non_overlapping():
    plan = chunk_plan(2000.0, chunk_length_sec=900.0, min_asset_sec=1200.0)
    assert [spec.index for spec in plan] == [0, 1, 2]
    assert plan[0].start_sec == 0.0 and plan[0].end_sec == 900.0
    assert plan[1].start_sec == 900.0 and plan[1].end_sec == 1800.0
    assert plan[2].start_sec == 1800.0 and plan[2].end_sec == 2000.0
    # contiguous: each chunk's end is the next chunk's start, no gaps or overlap
    for previous, current in zip(plan, plan[1:]):
        assert previous.end_sec == current.start_sec


def test_chunk_plan_folds_a_near_zero_trailing_chunk_into_the_previous_one():
    # 1800.1s at a 900s chunk length would naively produce a 0.1s third chunk
    plan = chunk_plan(1800.1, chunk_length_sec=900.0, min_asset_sec=1200.0)
    assert len(plan) == 2
    assert plan[-1].end_sec == 1800.1


def test_chunk_plan_zero_or_missing_duration_is_a_single_zero_length_chunk():
    assert chunk_plan(None, min_asset_sec=1200.0) == [ChunkSpec(index=0, start_sec=0.0, end_sec=0.0)]
    assert chunk_plan(0.0, min_asset_sec=1200.0) == [ChunkSpec(index=0, start_sec=0.0, end_sec=0.0)]


# ---------------------------------------------------------------------------
# generate_chunks: real ffmpeg extraction
# ---------------------------------------------------------------------------


def test_generate_chunks_produces_expected_file_count_and_durations(tmp_path):
    source = tmp_path / "source.mp4"
    make_mp4(source, seconds=6)
    plan = chunk_plan(6.0, chunk_length_sec=2.0, min_asset_sec=3.0)
    assert len(plan) == 3

    output_dir = tmp_path / "chunks"
    paths = generate_chunks(source, plan, output_dir)

    assert len(paths) == 3
    from agentic_video_editor.media import probe_media

    for path, spec in zip(paths, plan):
        assert path.exists()
        probe = probe_media(path)
        assert probe.status == "ok"
        duration = probe.metadata.get("duration_sec")
        assert duration is not None
        assert abs(duration - spec.duration_sec) < 0.5


def test_generate_chunks_single_spec_is_a_no_op_returning_the_source(tmp_path):
    source = tmp_path / "source.mp4"
    make_mp4(source, seconds=1)
    plan = chunk_plan(1.0, min_asset_sec=1200.0)
    paths = generate_chunks(source, plan, tmp_path / "chunks")
    assert paths == [source]


# ---------------------------------------------------------------------------
# remap_flat_items / remap_semantic_payload: pure function correctness
# ---------------------------------------------------------------------------


def test_remap_flat_items_offsets_and_clamps_to_chunk_duration():
    unit = VideoUnit(path=None, index=1, offset_sec=900.0, duration_sec=900.0)
    items = [{"start_sec": 0, "end_sec": 10, "text": "a"}, {"start_sec": 895, "end_sec": 950, "text": "overruns"}]
    remapped = remap_flat_items(items, unit)
    assert remapped[0]["start_sec"] == 900.0
    assert remapped[0]["end_sec"] == 910.0
    # the second item claims a span past this chunk's own duration (950 > 900);
    # it must be clamped to the chunk's real end before the offset is added
    assert remapped[1]["end_sec"] == 900.0 + 900.0


def test_remap_semantic_payload_shifts_relationship_indices_and_word_units():
    unit = VideoUnit(path=None, index=1, offset_sec=100.0, duration_sec=200.0)
    payload = {
        "segments": [
            {"start_sec": 0, "end_sec": 5, "summary": "a", "word_units": [{"text": "hi", "start_sec": 1, "end_sec": 2}]},
            {"start_sec": 6, "end_sec": 9, "summary": "b"},
        ],
        "relationships": [{"from_index": 0, "to_index": 1, "relationship_type": "sets_up", "evidence": "x"}],
    }
    segments, relationships, next_offset = remap_semantic_payload(payload, unit, index_offset=3)
    assert segments[0]["start_sec"] == 100.0 and segments[0]["end_sec"] == 105.0
    assert segments[0]["word_units"][0]["start_sec"] == 101.0
    assert segments[1]["start_sec"] == 106.0
    # indices are shifted by the running offset (3), not left pointing at
    # positions within just this chunk's own segment list
    assert relationships[0]["from_index"] == 3
    assert relationships[0]["to_index"] == 4
    assert next_offset == 5


# ---------------------------------------------------------------------------
# End-to-end: chunked assets get absolute timestamps; unchunked is unchanged
# ---------------------------------------------------------------------------


def test_facets_on_chunked_asset_store_absolute_not_chunk_relative_timestamps(tmp_path, run_ave):
    project_dir = _make_project(tmp_path, run_ave, seconds=6)
    project = load_project(project_dir)

    chunk_summary = generate_chunks_for_project(project, chunk_length_sec=2.0, min_asset_sec=3.0)
    assert chunk_summary["assets_completed"] == 1
    assert chunk_summary["chunks_created"] == 3

    chunk_rows = _rows(
        project_dir,
        "select chunk_index, start_sec, end_sec from media_artifacts where artifact_type='chunk' order by chunk_index",
    )
    assert [row["chunk_index"] for row in chunk_rows] == [0, 1, 2]

    facet_analyze_project(project, provider_name="mock", only=["actions_events"])

    observations = _rows(
        project_dir,
        "select start_sec, end_sec from observations where observation_type='actions_events' order by start_sec",
    )
    assert observations, "expected at least one observation"
    # evidence from later chunks must land later on the absolute timeline -
    # if remapping were broken (chunk-relative timestamps stored as-is),
    # every chunk's observations would cluster in the same 0-2s window
    max_start = max(row["start_sec"] for row in observations)
    assert max_start >= chunk_rows[-1]["start_sec"]


def test_transcribe_on_chunked_asset_spans_the_full_absolute_timeline(tmp_path, run_ave):
    project_dir = _make_project(tmp_path, run_ave, seconds=6)
    project = load_project(project_dir)
    generate_chunks_for_project(project, chunk_length_sec=2.0, min_asset_sec=3.0)

    transcribe_project(project, provider_name="mock")

    spans = _rows(project_dir, "select start_sec, end_sec from transcript_spans order by start_sec")
    assert spans
    # the mock always returns a 0-10s span per call; three chunks means three
    # remapped spans that must NOT all be stacked at 0-10s if offsetting works
    starts = {row["start_sec"] for row in spans}
    assert len(starts) == 3
    assert max(row["end_sec"] for row in spans) > 6.0 - 1e-6 or max(starts) >= 4.0


def test_semantic_analyze_on_chunked_asset_remaps_segments_and_relationships(tmp_path, run_ave):
    project_dir = _make_project(tmp_path, run_ave, seconds=6)
    project = load_project(project_dir)
    generate_chunks_for_project(project, chunk_length_sec=2.0, min_asset_sec=3.0)

    semantic_analyze_project(project, provider_name="mock")

    segments = _rows(project_dir, "select id, start_sec, end_sec from segments order by start_sec")
    assert segments
    starts = {row["start_sec"] for row in segments}
    assert len(starts) == 3, "each chunk's segment(s) should land in a distinct absolute window"


def test_chunks_limit_makes_progress_across_repeated_resumed_runs(tmp_path, run_ave):
    """Same lesson as facets.py (17b1bdf) and proxy.py: --limit smaller than the
    eligible count must advance through the backlog, not re-select the same
    already-chunked assets forever."""
    project_dir = tmp_path / "project"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    for index in range(3):
        make_mp4(source_dir / f"clip_{index}.mp4", seconds=6)
    run_ave("init", project_dir)
    run_ave("ingest", project_dir, source_dir)
    project = load_project(project_dir)

    for _ in range(3):
        generate_chunks_for_project(project, chunk_length_sec=2.0, min_asset_sec=3.0, limit=1)

    rows = _rows(
        project_dir,
        "select asset_id, count(*) as n from media_artifacts where artifact_type='chunk' group by asset_id",
    )
    assert len(rows) == 3
    assert all(row["n"] == 3 for row in rows)


def test_unchunked_asset_facets_behavior_is_unchanged(tmp_path, run_ave):
    project_dir = _make_project(tmp_path, run_ave, seconds=1)
    project = load_project(project_dir)

    # no chunks generated: asset duration (1s) is far under any chunking threshold
    summary = facet_analyze_project(project, provider_name="mock", only=["actions_events"])
    assert summary.assets_completed == 1
    assert summary.facets_run == 1

    observations = _rows(
        project_dir,
        "select start_sec, end_sec from observations where observation_type='actions_events'",
    )
    # the mock canned payload for actions_events has two entries regardless
    # of chunking; an unchunked asset must see the same count as before
    # chunking machinery existed (only the pre-existing duration clamp in
    # _insert_observations applies, nothing chunk-related)
    assert len(observations) == 2

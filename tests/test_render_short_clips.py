import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentic_video_editor.db import connect_db, migrate  # noqa: E402
from agentic_video_editor.ingest import ingest_paths  # noqa: E402
from agentic_video_editor.project import init_project, utc_now  # noqa: E402
from agentic_video_editor.render import (  # noqa: E402
    _flat_joined_command,
    _joined_command,
    _legacy_chain_safe,
    _resolve_joins,
    render_timeline,
)

from helpers import make_mp4  # noqa: E402


def _crossfades(count, duration=0.35):
    return [{"type": "crossfade", "duration_sec": duration} for _ in range(count)]


def test_resolve_joins_shortens_fades_short_clips_cannot_afford():
    joins, decisions = _resolve_joins(_crossfades(3), [0.5, 0.5, 0.5, 0.5])
    assert all(join["type"] == "crossfade" for join in joins)
    assert all(join["duration_sec"] < 0.35 for join in joins)
    assert [d["action"] for d in decisions] == ["shortened"] * 3
    # every middle clip keeps its minimum of un-faded content
    for index in (1, 2):
        fade_in = joins[index - 1]["duration_sec"]
        fade_out = joins[index]["duration_sec"]
        assert fade_in + fade_out <= 0.5 - 0.1 + 1e-6


def test_resolve_joins_downgrades_to_cut_below_floor():
    joins, decisions = _resolve_joins(_crossfades(2), [0.5, 0.2, 0.5])
    assert joins[1] == {"type": "cut"}
    actions = {d["join_index"]: d["action"] for d in decisions}
    assert actions[1] == "downgraded_to_cut"
    assert all("why" in d and d["why"] for d in decisions)


def test_resolve_joins_leaves_affordable_fades_untouched():
    requested = _crossfades(2)
    durations = [4.0, 4.0, 4.0]
    joins, decisions = _resolve_joins(requested, durations)
    assert decisions == []
    assert joins == requested
    # and the legacy graph is byte-identical to the pre-fix one
    paths = [Path(f"/tmp/clip{i}.mp4") for i in range(3)]
    out = Path("/tmp/out.mp4")
    assert _joined_command(paths, durations, joins, out) == _joined_command(
        paths, durations, requested, out
    )


def test_legacy_chain_safe_envelope():
    short = [0.5] * 8
    long = [4.0] * 8
    assert _legacy_chain_safe(short, _crossfades(3) + [{"type": "cut"}] * 4)
    assert not _legacy_chain_safe(short, _crossfades(7))
    assert _legacy_chain_safe(long, _crossfades(7))


def test_flat_joined_command_builds_unchained_audio():
    paths = [Path(f"/tmp/clip{i}.mp4") for i in range(6)]
    joins, _ = _resolve_joins(_crossfades(5), [0.5] * 6)
    command = _flat_joined_command(paths, [0.5] * 6, joins, Path("/tmp/out.mp4"))
    graph = command[command.index("-filter_complex") + 1]
    assert "acrossfade" not in graph
    assert graph.count("xfade") == 5
    assert "amix=inputs=6:normalize=0" in graph
    assert graph.count("afade=t=in") == 5
    assert graph.count("afade=t=out") == 5
    assert graph.count("adelay") == 5  # every clip after the first is delayed


def _timeline_of_short_clips(project, asset_id, count=6, clip_sec=0.5):
    timeline_id = f"timeline_{uuid.uuid4().hex[:12]}"
    now = utc_now()
    with connect_db(project.db_path) as conn:
        migrate(conn)
        conn.execute(
            """
            insert into timelines (
                id, project_id, directive_id, name, duration_target_sec,
                timeline_json, status, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (timeline_id, "default", None, "short clips", count * clip_sec, "{}", "ready", now),
        )
        for index in range(count):
            transition = (
                json.dumps({"type": "crossfade", "duration_sec": 0.35, "why": "test"})
                if index > 0
                else None
            )
            conn.execute(
                """
                insert into timeline_items (
                    id, project_id, timeline_id, track_kind, track_name, asset_id,
                    segment_id, source_start_sec, source_end_sec, timeline_start_sec,
                    timeline_end_sec, role, reason, transition_json, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"item_{index}",
                    "default",
                    timeline_id,
                    "video",
                    "A-roll",
                    asset_id,
                    None,
                    index * clip_sec,
                    (index + 1) * clip_sec,
                    index * clip_sec,
                    (index + 1) * clip_sec,
                    "beat",
                    "test clip",
                    transition,
                    now,
                ),
            )
    return timeline_id


def test_render_survives_sub_second_crossfade_chain(tmp_path):
    """Regression: 6 half-second clips with crossfades everywhere used to make
    the chained xfade+acrossfade graph starve ffmpeg's scheduler (no audio
    packets, exit 234)."""
    source = make_mp4(tmp_path / "source.mp4", seconds=4)
    project = init_project(tmp_path / "project")
    ingest_paths(project, [source])
    with connect_db(project.db_path) as conn:
        migrate(conn)
        asset_id = conn.execute("select id from assets").fetchone()["id"]

    timeline_id = _timeline_of_short_clips(project, asset_id)
    summary = render_timeline(project, timeline_id=timeline_id)
    assert summary.status == "complete"
    assert Path(summary.path).exists()

    with connect_db(project.db_path) as conn:
        row = conn.execute(
            "select report_json from renders where id = ?", (summary.render_id,)
        ).fetchone()
    report = json.loads(row["report_json"])
    decisions = report["join_decisions"]
    assert decisions, "downgrade notes must be recorded for unaffordable fades"
    assert all(d["action"] in {"shortened", "downgraded_to_cut"} for d in decisions)
    assert all(d.get("why") for d in decisions)

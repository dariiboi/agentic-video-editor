from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .align import align_project, align_summary
from .analyze import analyze_project, summarize_local_analysis
from .context import build_editorial_context, context_summary
from .critique import critique_render, review_summary
from .cutpoints import cut_point_summary, detect_cut_points
from .gemini_provider import DEFAULT_MODEL
from .ingest import ingest_paths, list_assets
from .planner import create_edit_plan
from .project import init_project, load_project
from .qmd_bridge import export_cards, relate_from_qmd
from .render import render_summary, render_timeline
from .retrieval import context_search, related_segments
from .semantics import search_segments, semantic_analyze_project, semantic_summary
from .timeline import create_timeline, get_timeline, timeline_summary
from .transcript import search_transcripts, transcribe_project, transcript_summary


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return args.func(args)
    except BrokenPipeError:
        return 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ave", description="Agentic Video Editor")
    subcommands = parser.add_subparsers(dest="command", required=True)

    init_parser = subcommands.add_parser("init", help="Create or update an AVE project")
    init_parser.add_argument("project_dir", type=Path)
    init_parser.add_argument("--name", help="Project display name")
    init_parser.set_defaults(func=_init_command)

    ingest_parser = subcommands.add_parser("ingest", help="Ingest media paths into a project")
    ingest_parser.add_argument("project_dir", type=Path)
    ingest_parser.add_argument("paths", nargs="+", type=Path)
    ingest_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    ingest_parser.set_defaults(func=_ingest_command)

    assets_parser = subcommands.add_parser("assets", help="List project assets")
    assets_parser.add_argument("project_dir", type=Path)
    assets_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    assets_parser.set_defaults(func=_assets_command)

    analyze_parser = subcommands.add_parser("analyze", help="Run cheap local analysis")
    analyze_parser.add_argument("project_dir", type=Path)
    analyze_parser.add_argument("--window-sec", type=float, default=30.0)
    analyze_parser.add_argument("--silence-noise-db", type=float, default=-35.0)
    analyze_parser.add_argument("--silence-duration-sec", type=float, default=0.5)
    analyze_parser.add_argument("--limit", type=int)
    analyze_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    analyze_parser.set_defaults(func=_analyze_command)

    align_parser = subcommands.add_parser("align", help="Word-align speech locally (faster-whisper) into spans + cut points")
    align_parser.add_argument("project_dir", type=Path)
    align_parser.add_argument("--model", default="small.en", help="faster-whisper model size (tiny.en, base.en, small.en, ...)")
    align_parser.add_argument("--limit", type=int)
    align_parser.add_argument("--force", action="store_true", help="Re-align assets that already have local ASR spans")
    align_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    align_parser.set_defaults(func=_align_command)

    align_summary_parser = subcommands.add_parser("align-summary", help="Summarize local ASR alignment")
    align_summary_parser.add_argument("project_dir", type=Path)
    align_summary_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    align_summary_parser.set_defaults(func=_align_summary_command)

    cutpoints_parser = subcommands.add_parser("cutpoints", help="Detect frame-accurate cut points (shot changes + audio gaps)")
    cutpoints_parser.add_argument("project_dir", type=Path)
    cutpoints_parser.add_argument("--scene-threshold", type=float, default=0.3)
    cutpoints_parser.add_argument("--gap-noise-db", type=float, default=-30.0)
    cutpoints_parser.add_argument("--gap-min-sec", type=float, default=0.12)
    cutpoints_parser.add_argument("--limit", type=int)
    cutpoints_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    cutpoints_parser.set_defaults(func=_cutpoints_command)

    cutpoints_summary_parser = subcommands.add_parser("cutpoints-summary", help="Summarize detected cut points")
    cutpoints_summary_parser.add_argument("project_dir", type=Path)
    cutpoints_summary_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    cutpoints_summary_parser.set_defaults(func=_cutpoints_summary_command)

    summary_parser = subcommands.add_parser("analysis-summary", help="Summarize local analysis")
    summary_parser.add_argument("project_dir", type=Path)
    summary_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    summary_parser.set_defaults(func=_analysis_summary_command)

    transcribe_parser = subcommands.add_parser("transcribe", help="Create transcript/search spans")
    transcribe_parser.add_argument("project_dir", type=Path)
    transcribe_parser.add_argument("--provider", default="gemini", choices=["gemini", "mock"])
    transcribe_parser.add_argument("--model", default=DEFAULT_MODEL)
    transcribe_parser.add_argument("--env-path", type=Path, default=Path(".gemini_api.env"))
    transcribe_parser.add_argument("--limit", type=int)
    transcribe_parser.add_argument("--force", action="store_true", help="Re-analyze assets that already have spans")
    transcribe_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    transcribe_parser.set_defaults(func=_transcribe_command)

    transcript_search_parser = subcommands.add_parser("transcript-search", help="Search transcript spans")
    transcript_search_parser.add_argument("project_dir", type=Path)
    transcript_search_parser.add_argument("query")
    transcript_search_parser.add_argument("--limit", type=int, default=10)
    transcript_search_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    transcript_search_parser.set_defaults(func=_transcript_search_command)

    transcript_summary_parser = subcommands.add_parser("transcript-summary", help="Summarize transcript spans")
    transcript_summary_parser.add_argument("project_dir", type=Path)
    transcript_summary_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    transcript_summary_parser.set_defaults(func=_transcript_summary_command)

    semantic_parser = subcommands.add_parser("semantic-analyze", help="Create semantic segments/selects")
    semantic_parser.add_argument("project_dir", type=Path)
    semantic_parser.add_argument("--provider", default="gemini", choices=["gemini", "mock"])
    semantic_parser.add_argument("--model", default=DEFAULT_MODEL)
    semantic_parser.add_argument("--env-path", type=Path, default=Path(".gemini_api.env"))
    semantic_parser.add_argument("--limit", type=int)
    semantic_parser.add_argument("--force", action="store_true", help="Re-analyze assets that already have segments")
    semantic_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    semantic_parser.set_defaults(func=_semantic_analyze_command)

    segment_search_parser = subcommands.add_parser("segment-search", help="Search semantic segments")
    segment_search_parser.add_argument("project_dir", type=Path)
    segment_search_parser.add_argument("query")
    segment_search_parser.add_argument("--limit", type=int, default=10)
    segment_search_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    segment_search_parser.set_defaults(func=_segment_search_command)

    semantic_summary_parser = subcommands.add_parser("semantic-summary", help="Summarize semantic analysis")
    semantic_summary_parser.add_argument("project_dir", type=Path)
    semantic_summary_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    semantic_summary_parser.set_defaults(func=_semantic_summary_command)

    context_build_parser = subcommands.add_parser("context-build", help="Build editorial context cards")
    context_build_parser.add_argument("project_dir", type=Path)
    context_build_parser.add_argument("--provider", default="mock", choices=["gemini", "mock"])
    context_build_parser.add_argument("--model", default=DEFAULT_MODEL)
    context_build_parser.add_argument("--env-path", type=Path, default=Path(".gemini_api.env"))
    context_build_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    context_build_parser.set_defaults(func=_context_build_command)

    context_summary_parser = subcommands.add_parser("context-summary", help="Summarize editorial context")
    context_summary_parser.add_argument("project_dir", type=Path)
    context_summary_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    context_summary_parser.set_defaults(func=_context_summary_command)

    context_search_parser = subcommands.add_parser("context-search", help="Search with context-aware retrieval")
    context_search_parser.add_argument("project_dir", type=Path)
    context_search_parser.add_argument("query")
    context_search_parser.add_argument("--limit", type=int, default=10)
    context_search_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    context_search_parser.set_defaults(func=_context_search_command)

    export_cards_parser = subcommands.add_parser("export-cards", help="Export per-segment markdown cards for qmd indexing")
    export_cards_parser.add_argument("project_dir", type=Path)
    export_cards_parser.add_argument("--out-dir", type=Path, help="Override the default PROJECT/qmd_cards directory")
    export_cards_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    export_cards_parser.set_defaults(func=_export_cards_command)

    relate_parser = subcommands.add_parser("relate", help="Mine cross-clip relationships via qmd vector search")
    relate_parser.add_argument("project_dir", type=Path)
    relate_parser.add_argument("--collection", required=True, help="qmd collection name that indexes the exported cards")
    relate_parser.add_argument("--top-k", type=int, default=8)
    relate_parser.add_argument("--min-score", type=float, default=0.6)
    relate_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    relate_parser.set_defaults(func=_relate_command)

    related_parser = subcommands.add_parser("related", help="Show related segments")
    related_parser.add_argument("project_dir", type=Path)
    related_parser.add_argument("segment_id")
    related_parser.add_argument("--limit", type=int, default=10)
    related_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    related_parser.set_defaults(func=_related_command)

    edit_plan_parser = subcommands.add_parser("edit-plan", help="Plan a context-aware rough cut")
    edit_plan_parser.add_argument("project_dir", type=Path)
    edit_plan_parser.add_argument("--directive", required=True)
    edit_plan_parser.add_argument("--duration-sec", type=float, default=60.0)
    edit_plan_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    edit_plan_parser.set_defaults(func=_edit_plan_command)

    timeline_parser = subcommands.add_parser("timeline", help="Create a simple rough-cut timeline")
    timeline_parser.add_argument("project_dir", type=Path)
    timeline_parser.add_argument("--directive", required=True)
    timeline_parser.add_argument("--duration-sec", type=float, default=60.0)
    timeline_parser.add_argument("--query")
    timeline_parser.add_argument("--max-clip-sec", type=float, default=12.0)
    timeline_parser.add_argument("--context-aware", action="store_true", help="Use editorial context planning")
    timeline_parser.add_argument(
        "--snap-tolerance-sec",
        type=float,
        default=1.0,
        help="Snap trim points to detected cut points within this window (0 disables)",
    )
    timeline_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    timeline_parser.set_defaults(func=_timeline_command)

    timelines_parser = subcommands.add_parser("timelines", help="List timelines")
    timelines_parser.add_argument("project_dir", type=Path)
    timelines_parser.add_argument("--timeline-id", default="latest")
    timelines_parser.add_argument("--show", action="store_true")
    timelines_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    timelines_parser.set_defaults(func=_timelines_command)

    render_parser = subcommands.add_parser("render", help="Render a timeline to MP4")
    render_parser.add_argument("project_dir", type=Path)
    render_parser.add_argument("--timeline-id", default="latest")
    render_parser.add_argument(
        "--crossfade-sec",
        type=float,
        default=0.0,
        help="Force a crossfade on every join, overriding the timeline's per-join transition decisions",
    )
    render_parser.add_argument("--burn-captions", action="store_true", help="Burn caption_text into the video")
    render_parser.add_argument("--no-loudnorm", action="store_true", help="Skip the final loudness-normalization pass")
    render_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    render_parser.set_defaults(func=_render_command)

    renders_parser = subcommands.add_parser("renders", help="List renders")
    renders_parser.add_argument("project_dir", type=Path)
    renders_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    renders_parser.set_defaults(func=_renders_command)

    critique_parser = subcommands.add_parser("critique", help="Critique a rendered rough cut")
    critique_parser.add_argument("project_dir", type=Path)
    critique_parser.add_argument("--render-id", default="latest")
    critique_parser.add_argument("--directive")
    critique_parser.add_argument("--provider", default="gemini", choices=["gemini", "mock"])
    critique_parser.add_argument("--model", default=DEFAULT_MODEL)
    critique_parser.add_argument("--env-path", type=Path, default=Path(".gemini_api.env"))
    critique_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    critique_parser.set_defaults(func=_critique_command)

    reviews_parser = subcommands.add_parser("reviews", help="List critiques/reviews")
    reviews_parser.add_argument("project_dir", type=Path)
    reviews_parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    reviews_parser.set_defaults(func=_reviews_command)

    return parser


def _init_command(args: argparse.Namespace) -> int:
    project = init_project(args.project_dir, name=args.name)
    print(f"Initialized AVE project: {project.root}")
    print(f"Database: {project.db_path}")
    return 0


def _ingest_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    summary = ingest_paths(project, args.paths)
    data = {
        "run_id": summary.run_id,
        "files_found": summary.files_found,
        "assets_created": summary.assets_created,
        "assets_updated": summary.assets_updated,
        "assets_unchanged": summary.assets_unchanged,
    }
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(f"Ingest run: {summary.run_id}")
        print(f"Files found: {summary.files_found}")
        print(f"Created: {summary.assets_created}")
        print(f"Updated: {summary.assets_updated}")
        print(f"Unchanged: {summary.assets_unchanged}")
    return 0


def _assets_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    assets = list_assets(project)
    if args.json:
        print(json.dumps({"assets": assets}, indent=2))
        return 0

    if not assets:
        print("No assets ingested.")
        return 0

    for asset in assets:
        duration = _format_duration(asset["duration_sec"])
        resolution = _format_resolution(asset["width"], asset["height"])
        audio = "audio" if asset["has_audio"] else "no-audio"
        codec = asset["video_codec"] or asset["audio_codec"] or "unknown-codec"
        print(
            f"{asset['id']}  {duration:>10}  {resolution:>10}  "
            f"{codec:<14}  {audio:<8}  {asset['ingest_status']}  {asset['path']}"
        )
    return 0


def _analyze_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    summary = analyze_project(
        project,
        window_sec=args.window_sec,
        silence_noise_db=args.silence_noise_db,
        silence_duration_sec=args.silence_duration_sec,
        limit=args.limit,
    )
    data = {
        "run_id": summary.run_id,
        "assets_requested": summary.assets_requested,
        "assets_completed": summary.assets_completed,
        "windows_created": summary.windows_created,
        "labels_created": summary.labels_created,
        "artifacts_created": summary.artifacts_created,
    }
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(f"Analysis run: {summary.run_id}")
        print(f"Assets requested: {summary.assets_requested}")
        print(f"Assets completed: {summary.assets_completed}")
        print(f"Windows created: {summary.windows_created}")
        print(f"Labels created: {summary.labels_created}")
        print(f"Artifacts created: {summary.artifacts_created}")
    return 0


def _analysis_summary_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    summary = summarize_local_analysis(project)
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    print(f"Assets: {summary['assets']}")
    print(f"Windows: {summary['windows']}")
    print("Labels:")
    for label, count in summary["labels"].items():
        print(f"  {label}: {count}")
    print("Artifacts:")
    for artifact_type, count in summary["artifacts"].items():
        print(f"  {artifact_type}: {count}")
    return 0


def _align_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    summary = align_project(
        project,
        model_size=args.model,
        limit=args.limit,
        force=args.force,
    )
    data = {
        "assets_requested": summary.assets_requested,
        "assets_completed": summary.assets_completed,
        "spans_created": summary.spans_created,
        "words_created": summary.words_created,
        "cut_points_created": summary.cut_points_created,
    }
    return _print_json_or_lines(args.json, data, "Align")


def _align_summary_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    data = align_summary(project)
    return _print_json_or_lines(args.json, data, "Align summary")


def _cutpoints_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    summary = detect_cut_points(
        project,
        scene_threshold=args.scene_threshold,
        gap_noise_db=args.gap_noise_db,
        gap_min_sec=args.gap_min_sec,
        limit=args.limit,
    )
    data = {
        "assets_requested": summary.assets_requested,
        "assets_completed": summary.assets_completed,
        "scene_points": summary.scene_points,
        "audio_gap_points": summary.audio_gap_points,
    }
    return _print_json_or_lines(args.json, data, "Cut points")


def _cutpoints_summary_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    data = cut_point_summary(project)
    print(json.dumps(data, indent=2))
    return 0


def _transcribe_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    summary = transcribe_project(
        project,
        provider_name=args.provider,
        model=args.model,
        env_path=args.env_path,
        limit=args.limit,
        force=args.force,
    )
    data = {
        "assets_requested": summary.assets_requested,
        "assets_completed": summary.assets_completed,
        "spans_created": summary.spans_created,
    }
    return _print_json_or_lines(args.json, data, "Transcript")


def _transcript_search_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    results = search_transcripts(project, args.query, limit=args.limit)
    if args.json:
        print(json.dumps({"results": results}, indent=2))
    else:
        for result in results:
            print(f"{result['file_name']} {result['start_sec']:.1f}-{result['end_sec']:.1f}: {result['text']}")
    return 0


def _transcript_summary_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    summary = transcript_summary(project)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"Spans: {summary['spans']}")
        for kind, count in summary["by_kind"].items():
            print(f"  {kind}: {count}")
    return 0


def _semantic_analyze_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    summary = semantic_analyze_project(
        project,
        provider_name=args.provider,
        model=args.model,
        env_path=args.env_path,
        limit=args.limit,
        force=args.force,
    )
    data = {
        "assets_requested": summary.assets_requested,
        "assets_completed": summary.assets_completed,
        "segments_created": summary.segments_created,
        "selects_created": summary.selects_created,
        "relationships_created": summary.relationships_created,
    }
    return _print_json_or_lines(args.json, data, "Semantic")


def _segment_search_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    results = search_segments(project, args.query, limit=args.limit)
    if args.json:
        print(json.dumps({"results": results}, indent=2))
    else:
        for result in results:
            print(f"{result['file_name']} {result['start_sec']:.1f}-{result['end_sec']:.1f}: {result['summary']}")
    return 0


def _semantic_summary_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    summary = semantic_summary(project)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"Segments: {summary['segments']}")
        print(f"Selects: {summary['selects']}")
        print(f"Relationships: {summary['relationships']}")
    return 0


def _context_build_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    summary = build_editorial_context(
        project,
        provider_name=args.provider,
        model=args.model,
        env_path=args.env_path,
    )
    data = {
        "source": summary.source,
        "segments_seen": summary.segments_seen,
        "collection_summaries": summary.collection_summaries,
        "material_bank_items": summary.material_bank_items,
        "context_cards": summary.context_cards,
        "caption_options": summary.caption_options,
    }
    return _print_json_or_lines(args.json, data, "Context")


def _context_summary_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    data = context_summary(project)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(json.dumps(data, indent=2))
    return 0


def _context_search_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    data = context_search(project, args.query, limit=args.limit)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        for packet in data["packets"]:
            print(f"{packet['file_name']} {packet['time_range'][0]:.1f}-{packet['time_range'][1]:.1f}:")
            print(f"  why: {'; '.join(packet['why_matches'])}")
            if packet["warnings"]:
                print(f"  warnings: {', '.join(packet['warnings'])}")
    return 0


def _export_cards_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    summary = export_cards(project, out_dir=args.out_dir)
    data = {"cards_written": summary.cards_written, "cards_dir": summary.cards_dir}
    return _print_json_or_lines(args.json, data, "Export cards")


def _relate_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    summary = relate_from_qmd(
        project,
        collection=args.collection,
        top_k=args.top_k,
        min_score=args.min_score,
    )
    data = {
        "segments_queried": summary.segments_queried,
        "relationships_created": summary.relationships_created,
    }
    return _print_json_or_lines(args.json, data, "Relate")


def _related_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    data = related_segments(project, args.segment_id, limit=args.limit)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        for item in data["related"]:
            print(f"{item['segment_id']} {item['relationship_type']}: {item['summary']}")
    return 0


def _edit_plan_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    data = create_edit_plan(project, directive=args.directive, duration_sec=args.duration_sec)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(f"Edit plan: {data['plan_id']}")
        for item in data["selected_sequence"]:
            print(f"  {item['timeline_start_sec']:.1f}-{item['timeline_end_sec']:.1f} {item['beat_role']}: {item['why_here']}")
    return 0


def _timeline_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    summary = create_timeline(
        project,
        directive=args.directive,
        duration_sec=args.duration_sec,
        query=args.query,
        max_clip_sec=args.max_clip_sec,
        context_aware=args.context_aware,
        snap_tolerance_sec=args.snap_tolerance_sec,
    )
    data = {
        "timeline_id": summary.timeline_id,
        "directive_id": summary.directive_id,
        "items_created": summary.items_created,
        "duration_sec": summary.duration_sec,
    }
    return _print_json_or_lines(args.json, data, "Timeline")


def _timelines_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    data = get_timeline(project, args.timeline_id) if args.show else timeline_summary(project)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(json.dumps(data, indent=2))
    return 0


def _render_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    summary = render_timeline(
        project,
        timeline_id=args.timeline_id,
        crossfade_sec=args.crossfade_sec,
        burn_captions=args.burn_captions,
        normalize_loudness=not args.no_loudnorm,
    )
    data = {
        "render_id": summary.render_id,
        "timeline_id": summary.timeline_id,
        "path": summary.path,
        "status": summary.status,
        "clips_rendered": summary.clips_rendered,
    }
    return _print_json_or_lines(args.json, data, "Render")


def _renders_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    data = render_summary(project)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(json.dumps(data, indent=2))
    return 0


def _critique_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    summary = critique_render(
        project,
        render_id=args.render_id,
        directive=args.directive,
        provider_name=args.provider,
        model=args.model,
        env_path=args.env_path,
    )
    data = {"review_id": summary.review_id, "render_id": summary.render_id, "source": summary.source}
    return _print_json_or_lines(args.json, data, "Critique")


def _reviews_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    data = review_summary(project)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(json.dumps(data, indent=2))
    return 0


def _print_json_or_lines(as_json: bool, data: dict[str, object], label: str) -> int:
    if as_json:
        print(json.dumps(data, indent=2))
    else:
        print(f"{label}:")
        for key, value in data.items():
            print(f"  {key}: {value}")
    return 0


def _format_duration(value: object) -> str:
    if not isinstance(value, int | float):
        return "--"
    total = int(round(float(value)))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _format_resolution(width: object, height: object) -> str:
    if isinstance(width, int) and isinstance(height, int):
        return f"{width}x{height}"
    return "--"


if __name__ == "__main__":
    raise SystemExit(main())

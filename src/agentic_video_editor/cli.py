from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .analyze import analyze_project, summarize_local_analysis
from .critique import critique_render, review_summary
from .gemini_provider import DEFAULT_MODEL
from .ingest import ingest_paths, list_assets
from .project import init_project, load_project
from .render import render_summary, render_timeline
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

    timeline_parser = subcommands.add_parser("timeline", help="Create a simple rough-cut timeline")
    timeline_parser.add_argument("project_dir", type=Path)
    timeline_parser.add_argument("--directive", required=True)
    timeline_parser.add_argument("--duration-sec", type=float, default=60.0)
    timeline_parser.add_argument("--query")
    timeline_parser.add_argument("--max-clip-sec", type=float, default=12.0)
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


def _timeline_command(args: argparse.Namespace) -> int:
    project = load_project(args.project_dir)
    summary = create_timeline(
        project,
        directive=args.directive,
        duration_sec=args.duration_sec,
        query=args.query,
        max_clip_sec=args.max_clip_sec,
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
    summary = render_timeline(project, timeline_id=args.timeline_id)
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

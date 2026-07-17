#!/usr/bin/env bash
# =============================================================================
# AVE end-to-end demo on the smoke_e_digglera sample project.
#
# Purpose: let a fresh session (human or agent) reproduce and inspect the full
# pipeline fast. Every stage is idempotent-ish: analysis stages skip assets
# that already have results unless --force is passed; timeline/render always
# create new rows.
#
# Prerequisites already on this machine:
#   - ffmpeg/ffprobe on PATH
#   - faster-whisper in the python env (local ASR; model downloads once)
#   - qmd CLI (~/.local/bin/qmd) for embedding-based relationship mining
#   - Gemini key in .gemini_api.env (needed for semantic analysis /
#     transcription re-runs AND the context-aware plan in step 4; add
#     "--provider mock" to the timeline command for an offline smoke run)
#
# Pipeline map (module -> what it writes):
#   ingest.py           -> assets
#   analyze.py          -> windows, activity_labels, media_artifacts (proxies)
#   cutpoints.py        -> scene_boundaries (ffmpeg_scdet + audio_gap)   [NEW]
#   align.py            -> transcript_spans(local_asr), word_alignments,
#                          scene_boundaries(asr_word)                    [NEW]
#   semantics.py        -> segments, selects, relationships (Gemini)
#   context.py          -> editorial_context_cards, caption_options
#   qmd_bridge.py       -> qmd_cards/*.md, relationships(source=qmd)     [NEW]
#   planner.py          -> edit_plans (intent -> structure -> cast sequence)
#   timeline.py         -> timelines, timeline_items (snaps to cut points) [NEW]
#   render.py           -> renders/*.mp4 (micro fades, single loudnorm,
#                          optional crossfades + burned captions)        [NEW]
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${1:-$REPO_ROOT/demo_projects/smoke_e_digglera}"
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
AVE() { python -m agentic_video_editor "$@"; }
DB="$PROJECT/library.sqlite"

echo "== 1. Frame-accurate cut points (shot changes + audio gaps) =="
AVE cutpoints "$PROJECT" --json
AVE cutpoints-summary "$PROJECT" --json

echo "== 2. Local word-level ASR alignment (faster-whisper, offline) =="
# Verbatim quotable spans + pause-adjacent word boundaries as snap targets.
# Use --model tiny.en for a fast smoke test; small.en (default) for quality.
AVE align "$PROJECT" --json
AVE align-summary "$PROJECT" --json

echo "== 3. Cross-clip relationships via qmd vector search =="
AVE export-cards "$PROJECT" --json
if command -v qmd >/dev/null && ! qmd collection list 2>/dev/null | grep -q "ave-smoke"; then
  qmd collection add "$PROJECT/qmd_cards" --name ave-smoke
fi
qmd update >/dev/null 2>&1 || true
qmd embed 2>/dev/null || true
AVE relate "$PROJECT" --collection ave-smoke --json

echo "== 4. Context-aware plan + snapped timeline =="
AVE timeline "$PROJECT" \
  --directive "make a 60 second performance montage that builds from context to an emotional payoff" \
  --duration-sec 60 --max-clip-sec 6 --context-aware --json

echo "== 5. Render (micro audio fades + one loudness pass over the timeline) =="
AVE render "$PROJECT" --timeline-id latest --json
# Variants worth comparing by eye/ear:
#   AVE render "$PROJECT" --crossfade-sec 0.4 --json     # soft transitions
#   AVE render "$PROJECT" --burn-captions --json         # context overlays

echo "== 6. Inspection one-liners =="
echo "--- cut points by detector:"
sqlite3 "$DB" "select detector, count(*) from scene_boundaries group by detector"
echo "--- verbatim ASR spans (first 5):"
sqlite3 "$DB" "select round(start_sec,1), round(end_sec,1), substr(text,1,80) from transcript_spans where source='local_asr' limit 5"
echo "--- qmd-mined relationships (top 5 by similarity):"
sqlite3 "$DB" "select relationship_type, round(confidence,2), from_entity_id, to_entity_id from relationships where source='qmd' order by confidence desc limit 5"
echo "--- latest timeline items (are the cuts snapped?):"
sqlite3 "$DB" "select round(source_start_sec,2), round(source_end_sec,2), role, substr(why_here,1,60) from timeline_items where timeline_id=(select id from timelines order by created_at desc limit 1)"
echo "--- latest render:"
sqlite3 "$DB" "select path, status, round(duration_sec,1) from renders order by created_at desc limit 1"

# =============================================================================
# Where to dig deeper:
#   docs/design/next-agentic-editing-handoff.md  - architecture + research notes
#   docs/design/phase-execution-status.md        - what each phase shipped
#   src/agentic_video_editor/cutpoints.py        - snap_range/snap_time logic
#   src/agentic_video_editor/align.py            - word boundary -> cut point rules
#   src/agentic_video_editor/qmd_bridge.py       - card format + relate heuristics
#   tests/                                       - mock-provider end-to-end flows
# Known limits (honest): segments in this demo predate the word_units prompt
# (re-run `ave semantic-analyze --force` with Gemini to refresh); whisper word
# timing on *sung* vocals is approximate - the pause filter keeps only
# boundaries next to real gaps, so worst case is fewer snap targets, not bad ones.
# =============================================================================

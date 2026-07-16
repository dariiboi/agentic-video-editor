# Phase Execution Status

Last updated: July 14, 2026

Dummy footage source:

```text
/Users/dariusshaoul/Movies/smoke_e_digglera
```

Local AVE project:

```text
/Users/dariusshaoul/Documents/agentic-video-editor/demo_projects/smoke_e_digglera
```

Target runtime:

```text
/opt/anaconda3/bin/python3.12
```

## Concrete Event Series

### Phase 0: Skeleton

Status: complete for the first working slice.

Commands run:

```bash
PYTHONPATH=src /opt/anaconda3/bin/python3.12 -m agentic_video_editor init \
  /Users/dariusshaoul/Documents/agentic-video-editor/demo_projects/smoke_e_digglera \
  --name smoke_e_digglera
```

Result:

- Created `ave_project.json`.
- Created `library.sqlite`.
- Created project folders for media, analysis, vectors, timelines, renders, directives, and logs.
- Migrated SQLite schema.

### Phase 1: Ingest And Probe

Status: complete for the dummy corpus.

Commands run:

```bash
PYTHONPATH=src /opt/anaconda3/bin/python3.12 -m agentic_video_editor ingest \
  /Users/dariusshaoul/Documents/agentic-video-editor/demo_projects/smoke_e_digglera \
  /Users/dariusshaoul/Movies/smoke_e_digglera \
  --json
```

Result:

```json
{
  "files_found": 11,
  "assets_created": 11,
  "assets_updated": 0,
  "assets_unchanged": 0
}
```

Verified asset listing:

```bash
PYTHONPATH=src /opt/anaconda3/bin/python3.12 -m agentic_video_editor assets \
  /Users/dariusshaoul/Documents/agentic-video-editor/demo_projects/smoke_e_digglera \
  --json
```

The dummy corpus contains 11 ready MP4 assets with durations, FPS, resolution, video codec, audio codec, and audio presence stored in SQLite.

### Phase 2: Local Windows

Status: started and working across the dummy corpus.

Commands run:

```bash
PYTHONPATH=src /opt/anaconda3/bin/python3.12 -m agentic_video_editor analyze \
  /Users/dariusshaoul/Documents/agentic-video-editor/demo_projects/smoke_e_digglera \
  --window-sec 30 \
  --json
```

Result:

```json
{
  "assets_requested": 11,
  "assets_completed": 11,
  "windows_created": 68,
  "labels_created": 132,
  "artifacts_created": 33
}
```

Artifacts created:

- 11 low-resolution proxies in `media/proxies/`.
- 11 thumbnails in `media/thumbnails/`.
- 11 frame strips in `media/frame_strips/`.

Database rows created:

- `analysis_runs`
- `media_artifacts`
- `windows`
- `activity_labels`

Current labels are FFmpeg-derived:

- `audio_active`
- `high_energy`
- `silence` when detected
- `no_audio` for assets without audio

Motion scoring and PySceneDetect boundaries are still future Phase 2 hardening items.

### Phase 3: Transcript Search

Status: complete for the dummy corpus using Gemini-backed video summarization.

Commands added:

- `ave transcribe`
- `ave transcript-search`
- `ave transcript-summary`

Command run:

```bash
PYTHONPATH=src /opt/anaconda3/bin/python3.12 -m agentic_video_editor transcribe \
  /Users/dariusshaoul/Documents/agentic-video-editor/demo_projects/smoke_e_digglera \
  --provider gemini \
  --env-path .gemini_api.env \
  --json
```

Result:

```json
{
  "gemini_transcript_assets": 11,
  "transcript_spans": 111,
  "by_kind": {
    "lyric_summary": 33,
    "music": 46,
    "speech": 8,
    "summary": 2,
    "visual_summary": 22
  }
}
```

Implementation notes:

- Gemini output is stored as timestamped `transcript_spans`.
- FTS search is available through `transcript_spans_fts`.
- Long verbatim lyric transcription is intentionally avoided; song content is stored as short summaries/search snippets.
- The Gemini pipeline is resumable: reruns skip assets that already have provider output unless `--force` is passed.

### Phase 4: Gemini Selects

Status: complete for the dummy corpus.

Commands added:

- `ave semantic-analyze`
- `ave segment-search`
- `ave semantic-summary`

Command run:

```bash
PYTHONPATH=src /opt/anaconda3/bin/python3.12 -m agentic_video_editor semantic-analyze \
  /Users/dariusshaoul/Documents/agentic-video-editor/demo_projects/smoke_e_digglera \
  --provider gemini \
  --env-path .gemini_api.env \
  --json
```

Result:

```json
{
  "gemini_semantic_assets": 11,
  "segments": 68,
  "selects": 68,
  "relationships": 52
}
```

Implementation notes:

- Gemini-created segments are stored in `segments`.
- Editorial candidates are stored in `selects`.
- Segment-to-segment links are stored in `relationships`.
- Segment FTS search is available through `segments_fts`.
- Gemini timestamp hallucinations are handled by timeline filtering and render-time clamping.

### Phase 5: First Timeline

Status: complete for a first simple timeline compiler.

Commands added:

- `ave timeline`
- `ave timelines`

Command run:

```bash
PYTHONPATH=src /opt/anaconda3/bin/python3.12 -m agentic_video_editor timeline \
  /Users/dariusshaoul/Documents/agentic-video-editor/demo_projects/smoke_e_digglera \
  --directive "Make a 45-second Smoke E. Digglera mini-documentary montage using archive footage, live studio performance energy, emotional vocal moments, and a strong musical payoff." \
  --query "live studio performance archive emotional vocal payoff" \
  --duration-sec 45 \
  --max-clip-sec 8 \
  --json
```

Latest useful timeline:

```json
{
  "timeline_id": "timeline_04163997134b4eea",
  "items_created": 6,
  "duration_sec": 45.0
}
```

Implementation notes:

- Timeline JSON includes rational time fields for future OTIO export.
- Timeline items preserve segment ids, source asset ids, roles, and reasons.
- This is still a simple one-track A-roll compiler; beat-sheet planning and OTIO export remain future hardening work.

### Phase 6: Render And Critique

Status: first render and critique loop complete.

Commands added:

- `ave render`
- `ave renders`
- `ave critique`
- `ave reviews`

Render command:

```bash
PYTHONPATH=src /opt/anaconda3/bin/python3.12 -m agentic_video_editor render \
  /Users/dariusshaoul/Documents/agentic-video-editor/demo_projects/smoke_e_digglera \
  --timeline-id latest \
  --json
```

Latest render:

```json
{
  "render_id": "render_45fe075ca17c48bd",
  "timeline_id": "timeline_04163997134b4eea",
  "duration_sec": 45.043677,
  "status": "complete"
}
```

Critique command:

```bash
PYTHONPATH=src /opt/anaconda3/bin/python3.12 -m agentic_video_editor critique \
  /Users/dariusshaoul/Documents/agentic-video-editor/demo_projects/smoke_e_digglera \
  --render-id latest \
  --directive "Make a 45-second Smoke E. Digglera mini-documentary montage using archive footage, live studio performance energy, emotional vocal moments, and a strong musical payoff." \
  --provider gemini \
  --env-path .gemini_api.env \
  --json
```

Latest review:

```json
{
  "review_id": "review_953f0bca93fc4a78",
  "render_id": "render_45fe075ca17c48bd",
  "source": "gemini"
}
```

Critique highlights:

- The montage captures archive footage and emotional performance energy.
- Audio transitions are too abrupt.
- It needs more documentary context.
- The ending needs a stronger musical resolution.
- Suggested future patches include audio crossfades/bed, better ending selection, context overlays, and trimming early visual repetition.

### Phase 6.5: Editorial Context Intelligence

Status: implemented as the first context-aware backend pass.

Commands added:

- `ave context-build`
- `ave context-summary`
- `ave context-search`
- `ave related`
- `ave edit-plan`
- `ave timeline --context-aware`

New durable tables:

- `collection_summaries`
- `material_bank_items`
- `editorial_context_cards`
- `intent_analyses`
- `caption_options`
- `edit_plans`

Implementation notes:

- `context-build` materializes collection-level themes, visual styles, recurring terms, per-segment editorial context cards, avoid-pairing notes, warnings, and caption options.
- `context-search` returns retrieval packets with source evidence, match reasons, relationship expansion, placement guidance, continuity compatibility, and warnings.
- `edit-plan` separates intent/beat planning from timeline compilation and outputs explicit intent, beat sheet, candidate clips per beat, selected sequence, captions, transition notes, and continuity warnings.
- `timeline --context-aware` preserves the existing simple timeline fallback while adding `why_here`, `before_context`, `after_context`, `caption_text`, `transition_note`, and `continuity_score` to planned timeline items.
- The mock provider path is deterministic for tests; the Gemini provider can generate JSON text context from existing segment summaries without re-uploading video.

Known limitation exposed by the 4-minute word-story test:

- The current system can create a constrained montage, but it does not yet author a real storyline.
- A brief such as "create a 4 minute movie that features a small story created thru words, max shot length 5 seconds" needs a StoryAgent, word spine, causal beat planning, pre-render critique, and repair loop.
- Handoff doc: [next-agentic-editing-handoff.md](next-agentic-editing-handoff.md).

### Phase 6.6: Micro-Timing Layer

Status: implemented (July 15, 2026).

Commands added:

- `ave cutpoints` / `ave cutpoints-summary` — shot changes (ffmpeg scene score) and audio gaps (silencedetect at phrase resolution) stored in `scene_boundaries`.
- `ave timeline --snap-tolerance-sec` — timeline compilation snaps LLM-approximate trim points to detected cut points, with a 0.15s protective margin inside audio gaps; budget truncation pulls out-points back to the nearest clean cut.
- `ave render --crossfade-sec / --burn-captions / --no-loudnorm` — micro audio fades at hard cuts, optional xfade+acrossfade transitions, drawtext caption burn-in with graceful fallback, and one loudness pass over the whole timeline instead of per clip.

Prompt/context upgrades:

- Semantic prompt rewritten with cut-hygiene timing rules and new fields: `word_units` (verbatim short quotes with tight timestamps), `story_function`, `setup_questions`, `payoff_answers`, `audio_affordance`, `visual_affordance`, `needs_caption`, `cut_notes`. Stored as new `segments` columns, FTS-indexed, and surfaced in retrieval packets.
- Transcript prompt rewritten so spans start at word onsets and break at breath points, making them safely quotable as cut ranges.

Dropped:

- The LLM-scored critique/revision loop, per the July 15 decision. Constraint validation with mechanical repair remains the plan.

### Phase 6.7: Local Word Alignment + qmd Relationship Mining

Status: implemented (July 15, 2026).

Commands added:

- `ave align` / `ave align-summary` — local word-level ASR via faster-whisper
  (already installed in the anaconda env; CT2 models download once, then fully
  offline; default `small.en`, same weights family superwhisper uses). Stores
  verbatim FTS-indexed `transcript_spans` (source `local_asr`), per-word timing in
  the new `word_alignments` table, and pause-adjacent word boundaries as
  `asr_word` cut points (`word_start` = clean in-point, `word_end` = clean
  out-point) that the timeline snapper consumes. Note: superwhisper's own model
  files (ggml/whisper.cpp + argmax CoreML) are format-incompatible with Python
  tooling; faster-whisper is the equivalent local path.
- `ave export-cards` — one markdown card per segment (summary, verbatim word
  units, affordances, context card fields, overlapping verbatim ASR spans) into
  `PROJECT/qmd_cards/` for qmd indexing.
- `ave relate --collection NAME` — vector-searches each card via `qmd vsearch
  --json`, writes typed segment relationships (`duplicates` >= 0.85 similarity,
  `echoes` / `echoes_same_source` above `--min-score`) into the existing
  `relationships` table, deduped per pair. Edges surface through `ave related`
  and retrieval packet `relationship_expansion`.

qmd workflow (one-time per project):

```bash
ave export-cards PROJECT
qmd collection add PROJECT/qmd_cards --name ave-PROJECT
qmd embed
ave relate PROJECT --collection ave-PROJECT
```

Note: qmd slugifies file names (underscores become hyphens); the relate command
matches ids through normalized slugs.

### Phase 6.8: Per-Join Transition Agency

Status: implemented (July 16, 2026).

- Transitions are now decided per join at timeline compile time
  (`timeline.py::_assign_transitions` / `_decide_transition`) from evidence on the
  adjacent clips: same-source jump cuts get a dissolve, abrupt/missing audio gets a
  short crossfade, music-to-music source changes blend, dialogue boundaries hard
  cut. Each decision is stored as `timeline_items.transition_json` with a `why`.
- The renderer executes the plan with a mixed filter chain
  (`render.py::_joined_command`): xfade+acrossfade at fade joins, concat at cut
  joins, `settb` normalization so the two can chain, micro audio fades only on
  hard-cut sides. `--crossfade-sec` is demoted to a force-everything override.
- Verified on the demo project: a talk-directive timeline produced mixed decisions
  (2 cuts, 3 crossfades) and rendered cleanly.
- Companion audit of remaining blanket creative decisions:
  [blanket-creative-decisions-audit.md](blanket-creative-decisions-audit.md).

### Phase 7: Review UI

Status: not started.

Next concrete events:

1. Add local API over the existing service functions.
2. Build project/library browser.
3. Add player, timeline review, and agent activity panels.
4. Add dataset-style filtering and select approval/swap flow.

## Verification

Command run:

```bash
/opt/anaconda3/bin/python3.12 -m pytest -q
```

Result:

```text
7 passed
```

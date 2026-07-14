# Agentic Video Editor Design Plan

Last updated: July 13, 2026

Implementation companion: [docs/design/v1-agentic-video-editor.md](docs/design/v1-agentic-video-editor.md)

This document captures the research-informed system direction. The companion spec translates it into the first product/architecture design for V1 implementation.

## 1. Goal

Build a local-first, hobby/non-commercial framework that can ingest hundreds of hours of raw, unstructured shoot footage and autonomously produce a logical edited video from a natural-language directive.

The system should not merely search footage. It should understand footage, store a reusable internal media library, plan an edit, assemble a timeline, render a rough cut, critique the result, and revise.

Target user flow:

```text
raw footage folder
  -> ingest and cheap analysis
  -> Gemini-assisted semantic understanding
  -> searchable media library
  -> directive-driven edit plan
  -> OTIO timeline
  -> rendered rough cut
  -> automated critique/revision
```

Example directive:

```text
Make a 3-minute behind-the-scenes video about the shoot becoming chaotic but eventually working.
Use funny moments, show the process, keep the pace high, and end on the best finished shot.
```

## 2. Key Product Principles

- The default behavior is autonomous: the agent should make an edit, not stop every few steps.
- Raw footage is expected to have few or no cuts, so segmentation must use audio, motion, transcript, faces, objects, camera behavior, and semantic events rather than relying on edited scene cuts.
- Gemini credits should be used as the main semantic/video-understanding resource, but only after cheap local passes reduce the amount of footage that needs expensive analysis.
- Every model call should produce structured, reusable metadata that improves the media library permanently.
- The edit should be represented as a non-destructive timeline before rendering.
- The first version should optimize for documentary/interview/BTS/story edits, because transcript-first story construction will produce useful results fastest.
- The framework should keep enough review hooks that the user can inspect, override, or approve selects without blocking fully automated runs.

## 3. What To Borrow

### VideoRAG / Vimo

Borrow:

- Long-video indexing pipeline shape: split, caption, transcribe, embed, store, query.
- Storage abstraction ideas: key-value metadata, vector storage, graph relationships.
- Query flow for "where does X happen?" style retrieval.

Do not adopt wholesale:

- It is not an editor.
- It uses fixed segments rather than editorially meaningful selects.
- Its media model is mostly file-path based, not a full internal library.
- It has non-commercial ImageBind-related licensing constraints in current implementation.

Source: https://github.com/HKUDS/VideoRAG

### VideoAgent

Borrow:

- Tool registry pattern.
- Intent-to-tool routing.
- Agent graph planning.
- Multi-step execution chain.
- Gemini-backed video captioning ideas.

Do not adopt wholesale:

- Orchestration depends on Claude/GPT/DeepSeek in several places.
- Install/runtime assumptions are heavy.
- It is closer to a research prototype than a robust raw-footage editor.

Source: https://github.com/HKUDS/VideoAgent

### UniVA

Borrow:

- Plan/Act architecture.
- MCP-style tool boundaries.
- Streaming task/tool events.
- Local editor/media-bin UI ideas from its OpenCut-derived frontend.
- Sliding-window probing idea for long videos.

Do not adopt wholesale:

- Repo is young.
- Long-video editing path is thin.
- Some helpers assume OpenAI-style providers even though core agents can use Gemini.
- License/project-site language should be treated cautiously for redistribution.

Source: https://github.com/univa-agent/univa

### OpenTimelineIO

Use as the canonical edit-decision format.

Source: https://github.com/AcademySoftwareFoundation/OpenTimelineIO

### PySceneDetect

Use as an optional visual boundary detector, but not the primary segmentation method for raw uncut footage.

Source: https://github.com/Breakthrough/PySceneDetect

### auto-editor

Borrow silence/motion heuristics and NLE export ideas.

Source: https://github.com/WyattBlue/auto-editor

### FiftyOne

Optional review/index UI for datasets and temporal annotations. For v1, prefer our own SQLite/vector-backed data model unless FiftyOne saves substantial time.

Source: https://github.com/voxel51/fiftyone

## 4. System Architecture

```text
                         +----------------------+
                         | Natural directive    |
                         +----------+-----------+
                                    |
                                    v
+-------------+       +-------------+-------------+       +------------------+
| Raw footage | ----> | Ingest and local analysis | ----> | Media library DB |
+-------------+       +-------------+-------------+       +---------+--------+
                                    |                               |
                                    v                               v
                         +----------+-----------+       +-----------+---------+
                         | Gemini semantic pass | ----> | Retrieval and graph |
                         +----------+-----------+       +-----------+---------+
                                    |                               |
                                    v                               v
                         +----------+-----------+       +-----------+---------+
                         | Story/edit planner   | <---- | Search/select tools |
                         +----------+-----------+       +-----------+---------+
                                    |
                                    v
                         +----------+-----------+
                         | OTIO timeline        |
                         +----------+-----------+
                                    |
                                    v
                         +----------+-----------+
                         | FFmpeg render        |
                         +----------+-----------+
                                    |
                                    v
                         +----------+-----------+
                         | Critique and revise  |
                         +----------------------+
```

## 5. Core Subsystems

### 5.1 Ingest

Inputs:

- One or more folders of raw footage.
- Optional project metadata: shoot name, intended output type, known people, locations, notes.

Responsibilities:

- Discover media files.
- Fingerprint each asset.
- Extract ffprobe metadata: duration, FPS, resolution, codec, audio streams, creation time.
- Generate low-resolution proxies.
- Generate thumbnails and sparse frame strips.
- Store file identity and analysis status.
- Avoid reprocessing unchanged files.

Preferred local tools:

- `ffprobe`
- `ffmpeg`
- Python orchestration
- SQLite for durable metadata

### 5.2 Cheap Local Analysis

Run before Gemini:

- Audio energy and silence detection.
- Speech/music/noise rough classification if local tools are available.
- Motion/activity score per time window.
- Blur/exposure/quality score.
- Keyframe extraction.
- Optional PySceneDetect boundaries.
- Optional auto-editor-style silence/motion labels.

Output:

- Coarse windows, likely speech turns, dead zones, high-activity zones, bad-quality zones, and candidate moments.

Important design choice:

- For raw footage, segmentation should be window-first and event-first, not cut-first.

### 5.3 ASR and Transcript Layer

For v1, the fastest useful editor is transcript-first.

Responsibilities:

- Transcribe speech with timestamps.
- Split speech into utterances and larger story beats.
- Track speaker labels when possible.
- Store transcript text with time ranges.
- Allow transcript search and quote selection.

Implementation options:

- Local Whisper/faster-whisper if available.
- Gemini audio/video transcription where it is cheaper or more accurate for the footage.
- Hybrid: local ASR first, Gemini only for summaries and corrections.

### 5.4 Gemini Semantic Understanding

Gemini should be used in tiers.

Tier 1: low-cost coarse analysis

- Analyze low-resolution windows.
- Use longer windows, such as 30-120 seconds.
- Ask for structured JSON only.
- Extract people, actions, objects, mood, location, story relevance, notable moments, bad footage, and possible uses.

Tier 2: focused selects analysis

- Reanalyze only candidate moments.
- Use shorter windows, such as 5-20 seconds.
- Ask for editorial value, start/end trim suggestions, visual description, emotional beat, quality, and relation to directive.

Tier 3: critique pass

- Analyze rendered rough cut or timeline summary.
- Identify pacing issues, missing context, confusing story jumps, repeated shots, weak ending, or better alternates.

Gemini output must be stored and reused. The same footage should not be re-captioned unless the prompt/schema/model version changes.

### 5.5 Media Library Database

Start with SQLite plus a vector store. Move to Postgres/pgvector only if scale or concurrency requires it.

Minimum entities:

- `assets`: original files and technical metadata.
- `analysis_runs`: model/tool run metadata, prompt/schema/model version, cost estimates, status.
- `windows`: coarse time windows for analysis.
- `segments`: meaningful candidate ranges with start/end timestamps.
- `transcript_spans`: speech ranges and speaker labels.
- `visual_observations`: people, objects, actions, locations, camera state, quality.
- `audio_observations`: speech, music, silence, noise, laughter, applause, impact sounds.
- `selects`: high-value editorial moments.
- `relationships`: same take, reaction to, setup/payoff, visual proof of claim, alternate angle, duplicate moment.
- `embeddings`: text/visual/segment embeddings.
- `projects`: user directives and output goals.
- `timelines`: generated edit plans and revisions.
- `renders`: output videos and review metadata.

Each segment/select should store:

```json
{
  "asset_id": "asset_123",
  "start_sec": 123.4,
  "end_sec": 138.9,
  "type": "select",
  "summary": "The director explains why the setup is failing while crew resets lights.",
  "transcript": "We thought this would be the easy shot...",
  "people": ["director", "gaffer"],
  "actions": ["explaining", "resetting lights"],
  "mood": ["frustrated", "funny"],
  "camera": {"motion": "handheld", "framing": "medium"},
  "quality": {"usable": true, "score": 0.82, "issues": ["slight noise"]},
  "story_roles": ["conflict", "process"],
  "embedding_text": "...",
  "source_model": "gemini",
  "schema_version": "segment-v1"
}
```

### 5.6 Retrieval

The edit agent needs richer retrieval than plain semantic search.

Search modes:

- Transcript search.
- Semantic vector search.
- People/location/action filters.
- Time-range and source-file filters.
- Quality filters.
- Story-role search: setup, conflict, process, payoff, reaction, joke, proof, beauty shot.
- Diversity search to avoid repeated visual material.
- Alternate-take search.
- Coverage search for B-roll and reaction shots.

The retrieval API should return both candidates and reasons.

Example:

```json
{
  "query": "funny moment showing chaos before the final shot works",
  "results": [
    {
      "segment_id": "seg_456",
      "score": 0.91,
      "reason": "Contains laughter, visible crew reset, and transcript mentions the failed setup.",
      "suggested_use": "midpoint escalation"
    }
  ]
}
```

### 5.7 Story and Edit Planner

The planner turns the directive into an editorial structure.

For v1, use a documentary/story grammar:

1. Hook.
2. Context.
3. Setup.
4. Conflict/problem.
5. Attempts/process.
6. Escalation or funny complication.
7. Resolution.
8. Payoff/ending.

Planner responsibilities:

- Interpret output duration, tone, audience, and constraints.
- Create a beat sheet.
- Query media library for each beat.
- Pick A-roll/story spine first when speech exists.
- Add B-roll, reactions, process shots, inserts, and beauty shots.
- Build a pacing curve.
- Avoid repeated moments unless intentional.
- Prefer clear audio and visually usable material.
- Produce an editable timeline plan with reasons.

### 5.8 Timeline Compiler

The compiler converts selected segments into OTIO.

Responsibilities:

- Create tracks for A-roll, B-roll, audio, music, captions, graphics.
- Apply trims.
- Add transitions only where justified.
- Add placeholders for captions, title cards, or missing media.
- Preserve source references and timecodes.
- Export OTIO and render instructions.

Timeline representation:

- OTIO is the canonical edit decision format.
- A simpler JSON timeline can exist as the agent-facing intermediate format.
- FFmpeg rendering should be deterministic from the timeline JSON/OTIO.

### 5.9 Renderer

V1 renderer:

- Use FFmpeg.
- Concatenate clips with trims.
- Support basic audio leveling.
- Support optional captions.
- Support simple crossfades or hard cuts.
- Output MP4 rough cut.

Later renderer:

- More complex overlays, effects, speed ramps, music ducking, and NLE exports.

### 5.10 Critique and Revision

After rendering, run a review pass.

Inputs:

- Directive.
- Beat sheet.
- Timeline summary.
- Rendered video or sampled frames/audio.
- Segment reasons.

Outputs:

- Pacing notes.
- Missing story beats.
- Weak or redundant clips.
- Suggested replacements.
- Concrete timeline patch operations.

The system should keep revisions:

```text
timeline_v1.otio
timeline_v2.otio
render_v1.mp4
render_v2.mp4
review_v1.json
```

## 6. Agent Architecture

Use a small set of explicit tools rather than one giant free-form agent.

### Agents

- `IngestAgent`: schedules file analysis and tracks progress.
- `LibrarianAgent`: turns raw observations into searchable segments/selects.
- `StoryAgent`: interprets the directive and creates a beat sheet.
- `CastingAgent`: retrieves candidate clips for each beat.
- `EditorAgent`: chooses clips, trims, pacing, and sequence.
- `TimelineAgent`: compiles the edit into OTIO/timeline JSON.
- `RenderAgent`: renders and validates output.
- `CriticAgent`: reviews the rough cut and proposes revisions.

### Tool Registry

Borrow the VideoAgent/UniVA idea: each tool has a schema and clear side effects.

Example tools:

```text
search_segments(query, filters, limit)
get_segment(segment_id)
find_alternates(segment_id)
find_reaction_shots(people, time_context)
create_beat_sheet(directive, duration)
compile_timeline(beats, selected_segments)
render_timeline(timeline_id)
critique_render(render_id, directive)
apply_timeline_patch(timeline_id, patch)
```

### Autonomy Levels

Support modes:

- `auto`: ingest, edit, render, critique, revise without stopping.
- `review_selects`: pause after selects before editing.
- `review_timeline`: pause before render.
- `manual`: use the library/search/timeline tools interactively.

Default for the project: `auto`.

## 7. Gemini Strategy

Gemini should be the primary paid intelligence layer, but the system should be cost-aware.

Rules:

- Never send all raw footage repeatedly.
- Prefer local ffmpeg/ASR/motion/silence passes first.
- Use low-resolution or proxy video for coarse analysis.
- Cache all Gemini results by asset hash, time range, prompt version, schema version, and model.
- Use focused reanalysis only for candidate selects.
- Use structured JSON schemas to prevent vague captions.
- Track estimated and actual token usage per project.

Model abstraction:

```text
VideoUnderstandingProvider
  - analyze_window(video_ref, start_sec, end_sec, schema)
  - analyze_select(video_ref, start_sec, end_sec, directive, schema)
  - critique_render(video_ref, directive, timeline_summary, schema)

TextReasoningProvider
  - plan_beats(directive, library_summary)
  - choose_selects(beat, candidates)
  - revise_timeline(review, available_alternates)
```

Gemini can implement both interfaces initially. The abstraction leaves room for OpenAI, Claude, local VLMs, or Ollama later.

## 8. CLI/API Surface

Start with a CLI plus internal Python API. Add UI after the pipeline works.

Suggested commands:

```text
ave init PROJECT_DIR
ave ingest PROJECT_DIR /path/to/footage
ave analyze PROJECT_DIR --budget low
ave search PROJECT_DIR "funny chaos before the final shot"
ave edit PROJECT_DIR --directive directive.txt --duration 180 --mode auto
ave render PROJECT_DIR --timeline latest
ave critique PROJECT_DIR --render latest
ave revise PROJECT_DIR --render latest --apply
```

Project structure:

```text
project/
  media/
    originals/          # optional symlinks or manifests, not duplicated by default
    proxies/
    thumbnails/
  library.sqlite
  vectors/
  analysis/
  timelines/
  renders/
  directives/
  logs/
```

## 9. Phased Build Plan

### Phase 0: Skeleton

Deliver:

- Project folder format.
- SQLite schema.
- Asset discovery and ffprobe metadata.
- Basic CLI.
- Analysis status tracking.

Acceptance:

- Ingest a folder and list all assets with duration/resolution/audio status.

### Phase 1: Cheap Segmentation

Deliver:

- Proxy generation.
- Thumbnail/keyframe extraction.
- Audio energy/silence detection.
- Motion/activity scoring.
- Coarse windows.
- Basic search over file names and transcripts if transcripts exist.

Acceptance:

- A 1-hour raw clip is split into useful windows with silence/activity labels.

### Phase 2: Gemini Segment Understanding

Deliver:

- Gemini provider adapter.
- Structured JSON schema for coarse window analysis.
- Cached model calls.
- Segment/select records.
- Semantic search.

Acceptance:

- Ask "find funny moments where the shoot is going wrong" and get timestamped candidates with reasons.

### Phase 3: Transcript-First Story Editor

Deliver:

- ASR integration.
- Beat-sheet planner.
- Candidate retrieval per beat.
- Select ranking.
- Timeline JSON.
- OTIO export.

Acceptance:

- Given a directive and raw interview/BTS footage, produce an OTIO timeline with a coherent story spine.

### Phase 4: Rough-Cut Renderer

Deliver:

- FFmpeg render from timeline.
- Basic audio leveling.
- Captions from transcript.
- Render metadata.

Acceptance:

- Produce a playable MP4 rough cut from the generated timeline.

### Phase 5: Critique and Revision

Deliver:

- Critique pass over render/timeline summary.
- Revision suggestions.
- Automatic timeline patching.
- Render v2.

Acceptance:

- The system can identify at least three concrete timeline improvements and produce a revised cut.

### Phase 6: Review UI

Deliver:

- Media library browser.
- Segment/select search.
- Timeline review.
- Render playback.
- Approve/reject selects.

Acceptance:

- User can inspect why each clip was chosen and swap alternates.

## 10. Testing and Validation

### Unit Tests

- Asset fingerprinting and metadata parsing.
- Time-range math.
- Segment/window overlap logic.
- JSON schema validation.
- Search filters.
- Timeline compilation.
- FFmpeg command generation.

### Integration Tests

- Ingest small fixture folder.
- Analyze with mocked Gemini provider.
- Generate beat sheet.
- Build timeline.
- Render short MP4.
- Run critique with mocked provider.

### Golden Fixtures

Maintain a tiny test footage set:

- Interview clip.
- Silent B-roll clip.
- Chaotic handheld/process clip.
- Beauty shot.

Expected outputs:

- Search results.
- Beat sheet.
- Timeline JSON.
- OTIO file.
- Rendered MP4 duration within tolerance.

### Manual Acceptance Scenarios

1. "Make a 60-second BTS story with a funny middle and satisfying ending."
2. "Find all usable quotes about why the shoot was hard."
3. "Make a fast montage of process shots with no dialogue."
4. "Replace the weakest clip in the rough cut with a better alternate."

## 11. Main Risks

- Gemini cost can grow quickly if caching and tiered analysis are not strict.
- Raw footage segmentation may miss important moments without good ASR/audio/motion signals.
- Fully autonomous narrative editing depends on strong beat planning and retrieval diversity.
- Rendering from OTIO may require a custom subset; OTIO is an interchange model, not a renderer.
- Face/person identification can become sensitive; keep it local and user-controlled.
- Long project runs need resumability and idempotent jobs from day one.

## 12. Default Technical Choices

- Language: Python for backend/CLI.
- Database: SQLite for v1.
- Vector search: local vector store, replaceable later.
- Timeline: OpenTimelineIO plus simple internal JSON.
- Rendering: FFmpeg.
- Semantic model: Gemini via adapter.
- ASR: faster-whisper or Gemini, chosen per environment.
- UI: defer until CLI pipeline produces useful rough cuts.
- License posture: hobby/non-commercial; avoid copying non-commercial code directly unless isolated and documented.

## 13. First Implementation Target

The first build should not attempt every possible video style. It should target:

```text
raw BTS/interview/process footage
  -> transcript-first narrative
  -> 60-180 second rough cut
  -> hard cuts, simple captions, basic audio leveling
```

That path gets to a magical result fastest while still building the foundation for more autonomous montage, music-video, commercial, and experimental edits later.

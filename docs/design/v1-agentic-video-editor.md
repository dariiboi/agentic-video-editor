# Agentic Video Editor V1 Design

Last updated: July 13, 2026

Companion audit: [third-party-repo-audit.md](third-party-repo-audit.md)
Current execution status: [phase-execution-status.md](phase-execution-status.md)
Next agentic editing handoff: [next-agentic-editing-handoff.md](next-agentic-editing-handoff.md)

## Product Thesis

The editor is a local-first system that turns unstructured raw footage into a coherent rough cut from a natural-language directive. It is not a search app with an export button. It is an editing agent with a memory: every ingest, transcript, analysis window, select, timeline, render, and critique becomes part of a durable project library.

V1 should make a useful documentary/BTS/interview rough cut before it tries to become a general-purpose NLE. The fastest path to a compelling first version is transcript-first storytelling, supported by visual/audio metadata and cost-aware Gemini analysis.

## V1 User Promise

Given a folder of raw footage and a directive like:

```text
Make a 3-minute behind-the-scenes video about the shoot becoming chaotic but eventually working.
Use funny moments, show the process, keep the pace high, and end on the best finished shot.
```

the system should:

1. Discover and index the footage.
2. Extract cheap local metadata and transcripts.
3. Use Gemini only on useful windows and candidate selects.
4. Build a searchable media library.
5. Plan a story beat sheet.
6. Choose clips with reasons.
7. Compile a non-destructive timeline.
8. Render a playable MP4 rough cut.
9. Critique the cut and produce a revised timeline when requested.

## Non-Goals For V1

- Frame-perfect professional finishing.
- A full Premiere/DaVinci-style UI.
- Complex compositing, motion graphics, speed ramps, or multicam syncing.
- Fully general music-video, sports, narrative-fiction, or social-template editing.
- Cloud collaboration.
- Reprocessing the same footage repeatedly with expensive model calls.

## Primary Experience

The first usable experience should be a command-line workflow plus a compact review UI. The CLI proves the pipeline. The UI makes the agent inspectable.

### CLI Flow

```text
ave init ./my-project
ave ingest ./my-project /Volumes/ShootDay
ave analyze ./my-project --budget low
ave edit ./my-project --directive ./directive.txt --duration 180 --mode auto
ave render ./my-project --timeline latest
ave critique ./my-project --render latest
ave revise ./my-project --render latest --apply
```

### Review UI Flow

The UI should open on the current project, not a marketing screen.

First screen:

- Left: project library and search.
- Center: selected media/player/timeline review.
- Right: agent run panel with current plan, selected beats, reasons, warnings, and revision notes.
- Bottom: timeline strip with A-roll, B-roll, audio, captions, and placeholders.

Expected user actions:

- Search for moments in plain language.
- Inspect why a select was chosen.
- Approve/reject a select.
- Swap with alternates.
- Adjust the directive.
- Render the current timeline.
- Ask for a revision focused on pacing, clarity, tone, or ending.

## Autonomy Modes

Autonomy is a project setting and a per-run override.

| Mode | Behavior |
| --- | --- |
| `auto` | Ingest, analyze, plan, select, compile, render, critique, and optionally revise without pausing. |
| `review_selects` | Pause after candidate selects and beat coverage are ready. |
| `review_timeline` | Pause after timeline compilation, before render. |
| `manual` | User drives search/select/timeline operations through tools. |

V1 default: `auto`.

## System Shape

```text
raw footage
  -> ingest
  -> local analysis
  -> transcript layer
  -> coarse Gemini windows
  -> media library
  -> retrieval
  -> beat planner
  -> select casting
  -> timeline compiler
  -> FFmpeg renderer
  -> critique/revision loop
```

## Third-Party Repo Audit Additions

This section records source-audit findings from the upstream projects that should influence the full design. These are design borrowings only; do not copy third-party implementation code into this repo.

### VideoRAG / Vimo Additions

Key details to preserve:

- Treat retrieval as a dual system: text/graph knowledge plus multimodal visual context, not embeddings alone.
- Support multi-video and cross-video relationships from the beginning of the schema.
- Store references back to exact clips/time ranges so generated answers, edit plans, and critiques can cite source evidence.
- Keep the long-video path budget-aware enough to work on hundreds of hours by distilling raw media into compact reusable knowledge.
- Plan for local model options later: VideoRAG explicitly supports local/Ollama-style model configuration, which fits the local-first goal.

Design implications:

- `relationships` should graduate from future concept to core library table before semantic search is considered complete.
- Search results must carry `source_reference` fields: asset, time range, transcript span, observation ids, and thumbnail/frame-strip pointers.
- Library summaries should be materialized per project and per collection so agents do not repeatedly scan all segments.

### VideoAgent Additions

Key details to preserve:

- Intent analysis should capture implicit sub-intents, not only literal requirements.
- A graph-powered workflow planner is useful for choosing agent/tool order dynamically.
- Retrieval should be generated from storyboards and visual queries, not only user text.
- Self-evaluation and adaptive feedback loops should be explicit first-class run steps.
- Editing use cases include rhythm-aware editing, scene-based assembly, emotion-driven construction, video overview, commentary, and source-to-screen adaptation.

Design implications:

- Add a `workflow_graph` concept: nodes are tool calls or agent steps, edges are dependencies, and every node has inputs, outputs, status, retries, and review notes.
- `StoryAgent` should output both narrative beats and visual retrieval queries.
- `CriticAgent` should be part of every automated edit run, not a detached optional afterthought.
- Music/rhythm metadata should be included in the local analysis roadmap even if V1 prioritizes documentary edits.

### UniVA Additions

Key details to preserve:

- Separate planning from execution: a Plan Agent decomposes intent; an Act Agent executes through modular tools.
- Deep memory matters: global/project/user/style memory keeps preferences and story continuity stable across rounds.
- The workspace should support multi-round co-creation, not only one-shot automatic output.
- MCP-style modularity is valuable for model/tool replacement and future generation/editing capabilities.
- A backend API plus timeline editor frontend is a reasonable long-term shape.

Design implications:

- Add `memories` or `preferences` tables later for project-specific style, people, recurring terms, and editorial preferences.
- Keep CLI functions behind service/tool functions so a FastAPI or local web backend can call the same operations.
- The UI should expose run history and memory, not just current timeline state.

### OpenTimelineIO Additions

Key details to preserve:

- OTIO is an edit-decision interchange format and API; it references external media but does not contain the media.
- The OTIO time model matters. Timeline data should preserve rational frame/time values, not only floats.
- Adapter plugins are now split: core OTIO handles `.otio`, `.otioz`, and `.otiod`; other NLE formats may require `OpenTimelineIO-Plugins`.
- Media linkers and hook scripts can become integration points for local media path conventions and validation.
- Current OTIO Python support is centered on Python 3.9-3.12.

Design implications:

- Timeline JSON should store `source_rate`, `timeline_rate`, and frame-accurate rational time alongside seconds.
- Keep local timeline JSON as our agent-facing editable format, then compile to OTIO for interchange.
- Target the real dependency environment at Python 3.11 or 3.12 before adding OTIO/FiftyOne, even though the current bare first slice compiles on Python 3.14.

### PySceneDetect Additions

Key details to preserve:

- PySceneDetect offers content-aware scene detection, `AdaptiveDetector` for fast camera movement, and `ThresholdDetector` for fades.
- It can save representative images and split video when `ffmpeg` is available.
- It is useful as an optional boundary detector, but raw footage can be long uncut takes, so it cannot be the primary segmentation strategy.

Design implications:

- Store detected boundaries with `detector_name`, `threshold`, `confidence`, and `reason`, not just start/end.
- Use scene boundaries as hints for window refinement and thumbnail selection.
- Preserve manual override because detector output will be weak on handheld raw footage, whip pans, and low-contrast scenes.

### auto-editor Additions

Key details to preserve:

- The useful primitive is not just silence removal; it is a timeline of labeled moments.
- `margin`/padding around kept sections is editorially important.
- Multiple edit methods can be combined: audio loudness, motion, multi-track audio, and custom labels.
- Export to NLEs is a user-facing value path, even if OTIO is our canonical interchange.

Design implications:

- Local analysis should produce `activity_labels` per time window: silence, speech, music, motion, dead-space, high-energy, keep-candidate, cut-candidate.
- Store configurable pre/post-roll margins on selects and timeline items.
- Add multi-track audio metadata before serious documentary/interview editing.

### FiftyOne Additions

Key details to preserve:

- FiftyOne is best thought of as a dataset inspection, labeling, and model-evaluation tool.
- It may be heavy for V1, but its mental model is useful: samples, labels, views, filters, evaluation, and visual QA.
- Current install docs target Python 3.10-3.12.

Design implications:

- Our review UI should support dataset-style views: filter by quality, person, story role, confidence, source asset, and rejected/approved state.
- Add export/import paths later for reviewing segments/selects in FiftyOne or a FiftyOne-like inspection mode.
- Do not make FiftyOne a hard dependency for V1.

## Core Modules

### Project Store

Owns the project directory and durable references to all generated artifacts.

```text
project/
  ave_project.json
  library.sqlite
  media/
    proxies/
    thumbnails/
    frame_strips/
  analysis/
    runs/
    gemini_cache/
    transcripts/
  vectors/
  timelines/
    timeline_v001.json
    timeline_v001.otio
  renders/
    render_v001.mp4
  directives/
  logs/
```

Original footage should be referenced by manifest/path/hash, not copied by default.

### Ingest

Responsibilities:

- Discover video/audio files.
- Fingerprint files.
- Run `ffprobe`.
- Store asset metadata.
- Detect whether assets are unchanged.
- Create jobs for proxy, thumbnail, transcript, and local analysis.

First implementation should support:

- `.mp4`, `.mov`, `.m4v`, `.wav`, `.mp3`.
- Local absolute paths.
- Re-ingest without duplicate asset rows.

### Local Analyzer

Cheap analysis happens before model calls.

Signals:

- Duration, frame rate, resolution, codec.
- Audio energy by window.
- Silence and speech-likely regions.
- Per-track audio presence and rough loudness where multiple audio streams exist.
- Motion/activity score by window.
- Label tracks inspired by auto-editor: silence, speech, music, motion, dead-space, high-energy, keep-candidate, cut-candidate.
- Blur/exposure rough quality.
- Sparse thumbnails and frame strips.
- Optional scene boundaries when useful, including detector type and confidence.

Output:

- Coarse windows, usually 30-120 seconds.
- Bad-quality/dead-zone flags.
- Candidate interesting regions.
- Model-analysis priorities.
- Editorial padding suggestions: pre-roll and post-roll margins around active sections.

### Transcript Layer

V1 should treat spoken words as the story spine whenever available.

Responsibilities:

- Produce timestamped transcript spans.
- Preserve word-level or phrase-level timing when the ASR backend supports it.
- Store speaker labels when available.
- Build searchable text chunks.
- Link transcript spans to windows and selects.

Backend strategy:

- Start with a provider interface.
- Use local Whisper/faster-whisper when available.
- Allow Gemini-backed transcription later for correction or hard cases.

### Gemini Semantic Layer

Gemini is used in three tiers.

Tier 1, coarse windows:

- Low-resolution proxy video.
- 30-120 second ranges.
- Structured JSON only.
- Identify people, actions, objects, location, emotional tone, quality, events, story roles, and candidate moments.

Tier 2, focused selects:

- 5-20 second ranges.
- Directive-aware.
- Suggest trim points.
- Explain editorial use.
- Flag audio/visual problems.

Tier 3, critique:

- Review timeline summary and/or rendered rough cut samples.
- Return concrete patch suggestions, not vague feedback.

Cache key:

```text
asset_hash + start_sec + end_sec + model + prompt_version + schema_version + media_proxy_hash
```

### Media Library

SQLite is the source of truth for V1. A vector store can be local files or SQLite-backed embeddings at first; the abstraction should allow later migration.

Minimum tables:

- `projects`
- `assets`
- `analysis_runs`
- `windows`
- `activity_labels`
- `scene_boundaries`
- `transcript_spans`
- `segments`
- `observations`
- `selects`
- `embeddings`
- `relationships`
- `collection_summaries`
- `material_bank_items`
- `editorial_context_cards`
- `intent_analyses`
- `caption_options`
- `directives`
- `edit_plans`
- `workflow_graphs`
- `workflow_nodes`
- `timelines`
- `timeline_items`
- `renders`
- `reviews`
- `memories`

Important distinction:

- A `window` is an analysis chunk.
- A `segment` is a meaningful time range.
- A `select` is an editorially valuable segment chosen for some possible use.
- A `timeline_item` is an actual placement in an edit.
- A `relationship` links two evidence-bearing entities, such as same-moment, setup/payoff, visual-proof, alternate-take, reaction-to, or duplicate.
- An `editorial_context_card` is the reusable editor-memory layer for a segment: local meaning, corpus meaning, editorial use, avoid-pairing notes, caption options, and warnings.
- An `intent_analysis` records the explicit and implicit directive interpretation used by retrieval and planning.
- A `workflow_node` records a tool/agent step in a generated edit plan, including dependencies, status, retries, and outputs.

### Retrieval

Retrieval must return candidates and reasons. The editor needs to know why a clip is useful.

Search dimensions:

- Transcript text.
- Semantic embedding.
- People.
- Actions.
- Objects.
- Locations.
- Story roles.
- Mood/tone.
- Quality.
- Asset/time filters.
- Diversity and visual repetition.
- Alternates and reaction shots.
- Source references suitable for citations: asset id, time range, transcript span ids, observation ids, and preview image paths.
- Visual query expansion generated from the beat sheet/storyboard, not only the user's literal words.

Example response:

```json
{
  "query": "archive to live studio emotional payoff",
  "packets": [
    {
      "segment_id": "seg_123",
      "select_id": "select_123",
      "score": 0.91,
      "source_evidence": {
        "summary": "The studio session reaches an emotional vocal peak.",
        "context": "Within the corpus, this works as emotion/payoff material."
      },
      "why_matches": ["story-role fit: emotion, payoff"],
      "why_belongs_before_after": "Belongs late, ideally after context or escalation.",
      "relationship_expansion": [
        {
          "segment_id": "seg_122",
          "relationship_type": "build_up",
          "evidence": "Previous clip establishes the studio session."
        }
      ],
      "warnings": ["check_audio_transition"]
    }
  ]
}
```

### Story Planner

V1 uses a documentary beat grammar.

Default beat structure:

1. Hook.
2. Context.
3. Setup.
4. Problem.
5. Attempts/process.
6. Escalation or funny complication.
7. Resolution.
8. Payoff.

Planner outputs:

- Target duration.
- Tone.
- Audience assumptions.
- Explicit/implicit directive intent analysis.
- Beat list with duration ranges.
- Retrieval queries for each beat.
- Visual/storyboard queries for each beat.
- Must-have constraints.
- Nice-to-have constraints.
- Ending criteria.
- Candidate clips per beat and selected sequence.
- Context-aware caption plan.
- Transition notes and continuity warnings.
- Workflow graph draft: which tools/agents are required and what each step must produce.

### Select Casting

The casting step fills the beat sheet with candidate material.

Responsibilities:

- Retrieve candidates for each beat.
- Prefer transcript clarity for A-roll.
- Add B-roll, reactions, process shots, inserts, and beauty shots.
- Avoid repeating the same visual moment.
- Track coverage gaps.
- Provide alternates.

### Editor

The editor turns candidates into an ordered timeline plan.

Responsibilities:

- Choose a story spine.
- Trim starts/ends.
- Decide pacing.
- Insert B-roll over A-roll where appropriate.
- Keep audio continuity intelligible.
- Balance story clarity with directive tone.
- Produce reasons for timeline choices.

V1 should favor hard cuts and clean audio over fancy transitions.

### Timeline Compiler

The agent-facing timeline JSON is the editing source for the renderer. OTIO is exported as the interchange format.

Timeline JSON shape:

```json
{
  "timeline_id": "timeline_v001",
  "duration_target_sec": 180,
  "tracks": [
    {
      "kind": "video",
      "name": "A-roll",
      "items": [
        {
          "id": "item_001",
          "asset_id": "asset_001",
          "source_start_sec": 120.4,
          "source_end_sec": 132.2,
          "source_start_time": {"value": 2889.6, "rate": 24},
          "source_duration_time": {"value": 283.2, "rate": 24},
          "timeline_start_sec": 0,
          "timeline_end_sec": 11.8,
          "timeline_start_time": {"value": 0, "rate": 24},
          "timeline_duration_time": {"value": 283.2, "rate": 24},
          "pre_roll_sec": 0.2,
          "post_roll_sec": 0.4,
          "role": "hook",
          "reason": "Clear funny opening with immediate conflict.",
          "why_here": "Hook beat: matches directive terms and establishes the subject.",
          "before_context": "First impression; no prior setup required.",
          "after_context": "Should lead into context rather than another similar hook.",
          "caption_text": "Studio archive: the first spark",
          "transition_note": "Open cleanly; establish the subject before adding context.",
          "continuity_score": 0.82,
          "source_references": ["transcript_span_003", "observation_019"]
        }
      ]
    }
  ]
}
```

### Renderer

V1 renderer should be deterministic from timeline JSON.

Required:

- Trim and concatenate video clips.
- Basic audio normalization.
- Optional transcript captions.
- Hard cuts.
- MP4 output.
- Render manifest with command, inputs, output path, duration, and validation status.

Later:

- Crossfades.
- Music ducking.
- NLE exports.
- Captions styling.
- Graphics/title cards.

### Critic And Revision

Critique should produce patch operations.

Inputs:

- Directive.
- Beat sheet.
- Timeline JSON.
- Render manifest.
- Timeline item reasons.
- Sampled render frames/audio/transcript, when available.

Outputs:

- What works.
- What fails.
- Missing beats.
- Redundant or weak timeline items.
- Pacing issues.
- Replacement queries.
- Timeline patch operations.
- Self-evaluation score by criterion: story clarity, pacing, directive fit, audio continuity, visual diversity, ending strength.

Patch examples:

```json
[
  {
    "op": "replace_item",
    "item_id": "item_014",
    "replacement_query": "clearer reaction shot after the failed lighting setup",
    "reason": "Current shot repeats the same speaker and weakens the escalation."
  },
  {
    "op": "trim_item",
    "item_id": "item_008",
    "new_source_start_sec": 441.2,
    "new_source_end_sec": 448.0,
    "reason": "Remove dead air before the useful line."
  }
]
```

## Agent Contracts

Keep agents small and tool-driven.

| Agent | Owns | Does not own |
| --- | --- | --- |
| `IngestAgent` | Asset discovery and analysis job scheduling | Story decisions |
| `LibrarianAgent` | Segments, observations, reusable metadata | Timeline pacing |
| `StoryAgent` | Directive interpretation and beat sheet | Low-level rendering |
| `CastingAgent` | Candidate retrieval and alternates | Final sequence decisions |
| `EditorAgent` | Timeline plan and pacing | Media transcoding |
| `WorkflowAgent` | Tool graph construction and dependency tracking | Creative clip ranking |
| `TimelineAgent` | Timeline JSON and OTIO export | Creative clip choice |
| `RenderAgent` | FFmpeg render and validation | Editorial critique |
| `CriticAgent` | Review and patch suggestions | Direct DB mutation without patch tools |

Every agent action should emit a run event:

```json
{
  "run_id": "run_001",
  "agent": "EditorAgent",
  "event": "selected_clip",
  "entity_id": "seg_123",
  "message": "Selected as midpoint escalation because it combines laughter, visible reset, and a clear spoken problem.",
  "created_at": "2026-07-13T00:00:00Z"
}
```

## Tool Surface

Internal tools should be schema-first so they can serve the CLI, UI, and agents.

Initial tool set:

- `init_project(path)`
- `ingest_paths(project_id, paths)`
- `probe_asset(asset_id)`
- `create_proxy(asset_id)`
- `analyze_local(asset_id)`
- `transcribe_asset(asset_id)`
- `analyze_window(window_id, provider, budget)`
- `search_segments(query, filters, limit)`
- `get_segment(segment_id)`
- `find_alternates(segment_id, constraints)`
- `create_beat_sheet(directive, duration_sec)`
- `create_workflow_graph(directive, beat_sheet, autonomy_mode)`
- `run_workflow_node(node_id)`
- `cast_beat(beat, constraints)`
- `compile_timeline(beat_sheet, selections)`
- `export_otio(timeline_id)`
- `render_timeline(timeline_id)`
- `critique_render(render_id, directive)`
- `apply_timeline_patch(timeline_id, patch)`

## Data Model Draft

### Asset

```json
{
  "id": "asset_001",
  "path": "/Volumes/ShootDay/A001.mov",
  "sha256": "abc...",
  "duration_sec": 1840.2,
  "fps": 23.976,
  "width": 3840,
  "height": 2160,
  "codec": "h264",
  "has_audio": true,
  "created_at_source": "2026-06-01T14:32:00Z",
  "ingest_status": "ready"
}
```

### Segment

```json
{
  "id": "seg_001",
  "asset_id": "asset_001",
  "start_sec": 120.4,
  "end_sec": 138.9,
  "kind": "candidate_moment",
  "summary": "The director jokes that the easy shot has become the hardest shot.",
  "transcript": "We thought this would be the easy one...",
  "story_roles": ["conflict", "humor"],
  "quality_score": 0.82,
  "usable": true
}
```

### Select

```json
{
  "id": "select_001",
  "segment_id": "seg_001",
  "directive_id": "directive_001",
  "suggested_role": "midpoint_escalation",
  "score": 0.91,
  "trim_start_sec": 122.0,
  "trim_end_sec": 135.5,
  "reason": "Funny, story-relevant, visually shows crew reset, and has clean enough audio."
}
```

### Activity Label

```json
{
  "id": "label_001",
  "asset_id": "asset_001",
  "start_sec": 118.0,
  "end_sec": 139.5,
  "label": "speech",
  "score": 0.87,
  "source": "local_audio_energy",
  "params": {"threshold_db": -19.0},
  "suggested_action": "keep"
}
```

### Scene Boundary

```json
{
  "id": "boundary_001",
  "asset_id": "asset_001",
  "time_sec": 512.25,
  "detector": "pyscenedetect_adaptive",
  "confidence": 0.73,
  "reason": "Large content delta after handheld move settles."
}
```

### Relationship

```json
{
  "id": "rel_001",
  "from_entity_type": "segment",
  "from_entity_id": "seg_001",
  "to_entity_type": "segment",
  "to_entity_id": "seg_009",
  "relationship_type": "setup_payoff",
  "confidence": 0.81,
  "evidence": "Both segments discuss the same failed lighting setup; later segment shows the fixed shot."
}
```

### Workflow Node

```json
{
  "id": "node_001",
  "workflow_id": "workflow_001",
  "agent": "CastingAgent",
  "tool": "cast_beat",
  "depends_on": ["node_000"],
  "status": "complete",
  "inputs": {"beat_id": "beat_004"},
  "outputs": {"select_ids": ["select_001", "select_014"]},
  "self_eval": {"coverage": 0.82, "visual_diversity": 0.74},
  "retry_count": 0
}
```

### Memory

```json
{
  "id": "memory_001",
  "scope": "project",
  "kind": "style_preference",
  "key": "pacing",
  "value": "fast but not frantic; preserve funny reaction beats",
  "source": "user_revision_note",
  "confidence": 1.0
}
```

## UI Design Direction

The UI should feel like a working editor, not a dashboard or landing page.

Layout:

- Fixed app shell.
- Dense media library.
- Player with timecode and source metadata.
- Timeline strip always visible.
- Agent activity and reasoning panel.
- Search as a primary control, not hidden inside filters.

Core screens:

- Project Library: ingest status, assets, transcript/search, segments, selects.
- Edit Run: directive, beat sheet, cast selects, timeline choices, render state.
- Timeline Review: playback, tracks, item reasons, alternates, patch suggestions.
- Render Review: rendered video, critique, apply/reject revision operations.

Important UI principle:

The agent should be visible but not chatty. Show decisions, confidence, reasons, and blockers. Avoid long conversational explanations in the main editor surface.

## Implementation Milestones

### Milestone 0: Repo Skeleton

Deliver:

- Package layout.
- CLI entry point.
- Project config file.
- SQLite migration system.
- Logging/event model.

Acceptance:

- `ave init ./project` creates a valid project directory and database.

### Milestone 1: Ingest And Probe

Deliver:

- File discovery.
- Asset hashing.
- `ffprobe` metadata extraction.
- Idempotent ingest.
- Asset list command.

Acceptance:

- Ingest a folder and list assets with path, duration, resolution, codec, and audio status.

### Milestone 2: Local Windows

Deliver:

- Proxy generation.
- Thumbnail/frame-strip generation.
- Window generation.
- Audio energy and silence labels.
- Activity label tracks with configurable thresholds and pre/post-roll margins.
- Optional PySceneDetect boundary import with detector metadata.
- Motion/activity scoring.

Acceptance:

- A long raw clip produces windows with activity/silence/quality labels, scene-boundary hints, and frame-strip previews.

### Milestone 3: Transcript Search

Deliver:

- ASR provider interface.
- Transcript storage.
- Transcript search.
- Transcript-linked segments.

Acceptance:

- Search a phrase or concept from the transcript and jump to source timecode.

### Milestone 4: Gemini Selects

Deliver:

- Gemini provider adapter.
- Prompt/schema versioning.
- Analysis cache.
- Coarse window analysis.
- Focused select analysis.
- Source references on every select: transcript spans, observations, thumbnails, and time ranges.
- Relationship extraction for alternates, reactions, setup/payoff, visual proof, and duplicate moments.

Acceptance:

- Search for a story concept like "funny chaos before the final shot works" and get timestamped candidate selects with reasons.

### Milestone 5: First Timeline

Deliver:

- Directive parser.
- Beat planner.
- Visual/storyboard query generation.
- Candidate casting.
- Workflow graph for tool/agent execution.
- Timeline JSON.
- Rational time fields for frame-accurate OTIO compilation.
- OTIO export.

Acceptance:

- Generate a coherent 2-3 minute rough timeline from BTS/interview footage.

### Milestone 6: Render And Critique

Deliver:

- FFmpeg rough-cut render.
- Render validation.
- Critique schema.
- Self-evaluation scores for story clarity, pacing, directive fit, audio continuity, visual diversity, and ending strength.
- Patch operations.
- Revised timeline.

Acceptance:

- Produce `render_v001.mp4`, critique it, apply at least one concrete timeline patch, and render `render_v002.mp4`.

### Milestone 7: Review UI

Deliver:

- Local project browser.
- Media library/search.
- Dataset-style filtering by quality, person, story role, source, confidence, approval state, and rejection reason.
- Player.
- Timeline review.
- Agent activity panel.
- Workflow graph/run history view.
- Project memory and style preference view.
- Select approval/swap.

Acceptance:

- User can inspect a generated cut, see why each clip was chosen, swap one clip with an alternate, and render again.

## Recommended Initial Stack

Language/runtime:

- Python for media pipeline, FFmpeg orchestration, SQLite, ASR, OTIO, and CLI.
- Target Python 3.11 or 3.12 for the real development environment. The first standard-library slice can run on newer Python, but OpenTimelineIO and FiftyOne currently document support only through Python 3.12.
- TypeScript/React later for the review UI.

Python libraries:

- `typer` for CLI.
- `pydantic` for schemas.
- `sqlalchemy` or direct `sqlite3` plus migrations.
- `opencv-python` or FFmpeg filters for motion/quality analysis.
- `openai-whisper` or `faster-whisper` behind an ASR interface.
- `opentimelineio` for OTIO export.
- `scenedetect` as an optional local-analysis extra.
- `fiftyone` as an optional review/export extra, not a V1 hard dependency.
- `OpenTimelineIO-Plugins` only when exporting to adapter formats beyond native OTIO.

External tools:

- `ffmpeg`
- `ffprobe`

Model providers:

- Gemini adapter first for video understanding and text reasoning.
- Provider interface from day one to avoid locking core logic to a single API.

## First Coding Slice

The first coding slice should be deliberately unglamorous:

1. Create the Python package and CLI.
2. Implement `ave init`.
3. Create SQLite migrations.
4. Implement `ave ingest`.
5. Implement `ave assets`.
6. Add `ffprobe` metadata extraction.
7. Add idempotency by file path, size, mtime, and hash.

This gives the rest of the editor a spine. Once assets and project state are durable, every later agent has somewhere honest to put its work.

# Next Agentic Editing Handoff

Last updated: July 15, 2026

> **Revision note (July 15, 2026):** The critique/revision loop (CriticAgent +
> critique-driven RepairAgent passes) is dropped for now — LLM-scored review loops
> are ungrounded and every OSS project we studied that used one got no measurable
> quality from it. Constraint validation with automatic repair stays. In its place,
> a micro-timing layer (cut-point detection, snap-on-compile, renderer polish) is
> now implemented; see "Micro-Timing Layer" and "External Research Findings" below.

## Why This Exists

The current system can ingest media, produce transcript/semantic segments, build context cards, retrieve candidates, create rough beat plans, and render timelines. The recent 4-minute "small story created thru words" test exposed the central limitation: the pipeline can assemble constrained montages, but it does not yet author or preserve a real storyline.

The next iteration should supercharge the agentic layer from "rank and assemble clips" into "plan, cast, simulate, critique, and revise a coherent edit." The target is not perfect filmmaking. The target is a visibly smarter assistant editor that can explain narrative intent, detect when it has failed, and loop until the cut satisfies the directive better than a naive montage.

## Current Failure Pattern

Brief:

```text
create a 4 minute movie that features a small story created thru words, max shot length 5 seconds
```

What the current scaffolding did:

- Filtered for transcript-heavy or vocal segments.
- Rotated through generic roles such as `hook`, `context`, `process`, `performance`, `emotion`, `reaction`, and `payoff`.
- Created 48 timeline items at 5 seconds each.
- Rendered a valid 4-minute MP4.

What it did not do:

- Generate a premise, dramatic question, conflict, turn, or resolution.
- Build a causal chain where one line or moment changes the meaning of the next.
- Decide which words belong on screen, in audio, or in captions.
- Use critique as an inner loop before rendering.
- Detect that the output was structurally a montage, not a story.

## Ten User Stories That Should Drive The Next Iteration

1. **Word Story From Raw Footage**
   - As a user, I want a short movie whose storyline is built from spoken lines, captions, and lyric summaries, so the cut feels written rather than merely shuffled.
   - Success means the edit plan contains a premise, beats, selected word moments, and a final resolution.

2. **Find The Emotional Arc**
   - As a user, I want a 2-minute cut that starts restrained, becomes vulnerable, and ends joyful.
   - Success means emotional intensity changes across the timeline, not just individual clips tagged `emotion`.

3. **Make A Mini Documentary**
   - As a user, I want archive/context footage first, then process, then payoff.
   - Success means viewers can answer who/what/why before the performance peak arrives.

4. **Cut Around A Single Line**
   - As a user, I want the whole edit to orbit one memorable quote or lyric idea.
   - Success means the line is identified, introduced, echoed, and resolved through visual choices.

5. **Avoid Repetition**
   - As a user, I want no two adjacent clips to feel like the same shot, same meaning, or same song moment.
   - Success means the planner detects duplicate source, duplicate role, duplicate transcript idea, and duplicate visual style before rendering.

6. **Make A Trailer**
   - As a user, I want a 60-second trailer with a hook, mystery, escalation, title moment, and final sting.
   - Success means the plan has trailer-specific beats and not documentary beats with trailer language pasted on top.

7. **Build A Music-Driven Montage**
   - As a user, I want a high-energy music montage that cuts on perceived energy and avoids ugly audio jumps.
   - Success means the system plans audio continuity, crossfades, beat/rhythm hints, and warnings before render.

8. **Create A Character Portrait**
   - As a user, I want a portrait that reveals personality through behavior, words, reactions, and performance.
   - Success means the system tracks character traits and chooses clips that add new information.

9. **Make A Revision From Critique** *(deferred — critique loop dropped July 15, 2026)*
   - As a user, I want the agent to watch or inspect its own render, identify weak structure, and produce a revised timeline.
   - Deferred until critique can be grounded in measurable signals (duration match, audio-level discontinuities at cuts) rather than ungrounded LLM scores.

10. **Respect Hard Format Constraints**
    - As a user, I want a fixed duration, max shot length, required caption density, and no off-brief clip types.
    - Success means constraints are validated before and after timeline compilation, with automatic repair when violated.

## Target Agentic Flow

The next engine should separate story authorship, retrieval, casting, timeline compilation, and critique. The current `edit-plan` command should evolve into an explicit loop:

```text
directive
  -> IntentAgent
  -> StoryAgent
  -> QueryAgent
  -> RetrievalAgent
  -> CastingAgent
  -> SequenceAgent
  -> ConstraintAgent (validate + auto-repair)
  -> timeline compiler (snaps to detected cut points)
  -> render
```

### 1. IntentAgent

Input:

- User directive.
- Target duration.
- Hard constraints.
- Project collection summary.

Output:

- Explicit requirements.
- Implicit requirements.
- Genre/edit type.
- Required structure.
- Hard constraints.
- Success rubric.
- Failure modes to watch for.

Example:

```json
{
  "edit_type": "word_story",
  "hard_constraints": {
    "duration_sec": 240,
    "max_shot_sec": 5,
    "requires_visible_or_audible_words": true
  },
  "implicit_requirements": [
    "must have a premise",
    "must progress through verbal beats",
    "must not be only a performance montage"
  ],
  "success_rubric": {
    "story_clarity": 0.3,
    "word_continuity": 0.25,
    "constraint_fit": 0.2,
    "visual_variety": 0.15,
    "ending_strength": 0.1
  }
}
```

### 2. StoryAgent

The StoryAgent authors a narrative skeleton before any clip is chosen.

Output:

- Logline.
- Dramatic question.
- Beginning/middle/end.
- Beat sheet.
- Word spine: the spoken/caption/lyric-summary ideas that carry the story.
- Required evidence per beat.

For the failed test, a better StoryAgent might produce:

```json
{
  "logline": "An artist explains himself through fragments of love songs, studio asides, and performance moments until the music becomes the answer.",
  "dramatic_question": "Can the scattered words become one coherent confession?",
  "beats": [
    {
      "id": "beat_01",
      "function": "thesis",
      "word_need": "a line or summary that declares love/music as central"
    },
    {
      "id": "beat_02",
      "function": "human_interrupt",
      "word_need": "a candid aside or self-aware admission"
    },
    {
      "id": "beat_03",
      "function": "escalation",
      "word_need": "a stronger vocal statement or repeated phrase"
    },
    {
      "id": "beat_04",
      "function": "resolution",
      "word_need": "a line/chorus/outro that answers the opening thesis"
    }
  ]
}
```

### 3. QueryAgent

The QueryAgent expands each story beat into retrieval queries.

It should create:

- Transcript queries.
- Semantic queries.
- Visual queries.
- Relationship queries.
- Negative queries.
- Required source evidence.

Example:

```json
{
  "beat_id": "beat_02",
  "queries": {
    "transcript": ["I only know my part", "explains", "admits", "talking"],
    "semantic": ["candid process aside", "human personality moment"],
    "visual": ["studio conversation", "face reaction", "pause in performance"],
    "negative": ["chorus payoff", "repeated performance"]
  }
}
```

### 4. RetrievalAgent

Retrieval must return candidates that are useful for a specific beat, not generally high-scoring clips.

Each candidate should include:

- Exact source range.
- Transcript/semantic evidence.
- Relationship evidence.
- Why it satisfies the beat.
- What new information it adds.
- Risks.
- Adjacent compatibility.

### 5. CastingAgent

The CastingAgent fills beats with candidates and alternates.

It should reason about:

- Novelty versus repetition.
- Whether a clip adds a new word/story idea.
- Character or topic continuity.
- Audio continuity.
- Visual variety.
- Source diversity.
- Whether the candidate is being spent too early.

Output:

```json
{
  "beat_id": "beat_03",
  "selected": "seg_123",
  "alternates": ["seg_456", "seg_789"],
  "why_selected": "It escalates the verbal idea from private admission to sung commitment.",
  "risks": ["same song source as prior beat; use visual contrast or caption bridge"]
}
```

### 6. SequenceAgent

The SequenceAgent turns cast beats into an ordered edit. This is where shot order, timing, and transitions become first-class decisions.

Responsibilities:

- Place clips in a causal or rhetorical order.
- Break longer beats into shot-sized units.
- Decide whether the words should be heard, paraphrased, or captioned.
- Add bridges when source/time/style changes.
- Preserve the ending.
- Create a timeline plan that can be simulated before render.

### 7. ConstraintAgent

The ConstraintAgent validates the plan before render.

Checks:

- Duration target.
- Max shot length.
- Required number of beats.
- Required word density.
- No empty beats.
- No adjacent duplicate source unless justified.
- No invalid source timestamps.
- Enough ending duration to land the payoff.

It should return repair actions, not just warnings. Repairs are mechanical and
verifiable — swap a duplicate source, shorten an over-budget clip to the nearest
cut point, replace an empty beat with the top alternate — and each repair is
re-validated by the same constraint checks. There is deliberately no LLM-scored
critique/revision loop in this iteration (see revision note at top).

## Context Engineering Needed

The current context cards describe a segment. The next version needs context that describes how segments can compose.

### Segment Context Card v2

Add fields:

- `word_units`: important phrases, paraphrases, or lyric-summary ideas.
- `story_function`: thesis, setup, complication, turn, proof, reflection, payoff.
- `character_reveal`: what this moment reveals about a person.
- `causal_affordances`: what this clip can cause or answer.
- `setup_questions`: questions this clip raises.
- `payoff_answers`: questions this clip answers.
- `audio_affordance`: usable dialogue, music bed, abrupt song change, silence, noisy.
- `visual_affordance`: close-up, wide, reaction, process detail, performance, archive.
- `needs_caption`: whether words must be captioned to make sense.
- `caption_suggestions`: title-card or lower-third options.

### Story Memory

Add a project-level story memory table or JSON document:

- recurring characters.
- recurring topics.
- recurring phrases.
- recurring emotional states.
- source eras/styles.
- best opening candidates.
- best ending candidates.
- known duplicate groups.
- known audio incompatibilities.

### Relationship Graph v2

Relationships should include story semantics:

- `sets_up`
- `answers`
- `contradicts`
- `echoes`
- `escalates`
- `resolves`
- `duplicates`
- `requires_context`
- `works_before`
- `works_after`

## Proposed Data Tables

Additive schema candidates:

```text
story_blueprints
  id
  project_id
  directive_id
  logline
  dramatic_question
  blueprint_json
  source
  created_at

story_beats
  id
  project_id
  blueprint_id
  position
  function
  word_need
  visual_need
  emotional_target
  duration_target_sec
  created_at

beat_candidates
  id
  project_id
  beat_id
  segment_id
  score
  evidence_json
  risks_json
  selected
  created_at

timeline_constraint_reports
  id
  project_id
  timeline_id
  report_json
  repair_json
  created_at
```

## New CLI Surface

Suggested commands:

```bash
ave story-plan PROJECT --directive TEXT --duration-sec N --json
ave cast-beats PROJECT --story-plan-id ID --json
ave sequence-plan PROJECT --story-plan-id ID --json
ave validate-plan PROJECT --edit-plan-id ID --json
ave repair-plan PROJECT --edit-plan-id ID --json
ave edit PROJECT --directive TEXT --duration-sec N --max-shot-sec N --json
```

Already shipped (July 15, 2026):

```bash
ave cutpoints PROJECT --json            # shot changes + audio gaps -> scene_boundaries
ave cutpoints-summary PROJECT --json
ave timeline PROJECT ... --snap-tolerance-sec 1.0
ave render PROJECT --crossfade-sec 0.3 --burn-captions [--no-loudnorm]
```

`ave edit` should orchestrate the full loop. Lower-level commands should remain available for testing and inspection.

## Loop Skeleton

Pseudo-flow:

```text
run = start_edit_run(directive, constraints)
intent = analyze_intent(directive, constraints, collection_summary)
story = author_story_blueprint(intent, project_story_memory)

for beat in story.beats:
    queries = expand_queries(beat, intent)
    candidates = retrieve_candidates(queries)
    candidates = score_candidates_for_story_function(candidates, beat, story)
    cast[beat] = choose_candidate_and_alternates(candidates)

sequence = order_and_time_cast(cast, constraints)
report = validate_constraints(sequence, constraints)

while report.has_repairable_failures and run.repair_count < max_repairs:
    patch = propose_repairs(report, sequence, candidates)
    sequence = apply_patch(sequence, patch)
    report = validate_constraints(sequence, constraints)

timeline = compile_timeline(sequence)   # snaps every in/out to detected cut points
render = render_timeline(timeline)
```

## Acceptance Tests For The Next Iteration

1. A word-story brief produces a `story_blueprint` with logline, dramatic question, beginning/middle/end, and word spine.
2. A 4-minute max-5-second brief produces 48 timeline items, no item over 5 seconds, and an explicit constraint report.
3. The constraint repair loop can replace at least one weak or duplicate beat automatically.
4. Adjacent duplicate source usage is either avoided or explicitly justified.
5. A mini-documentary brief uses context/process before payoff.
6. A trailer brief uses trailer-specific beats rather than documentary beats.
7. A character-portrait brief includes at least three different trait revelations.
8. Every final timeline item has a `why_here` that references the story beat, not only search terms.
9. Every timeline item whose source range was truncated to fit the duration budget ends on a detected cut point (shot change or audio gap) when one exists within tolerance.
10. A rendered timeline with music across cuts shows no per-cut loudness jumps (single loudness pass, micro fades or crossfades at joins).

## Micro-Timing Layer (implemented July 15, 2026)

Story intelligence cannot feel natural if cuts land mid-word. This layer does the
precision work locally and reserves the LLM for selection:

- `cutpoints.py`: per-asset shot changes (ffmpeg scene score) and audio gaps
  (silencedetect at phrase resolution) stored in `scene_boundaries`.
- Timeline compiler snaps every LLM-approximate in/out point to the nearest cut
  point within `--snap-tolerance-sec`, with a 0.15s protective margin inside audio
  gaps (auto-editor's margin trick) so word attacks and decays survive.
- Budget truncation is content-aware: a clip shortened to fit the duration target
  pulls its out-point back to the nearest clean cut instead of chopping arbitrarily.
- Renderer: micro audio fades (50ms) at every hard cut, optional `--crossfade-sec`
  (xfade + acrossfade), optional `--burn-captions` (drawtext with graceful fallback),
  and one loudness pass over the whole timeline instead of per clip.
- Semantic/transcript prompts rewritten: cut-hygiene timing rules, verbatim
  `word_units` with tight timestamps, `story_function`, `setup_questions` /
  `payoff_answers`, audio/visual affordances, and `cut_notes` stating what each
  in/out point lands on. All new fields are stored, FTS-indexed, and surfaced in
  retrieval packets.

## External Research Findings (July 15, 2026)

From a survey of FunClip, LAVE (Meta IUI 2024), auto-editor, videogrep,
HKUDS/VideoAgent, poseljacob/agentic-video-editor, and video-db/Director:

1. **Never ask the LLM to invent timestamps.** FunClip's core pattern: give the
   model a timestamped transcript and make it QUOTE spans back in a rigid format.
   Adopt for all planner prompts: present segments as
   `[seg_042 | 12.40-15.92 | "text..."]` and require answers that reference IDs.
2. **Word-level timestamps gate the best features.** videogrep's phrase-level
   supercuts only work with word timing (Vosk / whisperX forced alignment). This is
   the next data investment: local ASR word alignment for speech-bearing ranges.
3. **Margin + merge post-processing** (auto-editor): pad kept regions ~0.2s,
   merge spans closer than ~0.5s. Margin is implemented; merge is TODO in the
   SequenceAgent.
4. **Dedicated TrimRefiner pass** (poseljacob): after casting, a second cheap LLM
   pass per clip that only tightens in/out points, separate from clip selection.
5. **Embedding retrieval over narrations** (LAVE): embed segment summaries +
   transcripts to break the lexical ceiling; cosine ranking in SQLite is enough.
6. **The LLM can only cut on what is serialized.** LAVE's key failure: trims fail
   when users reference properties absent from captions. Energy/motion/framing must
   be written into context cards, not hoped for.
7. **Energy-adaptive pacing** (BeatSync-Engine): hold shots longer in calm music
   sections, cut faster at drops — maps onto duration assignment in the sequencer.
8. Dead ends: LLM-scored review loops as quality gates (ungrounded), ShortGPT-style
   generate-from-script pipelines (not editors), cloud-locked stacks (Director).

## First Implementation Slice

Recommended order:

1. ~~Micro-timing layer~~ — done (see above).
2. ~~Word-level ASR alignment feeding word-boundary cut points~~ — done July 15,
   2026 (`ave align`, faster-whisper, `word_alignments` table, `asr_word` snap
   targets).
3. ~~Embedding-based cross-clip relationship mining~~ — done July 15, 2026
   (`ave export-cards` + `ave relate` via qmd vector search; typed
   duplicates/echoes edges in `relationships`).
4. Add `story_blueprints`, `story_beats`, `beat_candidates`, and `timeline_constraint_reports`.
5. Implement `story.py` with deterministic mock StoryAgent and optional Gemini StoryAgent.
   The StoryAgent prompt should quote verbatim `transcript_spans` (source
   `local_asr`) and `word_units` with their timestamps — the quote-spans-back
   pattern — never invent times.
6. Implement `constraints.py` for duration, shot length, duplicate, source validity, and required beat checks.
7. Replace `planner.py` beat generation with StoryAgent-generated beats; use qmd
   `query` (hybrid BM25+vector+rerank) as the candidate generator behind
   QueryAgent sub-queries, with sqlite as the source of truth.
8. Add one constraint-repair loop that can replace duplicate or weak beats.
9. Add tests for the 10 user stories above using mock providers.

## Important Principle

The system should stop treating "agentic" as "many smart labels stitched together." Agentic editing should mean the system forms an intention, tests its own plan against that intention, repairs the plan when it fails, and leaves behind inspectable reasoning at every step.

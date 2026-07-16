# Blanket Creative Decisions Audit

Date: July 16, 2026

Context: the user noticed every cut in a render was a fade — because `--crossfade-sec`
was a global render flag rather than a per-join editorial decision. That is now fixed
(per-item `transition_json` decided at timeline compile from join evidence; the flag
is only an override). This audit catalogs the *other* blanket-applied creative
decisions that should become context-dependent, ranked by contribution to the
"machine-made" feel. Each entry names the signals already in the database that could
drive the decision instead.

## Ranked findings

### 1. Equal time per beat + end-of-timeline squeeze (uniform rhythm)
**Status: FIXED (July 16, 2026)** — per-role pacing weights in `ROLE_PACING`, proportional `_fit_durations`, `_reserve_ending` minimum for the final clip, and a word-unit guard that never cuts a quoted line in half.
- `planner.py` `_beat_sheet`: `clip_target = duration_sec / len(roles)` — every beat
  gets identical target duration; a hook and a payoff breathe the same.
- `planner.py` `_assign_timing`: greedy left-to-right refit means later clips absorb
  all accumulated error — the payoff, placed last, gets squeezed hardest (this
  produced the 1.26s payoff in the demo render).
- Drive with: beat role / `story_function` (payoffs hold, hooks cut fast),
  `audio_affordance`, `word_units` durations (a clip carrying a line needs the
  line's full length), audio-gap density as an energy proxy.
- Lives in: planner timing pass emitting per-beat duration weights; timeline
  compiler honors word-unit minimums. **Uniform shot rhythm is the single
  strongest montage tell.**

### 2. One fixed documentary arc for every directive
**Status: OPEN** — deferred to the StoryAgent phase.
- `planner.py` `DEFAULT_BEATS` + clamp to 3-6 beats: trailer, word story, and
  mini-doc all get hook→context→process→emotion→payoff, and beat count does not
  scale with duration (a 60s target yields ~31s).
- Drive with: `intent_analyses` edit type, directive duration, corpus story memory.
- Lives in: the planned StoryAgent (the known headline gap; also silently caps
  total duration).

### 3. Start-anchored truncation of long selects
**Status: FIXED (July 16, 2026)** — `cutpoints.anchor_trim` anchors the kept window on the strongest word unit (hint-aware) or centers it; used by planner and timeline compiler, recorded as `trim_anchor` with a why. Also surfaced and fixed legacy selects whose trim ranges overran the asset duration.
- `planner.py` `_sequence_item` / `timeline.py`: an over-budget clip is always the
  *first* N seconds of the trim range. The truncation snap cleans the out-point but
  never questions the anchor; the best moment of a 30s segment is rarely its start.
- Drive with: `word_units` timestamps (anchor on the quotable line), `cut_notes`,
  `selects.reason`, cut-point clusters.
- Lives in: a TrimRefiner pass between casting and compilation (see research
  findings in the handoff doc).

### 4. Uniform max shot length
**Status: FIXED (July 16, 2026)** — beats emit `max_duration_sec` from role pacing; the compiler honors it with the CLI `--max-clip-sec` enforced as a hard ceiling even after cut-point snapping.
- `timeline.py` `max_clip_sec` caps every clip identically, conflating a
  directive's hard constraint with an internal pacing default.
- Drive with: beat role (payoffs looser, hooks tighter), `audio_affordance`.
- Lives in: planner emits per-item max; the CLI flag stays a hard constraint only.

### 5. Caption selection + styling are template-driven and all-or-nothing
**Status: FIXED (July 16, 2026)** — `_assign_captions` decides per item (`needs_caption`, context/archive role fallback) stored as `caption_decision_json`; the renderer honors it under `--burn-captions`. Styling/placement hints remain open.
- `context.py`: first generated caption always becomes the overlay at fixed 0.8
  confidence; caption text is a formula. `render.py --burn-captions` burns every
  item's caption in one style (white 18px bottom-center) regardless of framing or
  whether the segment `needs_caption` (stored, never consulted).
- Drive with: `needs_caption`, `visual_affordance` (placement), `caption_type`,
  `word_units` of kind `on_screen_text` (avoid double-captioning).
- Lives in: per-item caption field mirroring the transition fix.

### 6. Retrieval scoring weights ignore directive type
**Status: FIXED (July 16, 2026)** — `_weight_profile` selects word_driven / visual_driven / default profiles from directive terms; word-driven adds a spoken-word hit bonus. Default profile keeps legacy weights.
- `retrieval.py` `_score_segment`: fixed weights (terms x2.0, role fit +3, card
  +1.5, quality cap +2) rank identically for a word story and a visual montage;
  transcript/word_units evidence carries no extra weight for word-driven briefs.
- Drive with: `intent_analyses` edit type/tone (computed in the same module,
  unused for weighting).
- Lives in: per-directive (later per-beat) weight profiles in retrieval/QueryAgent.

### 7. Fixed segment inventory per asset
**Status: FIXED (July 16, 2026)** — `_semantic_prompt` scales the moment count with asset duration (~one per 20-30s, clamped 4-16).
- `semantics.py` prompt: "Pick 4-8 moments" for a 59s cover and a 279s archive
  tape alike. Inventory granularity bounds everything downstream.
- Drive with: asset duration, speech density from `word_alignments`.
- Lives in: the semantic prompt, parameterized per asset (~1 moment per 20-30s).

### 8. Hardcoded sequencing rules
**Status: FIXED (July 16, 2026)** — `_sequencing_policy` skips the payoff swap for trailer/teaser/loop directives and extends source novelty to all beats when assets >= beats; payoff casting prefers selects long enough to land an ending.
- `planner.py` `_ensure_payoff_last`: force-swaps the final clip on every edit —
  right for docs, wrong for cold-open trailers or loops.
- `planner.py` `prefer_new_asset=index < 3`: source novelty enforced only for the
  first three beats, then abandoned.
- Drive with: intent edit type; qmd `duplicates`/`echoes` edges (a real repetition
  signal that now exists and is not consulted here).
- Lives in: StoryAgent (ending policy) and CastingAgent (novelty budget).

### 9. Single loudness/dynamics target
**Status: FIXED (July 16, 2026)** — `--loudness-range` plumbs LRA through to loudnorm (default 11).
- `render.py` `loudnorm=I=-16:TP=-1.5:LRA=11` on every timeline. -16 LUFS is a fine
  delivery default, but uniform LRA=11 flattens a quiet-vulnerable → loud-payoff
  arc. Cheap fix: make LRA/filter a render parameter the planner can widen.

### 10. Uniform micro-timing constants (minor)
**Status: OPEN** — consolidation deferred.
- 50ms micro-fade, 1.0s snap tolerance, scene threshold 0.3, gap -30dB/0.12s,
  margin 0.15s, ASR pause 0.12s — each could key off `audio_affordance`
  (dialogue: tight tolerance; music: snap to beats once a beat grid exists).
  Low individual impact; consolidate into one config surface.

## Fine as globals (class B)
Codec/CRF/resolution/fps normalization, AAC 128k/48kHz stereo, proxy specs,
analysis window size, qmd relate thresholds (already CLI-tunable), 16kHz ASR
extraction.

## Already context-dependent (calibration, class C)
Per-item `why_here`/placement hints; snap_range's in/out asymmetry with
shot-changes exempt from margin; truncation snap only shrinking; role-conditional
context templates; drawtext graceful fallback; per-join transitions (fixed
July 16, 2026).

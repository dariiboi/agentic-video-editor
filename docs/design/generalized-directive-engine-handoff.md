# Generalized Directive Engine — Handoff

Last updated: July 16, 2026 (expanded with directive-spectrum handling, operation
frames, structure primitives, and the macro→micro intensity bridge)
Supersedes: the StoryAgent slice of `next-agentic-editing-handoff.md` (that doc's
research findings, micro-timing layer, and prompt patterns still apply).
Companion: `blanket-creative-decisions-audit.md` (what hardcoding already fell),
`next-steps.md` (documentary gaps, the t-shirt battle analysis).

## Mission

Turn the pipeline from "a documentary-montage tool with adjustable knobs" into a
directive engine that can take an arbitrary creative prompt — "create a battle
between green t-shirts and blue t-shirts", "make it feel like a memory fading",
"cut a recruitment ad from this chaos" — and (1) understand it, (2) invent an
ad-hoc narrative structure for it, (3) cast it from evidence actually present in
the index, and (4) compile and render it with the existing micro-timing machinery.

Generality has four axes, and each gets a section below:

1. **Directive spectrum** — prompts range from fully vague ("do something cool
   with this") to fully pinned ("open on the dog shot, 2s max cuts, end on the
   chorus"). The engine must know, per decision, whether the user decided it or
   the agent did.
2. **Operation breadth** — not every ask is "compose a new cut from the whole
   corpus": supercuts, subtractive cleanups, revisions of an existing cut,
   and reports are different operations sharing the same evidence base.
3. **Macro logic** — buildup, dramatic irony, pathos, motif, parallel action,
   withholding: expressible without a device list, via a small closed set of
   compositional primitives the agent combines freely.
4. **Micro logic** — the macro plan must land as concrete cut timing: intensity
   curves become shot durations and cut rates; juxtaposition quality is scored
   from facet evidence; beats can contain scenes (multiple shots), not one clip.

Nothing editorial may be hardcoded. Deterministic code remains for *execution and
validation* (snapping, clamping, constraint checks, ffmpeg) — that is good
hardcoding. What must go is every fixed editorial vocabulary and template.

## Two design decisions already made

### 1. Rich multi-facet ingest instead of on-demand re-analysis

The t-shirt battle prompt showed that retrieval can only serve attributes someone
extracted. Decision: pay more at preprocessing time and capture many
characteristics up front, rather than detecting evidence gaps at query time and
commissioning re-analysis. Two implementation options, benchmark both on 2-3
assets before committing:

**Option A — layered facet passes (recommended default).** Separate focused
prompts per facet, each independently versioned and re-runnable. Focused prompts
have far better per-facet recall than a kitchen-sink prompt (an LLM asked for
everything returns the salient). Critical cost insight: the expensive part of a
Gemini video request is the file upload, and `gemini_provider.py` currently
uploads and deletes per request. Refactor it to upload once per asset and run
all facet prompts against the same file handle (Files API keeps files 48h) —
then each extra facet costs tokens only.

**Option B — one-shot maximal extraction.** One request per asset with a system
prompt demanding exhaustive structured output across all facets. Cheaper, but:
`maxOutputTokens` is 8192 (a "note literally everything" answer for a 4-minute
video will truncate or go shallow), and per-facet quality degrades. Keep as a
`--budget` ingest mode; the prompt should still emit the same facet schema so
storage is identical.

Either way, a tiny gap-detection fallback may remain as a safety net (directive
space is open-ended; no facet set is complete), but it is tertiary — design for
ingest-time coverage first.

### 2. Facet catalog and storage

Facets, each stored as rows in the existing `observations` table (add
`start_sec`/`end_sec` real columns; `value` holds JSON; `observation_type` is
the facet name; `source` carries the facet-prompt version like
`gemini:people_appearance:v1`):

| facet | captures | drives |
|---|---|---|
| `people_appearance` | per-person: clothing garments + colors, hair, accessories, distinguishing features; stable per-asset person labels (P1, P2…) with time ranges | casting by visual attribute ("green t-shirts"), person continuity |
| `groups_interactions` | groupings (teams, uniforms, crowds), who interacts with whom, cooperative/competitive/neutral tone | faction narratives, relationship arcs |
| `actions_events` | granular actions with tight time ranges ("throws ball", "high-fives", "walks off") | beat evidence, alternation material |
| `setting_context` | location type, indoor/outdoor, era cues, weather, time of day | scene grouping, continuity, era bridging |
| `cinematography` | shot size, angle, camera motion, composition, lighting | visual variety, pacing, juxtaposition quality |
| `emotion_tone` | expressions, body language, mood trajectory within the clip | emotional arcs, escalation ordering |
| `objects_text` | notable props, on-screen text, logos, signage | motif tracking, caption decisions |
| `audio_character` | speech/music/ambient balance, energy curve, notable sounds | transition decisions, music-driven pacing |
| `editorial` (exists) | summary, word_units, story_function, affordances, cut_notes | current planner surface |

Everything lands in FTS and in the qmd cards (add facet sections to
`qmd_bridge._card_markdown`) so embedding search sees it. This is the LAVE
lesson operationalized: the agent can only cut on what is serialized.

Prompts follow the established patterns: rigid JSON contracts, evidence/why
fields, tight timestamps at pause/boundary points, verbatim-quote rules.

## Directive spectrum: operation frames and decision provenance

### Decision provenance (the spine of specificity handling)

Every editorial parameter in a plan — duration, shot ceiling, structure shape,
tone, content inclusions/exclusions, ending policy — carries a provenance tag:

- `user_explicit` — stated in the directive. Inviolable hard constraint;
  validated before and after compile; never silently overridden. If it cannot
  be met, the plan **fails loudly** with the reason (or offers a reduced-target
  variant clearly labeled as not meeting the ask).
- `user_implicit` — inferred from the directive ("recruitment ad" implies
  upbeat, implies an ask/CTA-shaped ending). Soft: the agent should honor it
  and must record the inference so it is inspectable and contestable.
- `agent` — everything the directive left open. Free choice, but every one
  must carry a `why` grounded in corpus evidence, per the no-blanket-decisions
  rule.

This tag rides through the whole pipeline: the IntentAgent assigns it, the
StructureAgent echoes it in `constraints_ack`, the compiler validates
`user_explicit` entries mechanically, and every timeline item's `why_here` can
cite it. Vague and precise prompts then need no separate code paths — a vague
prompt is simply one where almost everything is `agent`, a pinned prompt one
where almost everything is `user_explicit`.

### The vague end: footage-first authorship

For "make something cool from this" the IntentAgent has nothing to extract, so
the StructureAgent must pitch **from the footage**. Prerequisite: a cheap
deterministic `corpus_profile(project)` (no LLM) summarizing what the index
holds — asset count and durations, speech density, facet value histograms
(people, teams, settings, actions, moods, energy), strongest openers/enders by
existing scores, cluster hints from qmd `duplicates`/`echoes` edges. This
profile is prompt input to both IntentAgent and StructureAgent so invented
structures are grounded in what exists rather than in genre priors. The chosen
pitch is recorded with the alternatives considered (in the stored raw output,
not as extra render work).

### The pinned end: anchors

Directives may pin concrete content: "open on the shot of the dog", "use the
line about the flood", "end on the chorus". The IntentAgent extracts these as
`anchors` — content descriptions with a position requirement. The QueryAgent
resolves each anchor to a segment (or reports failure — an unresolvable
`user_explicit` anchor fails the plan loudly). Anchored items are fixed points;
the StructureAgent authors the structure *around* them.

### Conflicting or infeasible constraints

Over-specified prompts can conflict internally ("max 2s shots" + "let every
line finish") or with the corpus ("6 minutes" from 90s of usable footage). The
IntentAgent emits a `conflicts` list naming each tension; the plan records the
chosen resolution and its why. Resolution rules: `user_explicit` beats
`user_implicit` beats `agent`; two conflicting `user_explicit` constraints are
never silently traded off — the plan surfaces the pair and either fails or
picks one **with the tradeoff stated at the top of the plan output**, not
buried.

### Operation frames (wide-ranging asks)

The IntentAgent's first classification is not genre but *operation*: what is
the input, what is the output, what is the transformation. Emitted as:

```json
{
  "operation": {
    "sources": "corpus | subset(filter) | timeline(id)",
    "output": "timeline | revision | report",
    "mode": "compose | enumerate | subtract | transform"
  }
}
```

- **compose** — invent a structure and cast it (the main path in this doc).
- **enumerate** — the structure is a pattern over query matches: "every time
  someone says X", "all the crowd shots, chronological". No narrative
  invention; the structure document uses the `enumerate` beat pattern (below).
- **subtract** — keep-and-remove editing: "cut out the silences / the boring
  parts / every flubbed take". Operates on whole assets or an existing
  timeline; evidence comes from audio gaps, ASR, and facet observations
  (auto-editor/videogrep territory, but evidence-driven). Output is still a
  timeline; the "structure" is the kept-region list with whys.
- **transform** — a revision of an existing plan/timeline: "make the middle
  faster", "swap the second clip", "same cut but end on the wide shot".
  Requires plan lineage: load the prior plan, apply a *minimal targeted diff*,
  keep everything else byte-stable. Edit stability is a feature — a revision
  that re-rolls the whole cut is a failure even if the result is "good".

`mode` values are the closed set the code must execute; everything finer-
grained ("recruitment ad", "memory fading") stays a free-form `edit_type`
string. compose + enumerate ship in this phase; subtract + transform are
Phase C (below) but the operation frame and plan-lineage columns land now so
nothing needs re-architecting.

## Architecture: directive → ad-hoc structure → cast → compile

```text
directive + corpus_profile
  -> IntentAgent      (LLM)  operation frame, provenance-tagged requirements,
                             anchors, conflicts, evidence attributes, rubric
  -> StructureAgent   (LLM)  invents the narrative structure for THIS directive
                             from primitives (below)
  -> QueryAgent       (LLM)  per-beat sub-queries against facets + qmd + FTS;
                             anchor resolution; coverage report
  -> CastingAgent     (LLM)  fills beats from candidate packets (IDs only)
  -> compiler         (code) pattern expansion, intensity→timing, snapping,
                             transitions, constraint validation, repair
  -> render           (code)
```

### IntentAgent
Replaces `analyze_directive_intent`'s keyword table. Output: the operation
frame, free-form `edit_type` string (not an enum), explicit/implicit
requirements each with provenance, anchors, conflicts, hard constraints, the
*evidence attributes the directive pivots on* (e.g. `["t-shirt color", "team
membership", "confrontational action"]`), and a success rubric. The
evidence-attribute list is what the QueryAgent matches against facet types.

### StructureAgent (the core novelty — ad-hoc structures from primitives)

Does NOT pick from templates. It authors a structure document for this
directive by combining a small **closed set of mechanical primitives** — the
only things the compiler executes — with free text everywhere else (beat
functions, visual/word needs, juxtaposition guidance), which flows into casting
prompts and the why trail but never into a code branch.

The executable primitives, chosen because together they can express essentially
any macro device (see the table below) while staying mechanically expandable:

1. **beats** — ordered slots with free-string `function`, needs, and casting
   filters. A beat's `fill` may request **multiple shots** (a scene), with
   continuity keys naming which facets must agree across its shots
   (`setting_context`, person labels). One-clip-per-beat is the single biggest
   generality ceiling in the current planner — anything longer than a montage
   needs scenes.
2. **lanes** — parallel casting pools with filters (factions, characters,
   eras, A-story/B-story), plus convergence beats (`lane: null`).
3. **patterns** — mechanical expanders: `alternate` (across lanes),
   `enumerate` (beats generated from a query's matches, ordered
   chronologically / by escalation — this is how supercuts fall out of the
   same schema), `repeat` (n similar slots).
4. **motif slots** — a named slot cast once, whose material *recurs* at other
   marked beats (same segment, or same matched evidence attribute), each
   occurrence optionally transformed (shorter, re-trimmed, re-contextualized).
   Recurrence is the sanctioned exception to the novelty budget and must be
   recorded as such.
5. **ordering constraints** — `before` / `after` / `adjacent` /
   `never_adjacent` between beat IDs, validated mechanically after casting.
6. **intensity curve** — a per-beat scalar target (0..1) that the compiler and
   CastingAgent translate into micro decisions (next section). This replaces
   both `ROLE_PACING` and any fixed arc: buildup is a rising curve, pathos a
   late fall with long holds — shapes the agent draws, not names the code knows.
7. **information staging** — `withhold` (evidence attributes that must not
   appear before this beat) and `recontextualizes: beat_id` (this beat is cast
   to change the meaning of an earlier beat — the found-footage mechanics of
   dramatic irony, reveals, and mystery). Enforced as casting-time filters and
   post-cast checks using `setup_questions`/`payoff_answers` and relationship
   edges (`answers`, `contradicts`, `echoes`).
8. **ending policy** — free-form intent plus one mechanical flag the compiler
   already knows how to honor (reserve minimum ending duration or not).

Schema sketch (the t-shirt battle, showing several primitives at once):

```json
{
  "logline": "Two crews trade escalating moves until one owns the floor.",
  "constraints_ack": {
    "duration_sec": {"value": 75, "provenance": "user_explicit"},
    "ending": {"value": "aftermath, not a winner card", "provenance": "agent",
               "why": "no footage shows a decisive outcome; corpus_profile has strong low-energy closers"}
  },
  "lanes": [
    {"id": "green", "casting_filter": "people_appearance: green t-shirt"},
    {"id": "blue",  "casting_filter": "people_appearance: blue t-shirt"}
  ],
  "beats": [
    {"id": "b1", "function": "meet_faction", "lane": "green",
     "intensity_target": 0.3, "motif": {"slot": "m1", "occurrence": 1},
     "visual_need": "group identity shot, green shirts together"},
    {"id": "b2", "function": "meet_faction", "lane": "blue", "intensity_target": 0.3},
    {"id": "b3-b8", "pattern": "alternate", "lanes": ["green", "blue"],
     "function": "escalation", "intensity_target": [0.4, 0.9],
     "visual_need": "increasingly aggressive/energetic actions"},
    {"id": "b9", "function": "collision", "lane": null, "intensity_target": 1.0,
     "fill": {"shots": [2, 4], "continuity": ["setting_context"]},
     "visual_need": "both colors in frame OR fastest cut pair"},
    {"id": "b10", "function": "aftermath", "intensity_target": 0.2,
     "motif": {"slot": "m1", "occurrence": 2, "transform": "shorter, now reads as before-the-storm"}}
  ],
  "ordering_constraints": [{"type": "before", "a": "b1", "b": "b9"}],
  "juxtaposition_rules": ["adjacent lane shots should match action direction or contrast energy"],
  "transition_policy_hints": {"escalation": "hard cuts only", "aftermath": "allow dissolve"}
}
```

`function` values are free strings the StructureAgent invents; nothing
downstream switches on them — they exist for the `why` trail and the rubric.
`intensity_target` may be a scalar or a `[from, to]` ramp across a pattern.
Beat count, lane count, and scene sizes are the structure's decision, scaled to
duration — no clamp.

### Macro devices are emergent, not enumerated

The following table is **documentation and few-shot prompt material only** —
worked examples showing the primitives are sufficient. It must never become a
lookup table in code, and the StructureAgent prompt must instruct invention
("combine primitives for this directive"), not selection from these rows:

| device | primitive combination |
|---|---|
| buildup / escalation | rising intensity ramp; alternate pattern; novelty budget reserves the strongest material for late beats |
| pathos / elegy | late falling intensity; long-hold beat cast on `emotion_tone` evidence; word_need for a vulnerable line; dissolve-friendly transition hints |
| dramatic irony / reveal | motif slot: occurrence 1 cast "innocent", a later beat `recontextualizes` it; `withhold` keeps the reframing evidence out of early beats |
| callback / bookend | motif slot at first and last beats, second occurrence transformed shorter |
| parallel narrative | two+ lanes, alternate pattern, convergence beat |
| mystery / withholding | `withhold` on the key attribute; enumerate partial views before the full view |
| supercut | one `enumerate` beat from a query, chronological or escalating order |
| memory fading | enumerate/repeat with a falling intensity ramp; occurrences shorten; transition hints lengthen dissolves; audio energy falls |

If a directive implies a device these primitives cannot express, that is a
schema gap to log (store the structure's raw output and the failure), not a
license to add a device enum.

## Micro logic: from intensity to timing

The macro plan lands as cut timing through deterministic mappings the agent
parameterizes but never bypasses:

- **Intensity → duration.** `intensity_target` maps to a duration weight
  (higher intensity → shorter shots → faster cut rate) fed to the existing
  proportional fitter `_fit_durations`. The mapping function is code; its
  curve parameters may come from the structure document. `ROLE_PACING` becomes
  the fallback only when a structure omits intensity entirely.
- **Intensity → casting rank.** The QueryAgent surfaces facet-derived energy
  evidence per candidate (`audio_character` energy curve, `actions_events`
  granularity, `cinematography` motion) so the CastingAgent can match
  candidates to the beat's intensity, not just its topic.
- **Intra-beat rhythm.** A multi-shot beat distributes its duration across
  shots along an `intra_curve` (`steady | accelerate | decelerate`) — an
  escalation scene can tighten internally, mirroring the macro curve at micro
  scale.
- **Juxtaposition scoring.** Adjacency quality is computed, not vibed: code
  derives candidate-pair features from facets (shot-size delta, motion/action
  direction, energy delta, setting match) and puts them in the casting packets;
  the CastingAgent decides with them and the structure's free-text
  juxtaposition rules; `never_adjacent`/continuity violations are validated
  mechanically after casting.
- **Scenes and continuity.** Multi-shot beats cast against continuity keys
  (same `setting_context`, same person label) so a "scene" is shots that
  plausibly share a space and moment — the unit documentary and narrative
  grammar actually needs.
- **Everything already built stays.** Word-unit guards, trim anchoring,
  cut-point snapping, per-join transitions (LLM hints adjust *inputs* to
  `_decide_transition`, never bypass it), micro-fades, single loudness pass.
- **Cross-ref:** the a-roll/b-roll overlay track (`next-steps.md` gap 1) is
  the remaining micro-structural blocker for documentary grammar (J/L cuts,
  narration over cutaways). Schema is ready; compiler/renderer work slots into
  Phase C.

### QueryAgent + CastingAgent
QueryAgent turns each beat's needs into concrete queries: facet-filtered
observation lookups, qmd hybrid search, FTS. It also resolves anchors and emits
a **coverage report**: for each directive-critical evidence attribute, how many
candidates the index actually holds — zero coverage surfaces in the plan
instead of silently casting noise. Candidate packets are presented
FunClip-style — `[seg_042 | 12.40-15.92 | evidence…]` — and the CastingAgent
must answer in IDs with rationale; it never invents timestamps. Casting
respects lanes (a green beat only casts segments whose observations place a
green-shirt person in frame), honors `withhold` filters, matches intensity, and
spends a novelty budget across the whole sequence (motif recurrences are the
recorded exception).

### Compiler (deterministic, keeps everything already built)
Consumes the structure generically: expands patterns (`alternate`, `enumerate`,
`repeat`), fills multi-shot beats, maps intensity to durations via the existing
proportional fitter, honors word-unit guards, trim anchoring, cut-point
snapping, per-join transitions, ordering-constraint and continuity validation
with mechanical repair, and validates every `user_explicit` constraint before
and after fitting. The executable primitive set is **closed and versioned** —
schema growth is a deliberate decision, not a drive-by addition; a structure
using unknown constructs fails validation and falls back (see Risks).

## What gets deleted (the de-hardcoding list)

- `retrieval.ROLE_ALIASES`, default desired-roles list, keyword tone detection
  → IntentAgent output.
- `planner.DEFAULT_BEATS`, `_retrieval_hint`, `_role_matches_beat`,
  `_before_context`/`_after_context` maps, `_placement_hint` → StructureAgent
  output.
- `_ensure_payoff_last` and `_sequencing_policy` keyword lists (trailer/loop)
  → structure document ending policy.
- `_weight_profile` keyword triggers → IntentAgent evidence attributes.
- `ROLE_PACING` → per-beat `intensity_target` (table remains only as fallback).
- Fixed beat-count clamp (3-6) → structure decides beat count from duration.
- One-clip-per-beat in `_select_for_beat` → beat `fill` with scenes.

Keep: every executor and validator (snap, clamp, fit, transition executor,
constraint checks), mock providers, all storage.

## Acceptance tests

Mock-provider tests plus, where marked ⚑, a real run on suitable footage:

1. ⚑ **T-shirt battle**: "there are various teams with different t-shirt
   colors, create a battle between green t-shirts and blue t-shirts" → structure
   has two lanes + alternation; every cast segment's observations place the
   right color in frame; escalation beats shorten; render completes.
2. **Word story** (from the old handoff): structure contains a word spine of
   quoted verbatim spans with timestamps.
3. **Trailer**: no payoff-last swap; hook density high; structure functions are
   trailer-invented, not documentary defaults.
4. **Mini-doc**: context precedes payoff — emergent from the structure, not
   from `DEFAULT_BEATS` (which no longer exists).
5. **Nonsense-attribute directive** ("battle between red hats and top hats"
   where footage has none): QueryAgent coverage report names the zero-coverage
   attributes; plan surfaces the shortfall instead of silently casting noise.
6. Every timeline item's `why_here` cites its beat's invented function and the
   observation evidence that qualified it.
7. Facet ingest on one asset produces observations rows for ≥6 facet types with
   time ranges, all FTS-searchable and present in the qmd card.
8. One-shot budget mode produces the same schema (fewer rows is acceptable).
9. **Vague directive** ("make something with this footage that feels good"):
   IntentAgent tags essentially every parameter `agent`; the structure's
   logline and choices cite `corpus_profile` evidence; no error, no genre
   default.
10. **Pinned + over-constrained**: "open on the dog shot, 6 minutes" against
    90s of footage → the dog anchor resolves and lands first
    (`user_explicit`); the duration conflict is surfaced at the top of the plan
    with the chosen resolution; no silent override.
11. **Supercut** ("every time someone says 'love'"): operation frame is
    `enumerate`; every timeline item's evidence contains a matching verbatim
    span; order matches the requested ordering.
12. **Buildup** ("one continuous build in tension to the end"): cut rate
    (items per 10s) increases monotonically across the escalation region of
    the render; intensity targets in the structure form a rising ramp.
13. **Motif/callback**: a bookend directive produces the same motif slot at
    open and close, the second occurrence shorter, and the novelty check log
    records the reuse as sanctioned.
14. **Pathos ending**: final beat gets a long hold (duration ≥ the mechanical
    ending minimum, intensity ≤ the sequence median), cast on `emotion_tone`
    evidence, with a dissolve-permitting transition hint.
15. **Recontextualization pair**: a directive asking for a reveal produces a
    beat with `recontextualizes` pointing at an earlier beat, and the pair's
    casting evidence links via `answers`/`contradicts`/`echoes` or
    setup/payoff fields.
16. *(Phase C)* **Minimal-diff revision**: "make the middle faster" against a
    stored plan changes only middle-beat durations/cut count; opening and
    ending items are byte-identical; lineage links revision to parent plan.
17. *(Phase C)* **Subtractive**: "cut out the silences and dead moments" emits
    a kept-region timeline where every removed region carries evidence (audio
    gap, ASR silence, low-energy observation) and a why.

## Implementation order

**Phase A — evidence (unchanged):**
1. `gemini_provider`: upload-once/multi-prompt refactor (unlocks cheap facets).
2. `observations` start/end columns + facet prompt set + `ave facets` command
   (per-facet `--only`, versioned sources, resumable like other passes).
3. Facet data into FTS + qmd cards; verify retrieval finds "green t-shirt".

**Phase B — directive engine core:**
4. `corpus_profile(project)` — deterministic, no LLM; prompt input for both
   agents.
5. IntentAgent: operation frame, provenance tagging, anchors, conflicts,
   evidence attributes, rubric; deterministic mock. Store lineage columns
   (`parent_plan_id` on `edit_plans`) now, even though `transform` ships later.
6. Structure schema v2 (the eight primitives) + generic compiler expansion:
   patterns, lanes, motif slots, multi-shot fill, intensity→duration mapping,
   ordering validation. JSON-schema validation, minimal-linear-structure
   fallback, raw output stored either way.
7. QueryAgent/CastingAgent against facet-filtered packets: coverage report,
   anchor resolution, juxtaposition pair-features, intensity-aware ranking,
   novelty budget with motif exceptions.
8. Delete the hardcoded lists; keep fallbacks only where an agent may omit a
   field.
9. Acceptance tests 1-15; ⚑ tests on a real multi-team corpus if available.

**Phase C — operation breadth (after B lands):**
10. `subtract` mode (kept-region planning from gaps/ASR/facets).
11. `transform` mode: `ave revise` with plan lineage and minimal-diff
    guarantee (tests 16-17).
12. A-roll/b-roll overlay track (compiler + renderer; see `next-steps.md`).

## Risks / honest notes

- Facet ingest multiplies Gemini calls (~8-9 per asset in Option A). The
  upload-once refactor makes this tokens-only; still, add `--facets` selection
  and per-facet skip-if-done so cost is incremental.
- Person identity is per-asset only (P1/P2 labels). Cross-asset re-ID is out of
  scope; lanes work because casting filters on appearance, not identity.
- Whisper/Gemini disagreements on timing are already handled by snapping; facet
  time ranges should be treated as approximate and snapped the same way.
- An LLM-authored structure can be malformed: validate against a JSON schema,
  fall back to a minimal linear structure on failure, and store the raw output
  for inspection either way.
- **Primitive creep.** The compiler's executable set (patterns, lanes, motifs,
  ordering, intensity, fill, staging, ending flag) must stay closed and
  versioned. Any macro idea expressible as free text + casting guidance stays
  free text; only add a primitive when a device *provably* cannot land without
  mechanical support, and log the gap first.
- **Worked examples becoming templates.** The device table is prompt
  inspiration; if generated structures start converging on its rows across
  unlike directives, that's a prompt bug (test 3 partially guards this).
- **Provenance discipline.** The whole specificity story collapses if an agent
  quietly relabels a `user_explicit` constraint. Constraint validation must
  read provenance from the IntentAgent output, not from the structure document
  that echoes it.
- **Revision stability** (Phase C) is a hard guarantee to keep once compilation
  has stochastic inputs; the minimal-diff test must pin item identity, not just
  "looks similar".

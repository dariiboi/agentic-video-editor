# Generalized Directive Engine — Handoff

Last updated: July 16, 2026
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

## Architecture: directive → ad-hoc structure → cast → compile

```text
directive
  -> IntentAgent      (LLM)  arbitrary structured intent, no fixed genre list
  -> StructureAgent   (LLM)  invents the narrative structure for THIS directive
  -> QueryAgent       (LLM)  per-beat sub-queries against facets + qmd + FTS
  -> CastingAgent     (LLM)  fills beats from candidate packets (IDs only)
  -> compiler         (code) timing, snapping, transitions, constraints, repair
  -> render           (code)
```

### IntentAgent
Replaces `analyze_directive_intent`'s keyword table. Output: free-form
`edit_type` string (not an enum), explicit/implicit requirements, hard
constraints, the *evidence attributes the directive pivots on* (e.g.
`["t-shirt color", "team membership", "confrontational action"]`), and a
success rubric. The evidence-attribute list is what the QueryAgent matches
against facet types.

### StructureAgent (the core novelty — ad-hoc structures)
Does NOT pick from templates. It authors a structure document for this
directive. Schema must be expressive enough for parallel narratives:

```json
{
  "logline": "Two crews trade escalating moves until one owns the floor.",
  "lanes": [
    {"id": "green", "casting_filter": "people_appearance: green t-shirt"},
    {"id": "blue",  "casting_filter": "people_appearance: blue t-shirt"}
  ],
  "beats": [
    {"id": "b1", "function": "meet_faction", "lane": "green", "pacing_weight": 1.0,
     "visual_need": "group identity shot, green shirts together"},
    {"id": "b2", "function": "meet_faction", "lane": "blue", "pacing_weight": 1.0},
    {"id": "b3-b8", "pattern": "alternate", "lanes": ["green", "blue"],
     "function": "escalation", "pacing_weight": "decreasing",
     "visual_need": "increasingly aggressive/energetic actions"},
    {"id": "b9", "function": "collision", "lane": null,
     "visual_need": "both colors in frame OR fastest cut pair"},
    {"id": "b10", "function": "aftermath", "pacing_weight": 1.6}
  ],
  "juxtaposition_rules": ["adjacent lane shots should match action direction or contrast energy"],
  "transition_policy_hints": {"escalation": "hard cuts only", "aftermath": "allow dissolve"}
}
```

`function` values are free strings the StructureAgent invents; nothing
downstream switches on them — they exist for the `why` trail and the rubric.
Beat `pattern: alternate` expands mechanically in code. `pacing_weight`
replaces the hardcoded `ROLE_PACING` table (which becomes the fallback when the
agent omits weights).

### QueryAgent + CastingAgent
QueryAgent turns each beat's needs into concrete queries: facet-filtered
observation lookups, qmd hybrid search, FTS. Candidate packets are presented
FunClip-style — `[seg_042 | 12.40-15.92 | evidence…]` — and the CastingAgent
must answer in IDs with rationale; it never invents timestamps. Casting
respects lanes (a green beat only casts segments whose observations place a
green-shirt person in frame) and a novelty budget across the whole sequence.

### Compiler (deterministic, keeps everything already built)
Consumes the structure generically: expands patterns, applies pacing weights
via the existing proportional fitter, honors word-unit guards, trim anchoring,
cut-point snapping, per-join transitions (LLM hints adjust *inputs* to
`_decide_transition`, never bypass it), constraint validation with mechanical
repair. New: `contrasts_with`/lane-alternation awareness in continuity checks.

## What gets deleted (the de-hardcoding list)

- `retrieval.ROLE_ALIASES`, default desired-roles list, keyword tone detection
  → IntentAgent output.
- `planner.DEFAULT_BEATS`, `_retrieval_hint`, `_role_matches_beat`,
  `_before_context`/`_after_context` maps → StructureAgent output.
- `_ensure_payoff_last` and `_sequencing_policy` keyword lists (trailer/loop)
  → structure document ending policy.
- `_weight_profile` keyword triggers → IntentAgent evidence attributes.
- `ROLE_PACING` → per-beat `pacing_weight` (table remains only as fallback).
- Fixed beat-count clamp (3-6) → structure decides beat count from duration.

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
   where footage has none): QueryAgent reports which evidence attributes had
   zero coverage instead of silently casting noise; plan surfaces the shortfall.
6. Every timeline item's `why_here` cites its beat's invented function and the
   observation evidence that qualified it.
7. Facet ingest on one asset produces observations rows for ≥6 facet types with
   time ranges, all FTS-searchable and present in the qmd card.
8. One-shot budget mode produces the same schema (fewer rows is acceptable).

## Implementation order

1. `gemini_provider`: upload-once/multi-prompt refactor (unlocks cheap facets).
2. `observations` start/end columns + facet prompt set + `ave facets` command
   (per-facet `--only`, versioned sources, resumable like other passes).
3. Facet data into FTS + qmd cards; verify retrieval finds "green t-shirt".
4. IntentAgent + StructureAgent with deterministic mocks; structure schema +
   generic expansion (patterns, lanes) in the compiler.
5. QueryAgent/CastingAgent against facet-filtered packets.
6. Delete the hardcoded lists; keep fallbacks only where an agent may omit a
   field.
7. Acceptance tests above; ⚑ tests on a real multi-team corpus if available.

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

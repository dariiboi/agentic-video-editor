# Next Steps

Last updated: July 16, 2026

## Documentary-Style Footage Readiness

Assessment of running the current system on a corpus mixing interviews,
voiceless b-roll, and live footage.

### Works today (doc footage is the system's best-case input)

- Interview speech gives faster-whisper far more accurate word timestamps than
  the sung vocals in the demo corpus: dense, reliable `word_start`/`word_end`
  snap targets and verbatim quotable spans.
- The per-join transition rules were effectively designed for this mix:
  `clean_dialogue` joins hard-cut, joins into voiceless b-roll crossfade
  (`abrupt_audio_or_no_audio`), live music transitions blend.
- qmd relationship mining is the doc editor's core lookup: interview card
  ("describes the flood") ↔ b-roll card ("water damage wide shots") similarity
  is the interview→cutaway association.
- The hardcoded hook→context→process→emotion→payoff arc is least wrong for
  documentary.

### Gaps, in priority order

1. **A-roll/B-roll layering (the structural blocker).** The compiler emits one
   video track; each clip carries its own audio, so voiceless b-roll renders as
   silence. Documentary grammar is interview audio continuing under a cutaway
   (J/L cuts, narration over b-roll). Schema is ready (`timeline_items` has
   `track_kind`/`track_name`); the compiler and renderer are not. Contained
   extension of the clip-based renderer: mark items as cutaways, take video
   from the b-roll asset and audio from the extended interview range, mux per
   segment. Slot after or alongside the StoryAgent work.
2. **Soundbite-granularity casting.** Selects come from Gemini's 4-16 moments
   per asset; a doc cut wants "this complete sentence at minute 14." Verbatim
   ASR spans exist and are FTS-searchable but the planner does not cast from
   them directly yet — covered by the StoryAgent/word-spine phase.
3. **Speaker diarization.** Local ASR does not diarize; multi-interviewee
   corpora cannot do "only the director's answers." Add pyannote (or similar)
   if needed; superwhisper's speakerkit models are CoreML and not reachable
   from Python.
4. **B-roll findability** rests entirely on Gemini visual summaries (no
   transcript). Mitigated by `visual_affordance` and qmd embedding search.

Verdict: usable today for a selects-and-order assembly with clean dialogue
cuts; add the b-roll overlay track to make it documentary-capable. If a real
doc corpus is available, ingest it and run `scripts/demo.sh` — interview
alignment quality will show quickly whether the b-roll track is the only
blocker.

## Directive-Conditioned Evidence Gathering (gap surfaced by the "t-shirt battle" prompt)

Directives like "create a battle between green t-shirts and blue t-shirts"
expose a capability class the roadmap does not yet cover: the evidence needed
(shirt colors, faction membership) was never extracted at ingest because no
prompt asked for it, and the pipeline has no way to notice the gap and go get
it.

- Today the intent analyzer is a keyword/alias table; "battle between X and Y"
  falls through to the default documentary roles, and retrieval only finds
  green/blue shirts if Gemini summaries happened to mention clothing.
- The planned IntentAgent/StoryAgent can parse factions and author a
  versus-arc (meet the factions → provocation → escalation → climax →
  resolution), and beats with `visual_need` can express "green aggression
  shot" — so the planned architecture can *carry* the request.
- Missing piece: **evidence-gap detection → targeted re-analysis.** When the
  QueryAgent finds no indexed evidence for a directive-critical attribute, it
  should commission a cheap directive-conditioned Gemini pass over the proxies
  ("tag visible t-shirt colors per person, per segment") and store results in
  the existing `observations` table (already in the schema, currently unused).
- Also needed for versus-narratives: an alternation/parallel sequencing
  pattern (A-faction shot ↔ B-faction shot with escalating pace) and
  `contrasts_with` relationship edges — narrative-by-juxtaposition rather than
  narrative-in-the-clip.
- The t-shirt battle directive is a strong acceptance test precisely because it
  forces all three layers at once (intent parsing, evidence coverage, ad-hoc
  narrative structure). Adopted as a primary acceptance test in
  [generalized-directive-engine-handoff.md](generalized-directive-engine-handoff.md),
  which supersedes the on-demand re-analysis idea with multi-facet ingest.

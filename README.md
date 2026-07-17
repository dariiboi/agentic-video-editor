# Agentic Video Editor

Local-first agentic video editor for turning raw footage into autonomous rough cuts.

The first slice provides the durable project spine:

```bash
ave init ./project
ave ingest ./project /path/to/footage
ave assets ./project --json
```

Context-aware rough-cut workflow:

```bash
ave analyze ./project --json
ave cutpoints ./project --json                       # shot changes + audio gaps -> snap targets
ave align ./project --json                           # local word-level ASR (faster-whisper)
ave transcribe ./project --provider mock --json
ave semantic-analyze ./project --provider mock --json
ave context-build ./project --provider mock --json
ave export-cards ./project --json                    # markdown cards for qmd indexing
ave relate ./project --collection ave-project --json # embedding-mined relationships
ave context-search ./project "archive to live studio emotional payoff" --json
ave edit-plan ./project --directive "make a short documentary montage with a strong payoff" --duration-sec 45 --json
ave timeline ./project --directive "make a short documentary montage with a strong payoff" --duration-sec 45 --context-aware --json
ave render ./project --timeline-id latest --json     # add --crossfade-sec / --burn-captions
```

**Quick start for a new session:** run or read [`scripts/demo.sh`](scripts/demo.sh) —
it exercises the whole pipeline on `demo_projects/smoke_e_digglera`, maps each
module to the tables it writes, and ends with sqlite inspection one-liners.

See [docs/design/v1-agentic-video-editor.md](docs/design/v1-agentic-video-editor.md) for the V1 design.
See [docs/design/phase-execution-status.md](docs/design/phase-execution-status.md) for the current phase status against the dummy footage.
See [docs/design/generalized-directive-engine-handoff.md](docs/design/generalized-directive-engine-handoff.md) for the next build phase: arbitrary-directive handling with multi-facet ingest, ad-hoc narrative structures from compositional primitives, operation frames (compose/enumerate/subtract/transform), and decision provenance across the specificity spectrum.
See [docs/design/next-steps.md](docs/design/next-steps.md) for documentary-footage readiness and open gaps.

Development note: the current standard-library CLI slice runs on the local Python available here, but the intended full video stack should target Python 3.11 or 3.12 because planned dependencies such as OpenTimelineIO and FiftyOne currently document support through Python 3.12.

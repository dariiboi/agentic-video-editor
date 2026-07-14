# Agentic Video Editor

Local-first agentic video editor for turning raw footage into autonomous rough cuts.

The first slice provides the durable project spine:

```bash
ave init ./project
ave ingest ./project /path/to/footage
ave assets ./project --json
```

See [docs/design/v1-agentic-video-editor.md](docs/design/v1-agentic-video-editor.md) for the V1 design.
See [docs/design/phase-execution-status.md](docs/design/phase-execution-status.md) for the current phase status against the dummy footage.

Development note: the current standard-library CLI slice runs on the local Python available here, but the intended full video stack should target Python 3.11 or 3.12 because planned dependencies such as OpenTimelineIO and FiftyOne currently document support through Python 3.12.

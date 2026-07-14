# Third-Party Repo Audit

Last updated: July 13, 2026

This audit captures architectural lessons from adjacent open-source projects. It is a design reference only; do not copy third-party code into this repository.

## Sources Reviewed

- VideoRAG / Vimo: https://github.com/HKUDS/VideoRAG
- VideoRAG algorithm README: https://github.com/HKUDS/VideoRAG/tree/main/VideoRAG-algorithm
- VideoAgent: https://github.com/HKUDS/VideoAgent
- VideoAgent demos: https://github.com/HKUDS/VideoAgent/blob/main/demos_documents.md
- UniVA: https://github.com/univa-agent/univa
- OpenTimelineIO: https://github.com/AcademySoftwareFoundation/OpenTimelineIO
- PySceneDetect: https://github.com/Breakthrough/PySceneDetect
- auto-editor: https://github.com/WyattBlue/auto-editor
- FiftyOne: https://github.com/voxel51/fiftyone

## Key Additions To Our Design

1. Retrieval needs graph knowledge, multimodal context, and source references, not only semantic vectors.
2. The edit planner should produce a workflow graph whose nodes are agent/tool calls with dependencies, status, retries, and self-evaluation.
3. The story planner should generate visual/storyboard queries in addition to transcript queries.
4. Local analysis should produce reusable label tracks, including silence, speech, music, motion, dead-space, high-energy, keep-candidate, and cut-candidate.
5. Scene detection should store detector type, parameters, confidence, and reason because it is a hint, not ground truth.
6. Timeline data should keep rational time/frame-rate fields so OTIO export can be frame-accurate.
7. OTIO is an interchange format that references external media; it is not a media container.
8. NLE adapter exports beyond native OTIO may require `OpenTimelineIO-Plugins`.
9. Project memory should track style preferences, recurring people, vocabulary, and revision lessons across runs.
10. The review UI should borrow dataset-inspection ideas: filter, label, approve/reject, compare, and evaluate.
11. The full dependency environment should target Python 3.11 or 3.12 because planned video/editorial dependencies currently document support through Python 3.12.

## Immediate Roadmap Changes

- Milestone 2 now includes activity labels, configurable margins, and optional scene-boundary metadata.
- Milestone 4 now includes source references and relationship extraction.
- Milestone 5 now includes visual query generation, workflow graph construction, and rational time fields.
- Milestone 6 now includes explicit self-evaluation scores.
- Milestone 7 now includes dataset-style filtering, workflow history, and project memory review.

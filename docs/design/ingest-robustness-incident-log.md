# Ingest Robustness Incident Log

Date: July 29, 2026

Context: a real multi-day ingest of production footage (testimonials, then a
camera-footage batch) surfaced a cluster of bugs and process gaps in the
batch-processing passes (`transcribe`, `semantic-analyze`, `facets`, `proxies`,
`chunks`, `cutpoints`). This log is the chronological record, so the next
ingest — or a re-ingest of this same footage — doesn't rediscover the same
failures. See the `blanket-creative-decisions-audit.md` for the equivalent
audit on the editorial/creative side; this one is purely about ingest
pipeline reliability.

## Incidents, in order

### 1. `facets.py --limit` re-selected the same complete assets forever
**Status: FIXED** (commit `17b1bdf`, with regression test).
`_assets_with_video_ref(limit=limit)` sliced the raw ready-asset list (ordered
by path) *before* checking which assets still had pending facet work. With a
`--limit` smaller than the asset count, the same first-N-by-path assets kept
getting selected every resumed run; once those finished, every subsequent
invocation "wasted" its limit on already-complete assets and never advanced
to the backlog. This alone stalled a real 12-asset facet backlog at 2 done
for the better part of a day. Fix: compute pending status over *all* ready
assets first, then apply `--limit` only to the ones still needing work.

### 2. Gemini `maxOutputTokens` (8192) truncated JSON on real-length assets
**Status: FIXED** (commit `c819812`, raised to 65536).
Mock-provider tests never exercise real response length, so this was invisible
until real ~5-20 minute interviews produced JSON replies that got cut off
mid-array. Manifested as `ValueError: Model did not return valid JSON:
Unterminated string...` on `semantic-analyze` (100% failure rate on affected
assets) and intermittently on `transcribe`.

### 3. `media_resolution` unset on Gemini video requests
**Status: IMPROVED** (commit `9872cc9`).
Not a bug, but a real cost lever: Gemini bills video by sampled-frame-count ×
a resolution tier set by this parameter, independent of the uploaded file's
actual pixel resolution. Left unset, it defaults to a higher (more expensive)
tier. Set to `LOW` for all video calls — sufficient for editorial description
work (colors, actions, framing), does not affect timestamp precision.

### 4. Stale/degraded proxy files of unknown origin
**Status: WORKED AROUND, not systemically prevented.**
12 testimonial assets already had `media_artifacts` rows with
`artifact_type='proxy'` pointing at 360×202, ~30-65kbps files — using a
different naming convention (`{id}.mp4`) than the real `generate_proxy()`
function (`{id}_proxy.mp4`), meaning they predated the current `proxy.py` and
came from some other, unknown process. Gemini's file processor reliably
rejected these (`Gemini file processing failed`), and because `video_ref`
silently prefers any existing proxy row, the batch passes kept trying (and
failing on) this degraded content without any signal that the *proxy itself*
was the problem, not the source footage. Diagnosed by manually inspecting
proxy file dimensions/bitrate and comparing against a freshly-generated one.
Fixed operationally (deleted the bad rows/files, regenerated properly) but
**no code changes came out of this** — there is still no validation step that
would catch a degraded or malformed existing proxy before a pass trusts it.
Documented here as a known gap, not fixed in this pass (out of scope for the
three fixes below; worth a follow-up: a lightweight `ffprobe`-based sanity
check — minimum resolution/bitrate, or at least a successful decode — before
`_assets_with_video_ref`/`resolve_video_units` hands a proxy path to a
Gemini upload).

### 5. ffmpeg CPU oversubscription during ad hoc parallel proxy regeneration
**Status: OPERATIONAL LESSON, not a code bug.**
An ad hoc script ran 6 parallel `ffmpeg` proxy-generation workers on an
11-core machine; each `ffmpeg` process defaults to using most/all available
threads, so 6× oversubscription caused severe thrashing — one 20-minute
source file's proxy encode took 45+ minutes instead of the ~5-6 minutes a
clean single run took. The real `proxy.py`/`ave proxies` command processes
assets sequentially by default and was never affected; this was purely a
one-off manual script's mistake. No code change needed, but worth remembering
if a future `--jobs`-style parallel proxy/chunk feature is ever added: cap
per-process thread count or overall worker count well under core count.

### 6. xfade/acrossfade filter-graph deadlock on chains of short clips
**Status: FIXED** (commit `93c63cc`) — unrelated to ingest, included for
completeness since it was found during this same debugging arc. Chained
crossfades over clips shorter than the fade duration deadlocked ffmpeg's
filter scheduler (not a fade-math problem — proven via direct measurement
that no fade length is safe for long chains of short clips). Fixed with a
measured-safe envelope (≤3 crossfade joins, or all clips ≥1.5s use the
existing chained-fade graph; anything riskier falls back to a flat per-clip
`afade`+`adelay`+`amix` audio graph). This one was a clean, fully-closed fix —
no repeat issue since.

### 7. Gemini "file processing failed" recurring on already-regenerated proxies
**Status: FIXED** (commit `ce29066`), but see incident 9 — the same *symptom*
recurred afterward for an entirely different root cause, which briefly sent
investigation down the wrong path.
After incident 4's proxies were regenerated properly, the *same* error kept
recurring on some of them. Root-caused (via isolated A/B testing — a fresh
`genai.Client()` + fresh upload succeeded reliably where retrying via the
existing `GeminiVideoSession` did not) to `generate_json`'s retry loop reusing
the *same* client instance across retries within one call. Fixed by rebuilding
the client (not just deleting the failed file) whenever a `_is_file_failure`
retry fires.

### 8. Python stdout full-buffering hid background-job progress
**Status: OPERATIONAL LESSON, not a code bug.**
Several ad hoc diagnostic/orchestration scripts (`regen_proxies.py`,
`facets_clean.py`, `ilona_audio.py`, the `ave-drain*.sh` wrappers) appeared
hung for many minutes because Python fully buffers stdout when it isn't a
TTY — the underlying work was progressing (confirmed via file mtimes / DB
row counts), but `tail`/`cat` on the redirected log showed nothing until a
buffer flushed or the process exited. Recommendation for any future
background/orchestration script: run with `python3 -u` (or set
`PYTHONUNBUFFERED=1`), or explicitly `flush=True` on prints, so `tail -f`
actually reflects real-time state.

### 9. THE BIG ONE — an entire overnight run made zero progress on 18 camera assets
**Status: FIXED** (Fix A below prevents recurrence; the specific instance was
resolved operationally by moving the offending assets back to
`ingest_status='hold'`).

Root cause: 18 unrelated BOOM SOUND audio files (`.mp3`/`.wav`) had been
flipped to `ingest_status='ready'` during a *previous* batch's cleanup step
(a blanket `update assets set ingest_status='ready' where ingest_status in
('done_cut','hold_wav')`) and were never actually processed — they were
always out of scope for both the testimonials batch and the camera batch.
Every batch pass selects its next asset with `order by assets.path`, and
`'BOOM ...'` sorts alphabetically before `'CAMERA ...'`. None of the ad hoc
orchestration scripts driving the overnight run filtered the underlying
`ave transcribe`/`ave semantic-analyze`/`ave facets` CLI invocations by
folder — only a bash-side *reporting* query was scoped to camera paths, not
the actual work being dispatched. So every single `--limit 1` call spent its
entire attempt re-selecting the same stray BOOM MP3 (which, for reasons never
fully diagnosed, reliably failed Gemini's file-processing step — plausibly an
audio-only-file limitation of the `generate_video_json` code path, never
confirmed either way), producing the exact same `Gemini file processing
failed` error that had just been "fixed" in incident 7. That surface-level
match sent investigation down the client-retry path again for a while before
the actual selected asset was directly verified.

The underlying codebase gap: **no batch command had any way to scope a run to
a subset of assets** (a specific folder, a specific collection). Any future
multi-source ingest — or a re-ingest of this exact footage — is exposed to
the identical failure mode unless every operator remembers to manually
audit and correct `ingest_status` for every asset outside the current batch's
intended scope, every time. That's not a durable fix; it's a trap.

### 10. `cutpoints`/`analyze` reported "assets_completed: N" while fewer than
N assets actually got `scene_boundaries` rows
**Status: FIXED** (Fix C below).
During the overnight camera run, `cutpoints` reported `assets_completed: 18`
on *every single invocation*, yet only 9 of 18 camera assets ever persisted
`scene_boundaries` rows. Root cause: the command's only "already done" check
was "does this asset have any `scene_boundaries` rows" — but an asset can
legitimately produce *zero* scene-change points and zero audio-gap points (a
single static shot with no audio), which is indistinguishable from "never
analyzed" under that check. Compounding it: an `ffmpeg` failure (non-zero
exit code, e.g. on some of the largest/longest 4K source files) was being
silently treated as "ran successfully, found nothing" rather than a real
failure, because the scene/gap detectors returned an empty list on both a
clean "nothing found" run and a crashed run. See Fix C for the resolution.

### 11. ILONA.MOV's `audio_character` facet failed all 5 attempts
**Status: OPEN, not pursued further.**
A 20.6-minute testimonial's `audio_character` facet pass produced malformed
JSON (distinct syntax errors — unterminated strings, missing delimiters — at
different positions each attempt) across 5 consecutive tries. This looks like
a genuine model-output-quality limit on very long single-response JSON
arrays rather than an infrastructure issue (retrying more didn't help). Left
at 7/8 facets for that one asset; not worth chasing further without a
different mitigation (e.g. asking the facet prompt to chunk its own output,
or applying this project's chunking machinery down to sub-20-minute
sub-ranges purely for facet-response-size reasons, independent of the
existing 20-minute chunking threshold).

### 12. `qmd embed` alone doesn't pick up changed card content
**Status: DOCUMENTED, not a bug in this codebase.**
After facet data changed substantially, `qmd embed --collection american`
reported embedding only 6 of 149 documents — because `qmd embed` only embeds
content hashes it already knows about; a stale/changed card needs `qmd
update` (re-index) run first to detect the new content hash, *then* `qmd
embed` picks it up. Not an `ave` bug, just a usage order that isn't obvious
from the command names. Worth remembering for any future workflow that
regenerates cards after new facet data lands: always `qmd update` before
`qmd embed`.

## Systemic fixes applied in this pass

### Fix A — `--path-contains` scope guard (directly prevents incident 9)
Added a `--path-contains SUBSTR` option to `transcribe`, `semantic-analyze`,
`facets`, `proxies`, and `chunks` (substring match on `assets.path`, default
`None` = no filter, fully backward compatible). A future batch run can now be
scoped with e.g. `--path-contains "CAMERA"`, and cannot silently pick up an
asset from an unrelated folder regardless of that asset's `ingest_status`
history. This is the actual structural fix for incident 9 — the operational
fix (flipping BOOM assets back to `hold`) only patched the one instance.

### Fix B — audit for the incident-1 bug class elsewhere
Checked `transcript.py`, `semantics.py`, `proxy.py`, and `chunking.py` for the
same "`--limit` applied before pending-filtering" pattern fixed in `facets.py`
(incident 1). Finding: `transcript.py` and `semantics.py` were never affected
— their `_assets_with_video_ref` queries apply `not exists (...)` filtering
*in SQL*, before `limit` is applied, so a bounded run can only ever select
assets that still need work. `proxy.py` and `chunking.py` already carried the
fix from their original implementation (each cites commit `17b1bdf` in a
comment). No additional code changes were needed for Fix B beyond confirming
this by reading each module's selection query directly.

### Fix C — honest `cutpoints` completion tracking (fixes incident 10)
Added a `cutpoints_done` marker (a `media_artifacts` row, mirroring the
pattern `proxy.py`/`chunking.py` already use for their own completion
tracking) so "already analyzed" no longer depends on "has scene_boundaries
rows" — an asset that legitimately finds zero cut points is now correctly
recognized as done. `_detect_scene_changes`/`_detect_audio_gaps` now return
`(points, ok)` instead of just `points`, so a real `ffmpeg` failure
(non-zero exit) is counted in a new `assets_failed` field rather than
silently folded into "completed, found nothing." `--force` added to
`ave cutpoints` to allow deliberate re-analysis, matching every other batch
command's convention.

## Known gaps, not fixed in this pass (see incident numbers above)

- No validation of an *existing* proxy/chunk artifact's basic health before a
  pass trusts it (incident 4).
- No confirmed answer on whether audio-only (`.mp3`/`.wav`) files reliably
  fail Gemini's video file-processing path, or whether that was specific to
  the one stray BOOM file encountered (incident 9's aside).
- ILONA.MOV's one incomplete facet (incident 11) — low priority, single
  asset, single facet.

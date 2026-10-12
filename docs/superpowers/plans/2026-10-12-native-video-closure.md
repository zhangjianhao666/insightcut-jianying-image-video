# H3 Native Video Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development for bounded components; primary agent owns delivery integration and final verification.

**Goal:** Produce native H3 speech video with real subtitle timeline and strict duration, and report truthful progress.

**Architecture:** Keep paid shot generation unchanged. Add a resumable local delivery module after generation, snapshot options in job records, and expose a separate no-paid-call delivery retry. Validate director output before starting new H3 production.

**Tech Stack:** FastAPI/Python 3.11, local faster-whisper through an explicit executable/model path, FFmpeg/libass, React.

## Global Constraints

Preserve original clips and failed assets. Do not silently repeat paid calls. Keep native sound, use SubtitleWriter, no implicit model download, no credentials in logs. Exactly two existing trial shots and <=10 second delivery for acceptance. Remain on codex branch.

### Task 1: Director planning

Files: src/text/storyboard_director.py, tests/test_continuity_director.py, API director review/replan routes if needed.
- [ ] Reproduce actual failure with current model using a sanitized probe; inspect error category without printing raw response/credentials.
- [ ] Test failed plan does not become reusable success; short plan coverage must be complete; report sanitized stage/category and project ID.
- [ ] Implement error metadata and validation, root cause fix, explicit retry path; preserve old assets.
- [ ] Run `venv311/bin/python -m pytest tests/test_continuity_director.py -q`.

### Task 2: Local native delivery

Files: src/media/native_delivery.py, scripts/transcribe_native.py, src/media/video_first.py, src/api/video_first_routes.py, tests/test_native_delivery.py.
Interfaces: `deliver(paths, shots, output, *, max_duration, transcribe, stage_callback)` returns verified duration/subtitle/source metadata. `retry_delivery(task_id, segments, ...)` may never call shot_video_jobs.start.
- [ ] Write failing tests for late speech trimming, exact budget, overflow, missing audio/model/filter, partial asset retention.
- [ ] Implement bounded local recognizer command using configured Python/model paths; silence-aware timeline, script mismatch gate, SubtitleWriter SRT/VTT.
- [ ] Normalize native AAC, trim only safe silence, render all verified clips, burn captions with available libass binary; verify streams/duration/output file.
- [ ] Integrate immutable generation/voice signatures and delivery option snapshot; persist transcribing/subtitling/rendering/blocked stages. Add explicit local retry endpoint.
- [ ] Run focused tests and retry actual saved two-shot inputs locally; no paid provider submission.

### Task 3: Workspace state

Files: components/VideoFirstPanel.jsx, pages/WorkspacePage.jsx, frontend state helpers/tests.
Interfaces: API returns `status`, `delivery_stage`, `delivery_error`, `max_duration`, `subtitles`, `delivery_duration`; delivery-only retry uses `POST .../video-first/delivery` with subtitles/max_duration; new production accepts same fields.
- [ ] Test active/completed native flow overrides legacy TTS pause; other workflows unaffected.
- [ ] Show stage/error, strict total-duration and caption options, delivery-only retry button distinct from paid generation.
- [ ] Test options and button payloads; run frontend lint and relevant component tests.

### Task 4: Verification and documentation

- [ ] Synchronize AGENTS.md/CLAUDE.md with native delivery environment and failure semantics.
- [ ] Run backend ruff, focused regressions, frontend lint/build, contribution check.
- [ ] Inspect real captioned output and duration/codec/decode, API downloads/status; record remaining limitations.
- [ ] Review all changes for paid duplicates, unsafe subprocess input, credential logging, source mutation; commit on codex branch with verified identity. Update existing PR if publishing authorized, never merge failing CI.

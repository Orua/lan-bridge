# Changelog

## 0.2.31 — 2026-10-30

- Run CI tests through Python so repository-local test and version-script imports resolve on clean Windows runners, matching the verified desktop build entrypoint.


## 0.2.30 — 2026-10-30

- Add authenticated POST /v1 (and /v1/) protocol dispatch and /v1/chat shorthand; preserve existing Chat, Responses and WebSocket endpoints.
- Support per-model minimum output budgets and reasoning effort aliases for WorkBuddy probes and auxiliary requests to OpenAI API models.


## 0.2.29 — 2026-10-30

- Include the previously uncommitted proxy lane isolation/diagnostics, optional local ASR HTTP/WebSocket gateway, account catalog snapshots, desktop refresh action and test-state isolation in one complete source release.
- Isolate pass-through model snapshots by credential/account identity and ignore malformed persisted snapshot envelopes. Validate ASR worker health response shape.
- Keep optional ASR model weights and the independently installed recognition worker outside the desktop package; document its configuration and protocol.
- Require clean committed product source for release builds, isolate output by version, and publish RELEASE.json with the exact source commit alongside verified package hashes.

## 0.2.28 — 2026-10-30

- Unify Windows desktop installation and startup documentation, add Build/Start/Dev entrypoints, remove obsolete packaged-code patchers and unsupported platform targets, and add a tested Windows release workflow.

## 0.2.27 — 2026-10-21

- Serve an account-scoped model catalog from an atomic persistent snapshot. Check in the background every five minutes; require repeated matching observations before accepting a changed list.
- Retry missing GPT catalogs and upstream failures three times while retaining the last verified list. Return 503 rather than a partial custom-only catalog when no verified snapshot exists.
- Reread official login credentials on each attempt and audit catalog changes, timeouts, rate limits, access failures and login refresh requirements without logging credentials.
- Isolate mocked tests from operator audit and usage storage.

## 0.2.26 — 2026-10-21

- Require an explicitly delivered ASR final result before reporting success. Separate completed, cancelled, invalid-input, worker-failure, recording-idle and correction-timeout outcomes; client cancellations use logical 204 and do not count as server errors.
- Keep the 120-second correction deadline separate from recording packet idle timeouts. Clean up both relay directions on every terminal outcome.
- Record audio-specific API names, authenticated key identity, client address, actual recognizer, request IDs, first-text and correction timing in request/audit logs without retaining audio or recognized text.
- Local test worker preserves the 139-term hotword model and rejects overlapping sessions; the test page isolates callbacks from old sockets when recording again.

## 0.2.25 — 2026-10-21

- Add authenticated `/v1/audio/stream` WebSocket forwarding for 16kHz PCM, incremental text and final utterance correction. Bound frames, recording length and connection lifetime; keep audio and transcripts in memory only.
- The local demo now supports press-and-hold recording with live text and full-text correction on release, including touch, keyboard and focus-loss handling.

## 0.2.24 — 2026-10-21

- Add authenticated local Chinese transcription at `/v1/audio/transcriptions` and `/v1/audio/status`, with per-model access checks, bounded multipart forwarding, sanitized failures and local-only worker URLs.
- Optionally start an independently installed local FunASR worker. Keep model weights and PyTorch outside the desktop package. Provide a loopback-only ASR-scoped demo session whose plaintext key stays in memory and is revoked on bridge shutdown.

## 0.2.23 — 2026-10-21

- Isolate official proxy traffic across up to four lazily created connection lanes. A failed VPN handshake retires its lane while preserving other warm clients; sequential reuse and total admission limits remain unchanged.
- Record connection stages on bounded connect-only retries and return a Chinese proxy/VPN diagnostic for native Responses connection timeouts. No read/write, HTTP or partial-stream replay is added.

## 0.2.15 — 2026-10-17

- Add an independent Windows Task Scheduler watcher that checks the desktop PID every five seconds and recovers unexpected whole-app exits. Manual stop/quit persists recovery intent; recovery launches stay in the tray and respect the existing login-start preference.
- Retry upstream connection setup failures at most twice with 0.5s/1s backoff for native Responses, custom Responses, and Chat routes. Never retry HTTP errors, read/write failures, or an already-started stream; cancellation stops retries immediately.
- Validate recovery state atomically, verify executable identity, and throttle repeated desktop starts. Keep recovery state and rotated diagnostic logs under the user's local application data.
- Validation: 379 Python tests and 10 subtests; desktop typecheck/build and 20 tests. After all seven deployed desktop/backend processes were force-terminated, the independent watcher restored a healthy 0.2.15 instance in 5.64 seconds. Native Chat and custom Responses streaming requests then passed.

## 0.2.14 — 2026-10-16

- Keep the last valid configuration when hot-reload normalization fails, including syntactically valid YAML with invalid provider/server structures.
- Close upstream clients on cancellation, request preparation errors, and failed error-body reads. Shield cleanup during stream cancellation and cancel pending heartbeat reads when a client disconnects.
- Observe custom-provider Responses streams with the shared SSE parser, preserving bytes and recognizing multiline or unterminated final events. Distinguish upstream failure/truncation (502) from client cancellation (499).
- Honor manual stop and application quit during asynchronous startup probes. Make the watchdog respect scheduled restart backoff instead of restarting early.
- Include startup-race and existing Codex configuration preservation tests in the desktop release checks.
- Validation: 363 Python tests and 10 subtests; desktop typecheck/build and 18 tests; packaged backend hash/CSP/version checks; deployed native Chat and custom Responses streaming requests passed.

## 0.2.13 — 2026-10-14

- Expose native hosted image generation/editing to Chat Completions clients, including WorkBuddy custom models and compacted continuations. Respect explicit tool opt-out and preserve image inputs.
- Translate completed image results into preview/download links for streaming and JSON replies; persist each final image once and report delivery failures instead of empty success.
- Serve generated PNG/JPEG/WebP files through random, image-only download URLs. Anyone with a download URL can view that image; API credentials are never embedded in links. Store images on G: when available, or use `LAN_BRIDGE_IMAGE_OUTPUT_DIR`.
- Validation: 341 Python tests and 10 subtests, desktop typecheck/build and 11 Electron tests; a real native `gpt-6.1-sol` Chat request generated an apple PNG and its download returned the saved bytes.

## 0.2.12 — 2026-10-10

- Discover the live ChatGPT account catalog using the host Codex version, so older downstream clients no longer hide new models such as `gpt-6.1-sol`.
- Show account catalog availability and provide a refresh action in the model library; retain both Codex and OpenAI-compatible model list formats.
- Record native Responses and native Chat first-response latency locally, independent of upstream timing headers.
- Distinguish client cancellations from upstream failures, incomplete responses, and truncated streams; recognize final SSE events without a trailing newline while forwarding original bytes unchanged.
- Close native HTTP clients when credential preparation or upstream error-body reads fail.
- Validation: 338 Python tests and 6 subtests, desktop typecheck/build and 11 Electron tests; live `gpt-6.1-sol` Responses tool/context roundtrip, Chat Completions, and streamed command tasks plus same-session followups on local and 113 Agents passed. The deployed runtime reports 0.2.12 and records native first-response timing.

## 0.2.11 — 2026-09-23

### Changed

- Allow any valid LAN BRIDGE access key to request a model by its ID without first adding that model to the local library. Explicit custom provider routes and disabled aliases keep their configured routing behavior. Upstream account access still determines whether a native model can run.
- Remove per-model permission controls from the access-key screen. Newly issued and existing valid keys allow all model IDs; key revocation and enable/disable controls remain available.
- Add GPT-6 Astra, Sol, and Luna to the native model defaults. Merge new defaults into older native-model configurations while preserving explicit overrides, and include models returned by the host Codex catalog in the desktop model library.
- Keep native image input, generation, and editing on capable Codex routes, using configured media routes only where needed.

### Fixed

- Keep the two `/v1/models` response shapes consistent when access-key permissions are applied.
- Preserve upstream model errors when an unconfigured model ID is forwarded through Chat Completions.

### Validation

- Python suite: 332 tests and 6 subtests passed.
- Desktop typecheck, Electron tests, backend build, and Windows packaging passed.
- Deployed runtime reported version `0.2.11`; the model library returned `gpt-6-astra`, `gpt-6-sol`, and `gpt-6-luna`.

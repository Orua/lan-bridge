# Changelog

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

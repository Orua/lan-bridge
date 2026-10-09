# Changelog

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

# Changelog

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

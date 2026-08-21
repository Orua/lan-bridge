# LAN BRIDGE

![LAN BRIDGE logo](desktop/assets/icon.png)

LAN BRIDGE is a local OpenAI-compatible gateway with a Windows desktop manager. It translates OpenAI Responses API traffic to provider-compatible Chat Completions calls, while keeping provider credentials and runtime configuration on your own machine.

> [!IMPORTANT]
> LAN BRIDGE is an independently maintained derivative of
> [git-liu835/codex-cn-bridge](https://github.com/git-liu835/codex-cn-bridge),
> formerly named `code CN Bridge`. It is not an official upstream release and
> is not maintained or supported by the upstream author. This repository was
> created from a substantially modified working tree with a new, squashed Git
> history, so GitHub does not display it as a platform-level fork. See
> [NOTICE.md](NOTICE.md) for attribution and provenance details.

## Differences from upstream

The exact upstream base commit was not preserved when this public repository
was created. The comparison below describes the material differences verified
against upstream `git-liu835/codex-cn-bridge` v0.3.22 and this repository's
initial public release.

| Area | LAN BRIDGE changes |
| --- | --- |
| Identity and local state | Renamed the public product, CLI command, application identifiers, executable, configuration file, environment-variable prefix, user-data directory, and UI links from `code CN Bridge` to `LAN BRIDGE`. |
| Responses compatibility | Extends Responses API translation, continuation handling, tool-call normalization, streaming recovery, WebSocket handling, image/vision flows, Web Search rounds, context compaction, and usage reporting for Codex-style clients. |
| Routing | Adds strict per-model provider routing, separate native Responses and Chat-compatible paths, custom provider slots, fallback controls, proxy support, and configurable context limits. |
| Providers and desktop controls | Adds an OpenAI-compatible adapter and expanded dashboard controls for model capabilities, native/custom routing, search, configuration import/export, and diagnostics. |
| Security and privacy | Uses a redacted example configuration, keeps live credentials outside Git, removes secrets from configuration exports, avoids shipping company-specific paths and test data, and defaults to loopback-only binding. |
| Reliability and tests | Adds connection reuse, bounded logging and statistics, lifecycle recovery, packaged-backend verification, CI, and a substantially expanded Python/Electron regression suite. |
| Packaging scope | Focuses the maintained release flow on Windows installer/portable builds with one unified application/tray icon. Upstream auto-update and macOS/Linux release paths are not carried as supported LAN BRIDGE release flows. |

These changes make the projects behaviorally different. Upstream issues and
support requests should be reproduced against upstream before being reported
there; LAN BRIDGE-specific issues belong in this repository.

## Features

- OpenAI-compatible `/v1/responses`, `/v1/chat/completions`, and image-generation endpoints.
- Model aliases and provider routing for Qwen, DeepSeek, Kimi, GLM, Doubao, OpenAI-compatible services, and custom slots.
- Streaming translation, tool-call handling, usage statistics, request logs, and configuration hot reload.
- Optional native Codex routing, Web Search integration, and model context-window controls.
- Electron desktop manager with dashboard, provider/model configuration, logs, themes, language settings, tray operation, and launch-at-login support.
- Credentials are read from local YAML or environment variables and are removed from configuration exports.

## Security model

This repository contains no production API keys or personal runtime configuration. Do not commit `.lan-bridge.yaml`, `.lan-bridge.env`, `.env`, logs, captures, or exported credentials. Keep the server bound to `127.0.0.1` unless you understand the network exposure and have enabled appropriate access controls.

## Requirements

- Python 3.10+
- Node.js 18+ and npm (desktop development and packaging only)
- Windows 10/11 for the packaged desktop application

## Quick start: backend

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e .
lan-bridge init --provider qwen
$env:QWEN_API_KEY = "your-key"
lan-bridge start
```

The default endpoint is `http://127.0.0.1:8765/v1`. The default configuration file is `%USERPROFILE%\.lan-bridge.yaml`. You can start from [config.example.yaml](config.example.yaml) and set credentials through environment variables instead of writing keys into YAML.

## Desktop development

```powershell
cd desktop
npm ci
npm run typecheck
npm run test:electron
npm run electron:dev
```

## Build a Windows release

```powershell
.\scripts\build-backend.ps1
cd desktop
npm ci
npm run electron:build
```

The backend is written to `dist-backend/`. Electron Builder produces the installer, portable package, and unpacked application in the configured `lan_bridge_release` directory. All application surfaces use the same logo master from `desktop/assets/logo-master.png`.

## Configure Codex or another OpenAI-compatible client

Use these client values after LAN BRIDGE is running:

```text
Base URL: http://127.0.0.1:8765/v1
API key: any non-empty value unless LAN BRIDGE API filtering is enabled
Model: one of the aliases configured in .lan-bridge.yaml
```

The desktop Settings page can update the local YAML, import/export redacted configuration, and switch Codex between LAN BRIDGE and official OpenAI routing.

## Tests

```powershell
pytest -q
cd desktop
npm ci
npm run typecheck
npm run test:electron
```

## Project layout

- `code_cn_bridge/` — Python gateway, protocol translation, adapters, admin API, routing, and statistics.
- `desktop/` — React + Electron desktop manager and shared logo assets.
- `tests/` — Python regression tests.
- `tools/` — diagnostic clients and packaging helpers.
- `lan-bridge.spec` — reproducible PyInstaller backend definition.

## License

MIT. See [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md). Upstream attribution is
retained because substantial portions of this project are derived from
`git-liu835/codex-cn-bridge`.

# LAN BRIDGE

![LAN BRIDGE logo](desktop/assets/icon.png)

LAN BRIDGE is a local OpenAI-compatible gateway with a Windows desktop manager. It translates OpenAI Responses API traffic to provider-compatible Chat Completions calls, while keeping provider credentials and runtime configuration on your own machine.

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

MIT. See [LICENSE](LICENSE).

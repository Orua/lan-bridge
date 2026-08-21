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

## 中文说明：与上游版本的关系和差异

LAN BRIDGE 是基于上游项目
[git-liu835/codex-cn-bridge](https://github.com/git-liu835/codex-cn-bridge)
修改形成的独立维护版本，上游项目原名为 `code CN Bridge`。本项目不是上游官方发布版，
上游作者不负责 LAN BRIDGE 的修改内容、构建产物、技术支持或安全决策。

本公开仓库是从经过大量修改的本地工作区重新建立，并使用了压缩后的全新 Git 历史，
因此 GitHub 页面不会自动显示“Forked from”标记。新历史没有保留当时所基于的精确上游
提交号；以下内容是以当前可核验的上游 v0.3.22 与 LAN BRIDGE 首个公开版本为参照整理的
主要差异。

| 对比范围 | LAN BRIDGE 相对上游的主要变化 |
| --- | --- |
| 品牌与本地状态 | 将公开产品名、CLI 命令、应用标识、可执行文件、配置文件、环境变量前缀、用户数据目录和界面链接由 `code CN Bridge` 统一调整为 `LAN BRIDGE`。 |
| Responses 兼容性 | 扩展 Responses API 转换、连续会话、工具调用规范化、流式恢复、WebSocket、图片与视觉流程、Web Search 多轮调用、上下文压缩及 Codex 客户端用量统计。 |
| 路由机制 | 增加严格的逐模型提供商路由，将原生 Responses 与 Chat 兼容路径分开，加入自定义提供商槽位、回退控制、代理支持和可配置上下文上限。 |
| 提供商与桌面控制 | 增加 OpenAI 兼容适配器，并扩展模型能力、原生/自定义路由、搜索、配置导入导出和诊断等桌面管理功能。 |
| 安全与隐私 | 使用脱敏示例配置，将真实凭据保留在 Git 之外；配置导出会移除敏感信息，同时排除公司内部路径、专用测试数据，并默认只监听本机回环地址。 |
| 稳定性与测试 | 增加连接复用、有容量上限的日志与统计、生命周期恢复、打包后端校验、CI，以及更完整的 Python/Electron 回归测试。 |
| 发布范围 | 当前维护重点是 Windows 安装版和便携版，并统一应用与托盘图标；上游的自动更新及 macOS/Linux 发布流程不属于 LAN BRIDGE 当前支持的发布范围。 |

两个项目现在的行为和发布方式已经明显不同。只有能在上游原版中复现的问题才适合提交给
上游；LAN BRIDGE 特有的问题应在本仓库反馈。更完整的来源与署名信息见
[NOTICE.md](NOTICE.md)。

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

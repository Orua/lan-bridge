# LAN BRIDGE

![LAN BRIDGE logo](desktop/assets/icon.png)

LAN BRIDGE turns one computer into an OpenAI-compatible bridge for trusted
clients on the same local network. Clients use one LAN address and a stable set
of model aliases; the bridge selects the configured upstream provider,
translates protocols and tool calls when required, forwards the request with
the provider credential stored on the bridge host, and streams the normalized
response back to the client.

## What LAN BRIDGE does

Run LAN BRIDGE on a computer that can reach your upstream model services, then
point Codex CLI, Codex Desktop, VS Code extensions, or another OpenAI-compatible
client at that computer. Multiple trusted devices on the LAN can share the same
bridge endpoint and routing configuration without copying every provider key to
every client.

```text
Codex / VS Code / other OpenAI-compatible clients
                         │
                         │  LAN: http://<bridge-ip>:8765/v1
                         ▼
                    LAN BRIDGE
       model alias → provider routing → protocol/tool/stream conversion
                         │
                         ▼
       Qwen / DeepSeek / Kimi / GLM / Doubao / OpenAI-compatible APIs
```

The bridge accepts OpenAI-style Responses, Chat Completions, model-list, and
image-generation requests. Depending on the selected model route, it either
passes a native Responses request to a compatible upstream or converts it to a
provider-compatible Chat Completions flow, then converts the result back into
the response shape expected by the client. Streaming, tool calls, conversation
continuations, vision, image generation, and optional Web Search are handled by
the same routing layer.

## 软件作用

LAN BRIDGE 用一台能够访问上游模型服务的电脑，在可信局域网内建立统一的 OpenAI 兼容
桥接入口。Codex CLI、Codex Desktop、VS Code 插件以及其他 OpenAI 兼容客户端只需连接
这台电脑的局域网地址，并使用统一的模型别名；桥接器会选择对应的上游提供商，按需转换
Responses 与 Chat Completions 协议、工具调用和流式事件，使用保存在桥接主机上的提供商
凭据转发请求，再把规范化后的结果流式返回客户端。

这样，多台可信设备可以共用一个局域网入口、一套模型路由和上游配置，不需要在每台客户
端分别保存全部提供商密钥。它同时处理连续会话、工具调用、视觉输入、图片生成及可选的
Web Search；对于支持原生 Responses API 的上游可直接转发，对于只支持 Chat
Completions 的上游则执行双向协议转换。

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
- Optional native Codex routing, host-side Codex login injection for the same user on a trusted LAN, Web Search integration, and model context-window controls.
- Managed bridge access keys with per-key model allowlists and lightweight token-usage accounting.
- Electron desktop manager with dashboard, provider/model configuration, logs, themes, language settings, tray operation, and launch-at-login support.
- Credentials are read from local YAML or environment variables and are removed from configuration exports.

## Security model

This repository contains no production API keys or personal runtime
configuration. Do not commit `.lan-bridge.yaml`, `.lan-bridge.env`, `.env`,
logs, captures, or exported credentials.

The server defaults to `127.0.0.1`, which is reachable only from the bridge
host. LAN access requires explicitly binding to `0.0.0.0` or to a LAN interface
address. The management API remains loopback-only. When `access_control.enabled`
is true, every `/v1/*` HTTP or WebSocket request must carry a bridge-issued
bearer key. A key may allow every model (`*`, including internal Codex helper
models such as auto-review) or use an explicit model allowlist. Key
verifiers are stored as hashes in a separate local access-key store (not in
exportable YAML); the raw value is displayed only once at creation or rotation.
Keep the bridge on a trusted LAN or VPN, restrict the port with the host
firewall, and never publish it directly to the Internet. Plain HTTP protects
neither prompts nor keys from a hostile LAN; use an isolated network, SSH
tunnel, or TLS where appropriate.

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

### Enable trusted LAN access / 开启可信局域网访问

The default `127.0.0.1` binding is local-only. To let other trusted devices on
the same LAN use the bridge, set the listening host in `.lan-bridge.yaml`:

默认的 `127.0.0.1` 只能由桥接主机本机访问。要让同一局域网中的其他可信设备使用，
请在 `.lan-bridge.yaml` 中修改监听地址：

```yaml
server:
  host: 0.0.0.0
  port: 8765
```

Restart LAN BRIDGE after changing the listening address. On another device,
replace `<bridge-ip>` with the bridge computer's LAN IP, for example:

修改监听地址后需要重启 LAN BRIDGE。其他设备应把 `<bridge-ip>` 替换为桥接主机的
局域网 IP，例如：

```text
Base URL: http://192.168.1.20:8765/v1
```

Allow TCP port `8765` only from the trusted LAN or selected client addresses in
the host firewall. A narrower alternative is to bind directly to the bridge
computer's LAN IP instead of `0.0.0.0`.

请在主机防火墙中只允许可信局域网或指定客户端访问 TCP `8765` 端口。相比
`0.0.0.0`，也可以直接绑定桥接主机的局域网 IP，以缩小监听范围。

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

Clients do not copy the bridge host's OpenAI login state or provider API keys.
They send one LAN BRIDGE access key, while the bridge chooses the upstream route
from the requested model name and keeps provider credentials on the bridge host.
Use these values after LAN BRIDGE is running:

```text
Base URL: http://127.0.0.1:8765/v1
API key: a LAN BRIDGE access key created in the desktop Access Keys page
Model: any model for an unrestricted key, or an explicitly allowed alias
```

For a client on another trusted LAN device, use
`http://<bridge-ip>:8765/v1` instead. Access control defaults to fail closed:
until the loopback-only desktop manager creates the first key, `/v1/*` returns
a configuration error rather than accepting unauthenticated traffic.

The desktop Settings page can update the local YAML, import/export redacted configuration, and switch Codex between LAN BRIDGE and official OpenAI routing.

### Official Codex custom provider

Codex can use the bridge as a custom Responses provider. The following is a
minimal example; keep the access key in the environment rather than in a
committed config file:

```toml
model_provider = "lan_bridge"

[model_providers.lan_bridge]
name = "LAN BRIDGE"
base_url = "http://192.168.1.20:8765/v1"
wire_api = "responses"
env_key = "LAN_BRIDGE_API_KEY"
requires_openai_auth = false
```

Set `LAN_BRIDGE_API_KEY` on the client to the key issued by LAN BRIDGE. Select
the desired route by model name. For example, the configured native aliases
`gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna` use the bridge host's native
Codex route; configured DeepSeek, Qwen, and Grok aliases continue to use their
existing provider adapters. The bridge does not forward a client OpenAI login
state.

### Host Codex login injection (same-user trusted LAN)

This optional mode lets a second computer call official Codex Responses models
without copying the bridge computer's OpenAI login cache. It is intended only
for one person's trusted devices. It is not an account-sharing, multi-user, or
public gateway feature, and it does not turn a ChatGPT subscription into an API
key.

On the bridge computer:

1. Sign in to Codex with ChatGPT and use file-based credential storage
   (`cli_auth_credentials_store = "file"`). LAN BRIDGE auto-detects
   `%CODEX_HOME%\auth.json` or `%USERPROFILE%\.codex\auth.json`.
2. Create an access key in **Access Keys**. Keep the default unrestricted mode
   when Codex helper models such as auto-review must pass through, or select an
   explicit model allowlist for a restricted client. Then enable **Host Codex
   Login Injection**.
3. Bind only to the required LAN interface and firewall the port to the selected
   client computer.

On the client computer, keep the OpenAI login out of the configuration and use
the bridge access key as the custom provider key:

```toml
model_provider = "lan_bridge"

[model_providers.lan_bridge]
name = "LAN BRIDGE"
base_url = "http://192.168.1.20:8765/v1"
env_key = "LAN_BRIDGE_API_KEY"
wire_api = "responses"
requires_openai_auth = false
```

Set `LAN_BRIDGE_API_KEY` locally to the bridge-only key. LAN BRIDGE validates it,
removes it before forwarding, then loads `access_token` and `account_id` from
the bridge host's Codex cache. Client-supplied OpenAI auth and account headers
are never used as the bridge credential. LAN BRIDGE does not implement its own
OAuth refresh endpoint; Codex refreshes its cache during normal use. If the
cached access token expires, use Codex on the bridge computer or sign in again.

### Access keys and usage

The desktop **Access Keys** page creates, disables, rotates, and deletes bridge
keys, and assigns each key an explicit list of model aliases (or all configured
models). The raw key is shown exactly once after creation or rotation, so copy
it to the client environment immediately. The bridge records a small local
summary of request count and input/output/total tokens per key; it does not
store prompts or upstream login tokens in that summary.

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

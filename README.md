# LAN BRIDGE

![LAN BRIDGE](desktop/assets/icon.png)

把一台电脑上的模型服务接入，提供给自己的可信局域网设备使用。
支持 Codex 原生 Responses、自定义 OpenAI 兼容上游、模型路由、流式输出、访问密匙与用量统计。

**日常使用统一安装 Windows 桌面版。桌面程序自带 Python 后端；不需要再安装一个“命令行版”。**

## 安装、设置、启动

1. 打开 [最新发布下载页](https://github.com/Orua/lan-bridge/releases/latest)。
2. Windows 10/11 x64 下载 **`LAN BRIDGE Setup <版本>.exe`**，完成安装。
3. 从桌面或开始菜单打开 **LAN BRIDGE**。它会显示独立窗口并自动启动后端。
4. 要使用自己的 ChatGPT/Codex 登录，在桥接电脑完成 Codex 文件登录，再到桌面 **设置 → 主机 Codex 登录注入** 启用并保存。
5. 在 **访问密匙** 创建一个 Bridge 密匙；客户端使用这个密匙和 `http://127.0.0.1:8765/v1`。
6. 其他设备访问时，在设置把监听地址改为局域网接口或 `0.0.0.0`，保存并重启代理，再把客户端地址改为 `http://<桥接电脑IP>:8765/v1`。

**首次配置请按 [Windows 安装、登录与使用指南](docs/WINDOWS.md) 操作。**
指南包含 Codex 登录命令、API 上游设置、客户端配置、托盘操作和故障处理。

GitHub 会把上传文件名中的空格转换为点号；下载页请认准 **`LAN.BRIDGE.Setup.<版本>.exe`**。

| 看到的文件/命令 | 用途 |
| --- | --- |
| `LAN BRIDGE Setup <版本>.exe` | 推荐安装包，包含完整桌面与后端 |
| `LAN BRIDGE <版本>.exe` | 同一个桌面程序的便携包，二选一即可 |
| `resources/backend/lan-bridge.exe` | 桌面内部组件，不要单独运行或分发 |
| `pip install -e .` / `lan-bridge start` | 后端开发入口，没有桌面 UI |
| `npm run dev` | Electron 桌面开发，要求先准备 Python 环境 |
| `npm run ui:dev` / 端口 `5173` | 仅前端调试服务，不能作为已安装的桌面版交付 |

Chrome 里打开一个开发网页不等于安装成功。ChatGPT 网页 Cookie、Codex 主机登录缓存和 Bridge 密匙是三种不同状态；LAN BRIDGE 不靠管理页面的浏览器登录工作。

## 从 Git 仓库安装

普通用户直接用上面的安装包。已经拉取源码、需要自行构建时，在仓库根目录执行：

```powershell
# 先安装 Python 3.10+ 和 Node.js 22.12+（含 npm），然后打开新的 PowerShell。
powershell -ExecutionPolicy Bypass -File scripts/windows.ps1 -Action Build
powershell -ExecutionPolicy Bypass -File scripts/windows.ps1 -Action Start
```

`Build` 自动创建本项目的 `.venv`、安装依赖、执行测试、打包后端与 Electron，并检查版本、CSP 和 SHA-256。
安装包在 **`agent/output/windows/<版本>/`**。`Start` 打开当前版本的完整桌面程序；首次构建需要联网下载依赖，正式 Build 要求产品源码已全部提交。
完整开发命令与发布约束见 [开发与发布指南](docs/DEVELOPMENT.md)。

## 安全与数据位置

- 默认只监听本机 `127.0.0.1:8765`；管理 API 只允许本机访问。
- `/v1/*` 默认需要有效 Bridge 密匙。不要用 `any-value` 或上游 ChatGPT 登录令牌替代。
- 上游凭据和 Codex 登录留在桥接主机，客户端只保存 Bridge 密匙。
- 配置通常是 `%USERPROFILE%\.lan-bridge.yaml`；以桌面设置页显示的实际路径为准。
- 只在自己的可信 LAN/VPN 使用；防火墙只放行必要设备，不要直接暴露到公网。
- 不要提交真实 YAML、`.env`、`auth.json`、访问密匙或日志。导出的脱敏配置不包含可迁移的登录凭据。

## 版本与来源

可选语音转写网关的依赖和接口见 [本机语音转写](docs/LOCAL_ASR.md)。识别引擎与模型需独立安装，不包含在桌面包中。

`VERSION` 是唯一发布版本来源。桌面、后端、CLI 与“关于”页必须保持一致。
目前维护 Windows x64 桌面发布，不提供独立网页产品或独立 CLI 安装包。
Python 后端属于同一产品的必要组件，保留其代码和开发命令。

LAN BRIDGE is an independently maintained derivative of
[git-liu835/codex-cn-bridge](https://github.com/git-liu835/codex-cn-bridge),
formerly named `code CN Bridge`. It is not an official upstream release and is
not maintained or supported by the upstream author. This repository was created
from a substantially modified working tree with a new, squashed Git history;
the exact original base commit was not preserved. Changes include LAN BRIDGE
branding, native/custom Responses routing, protocol and streaming conversion,
host-side Codex credential injection, access keys, and Windows desktop supervision.
See [NOTICE.md](NOTICE.md) for attribution and provenance. MIT: [LICENSE](LICENSE).

# Windows 安装、登录与使用指南

## 1. 选择安装文件

支持 Windows 10/11 x64。
打开 [Releases 最新版](https://github.com/Orua/lan-bridge/releases/latest)，下载 `LAN BRIDGE Setup <版本>.exe` 并安装。
开始菜单和桌面快捷方式都叫 **LAN BRIDGE**。
GitHub 会把文件名里的空格改成点号，因此下载页实际显示为 `LAN.BRIDGE.Setup.<版本>.exe`；便携包为 `LAN.BRIDGE.<版本>.exe`。
SHA256SUMS 中保留本机构建的空格形式文件名，校验时用 `Get-FileHash -Algorithm SHA256 <下载文件路径>` 对比对应哈希即可，重命名不影响哈希。
只运行桌面版不需要 Python 或 Node.js。只有需要进行 Codex 登录时，才另外安装官方 Codex CLI。

`LAN BRIDGE <版本>.exe` 是同一个桌面版的便携包，和 Setup 二选一；它会使用临时解压目录，日常固定使用建议选 Setup。
GitHub 的 `Source code (zip)` 是源码，不能当成安装包。
不要从旧的 CODE-CN-BRIDGE 项目或上游下载文件覆盖本项目。

## 2. 启动和关闭

打开 **LAN BRIDGE**，应出现有“仪表盘、模型、访问密匙、设置、日志、关于”等页面的独立窗口。
默认自动启动后端，界面应显示运行中；第一次启动尚未配置上游，也可以显示运行中。
“运行中”只表示桥接服务启动，实际模型权限需要发送请求确认。

关闭窗口通常缩到系统托盘；双击托盘图标恢复。需要完全结束时从托盘选择退出。
停止代理只停止服务；设置中可以控制自动启动、开机启动和关闭到托盘。
不要同时手动执行 `lan-bridge start` 再开桌面，否则桌面可能接上另一个仍占用 8765 的后端。
桌面管理固定使用 8765，日常安装保留该端口。

在“关于”和仪表盘确认桌面与后端版本相同。打开 Chrome 或显示 `localhost:5173` 是开发服务，不算桌面安装成功。

## 3. 使用自己的 ChatGPT/Codex 登录

在**运行 LAN BRIDGE 的 Windows 电脑、同一个 Windows 用户**下完成以下操作。
LAN BRIDGE 不包含独立 ChatGPT 登录页面，只读取这台电脑上的 Codex 文件缓存。
ChatGPT 网页的浏览器 Cookie 不能代替它；WSL、另一个 Windows 用户或另一台电脑的登录也不会自动出现在当前用户目录。

安装官方 Codex CLI（[官方入口](https://developers.openai.com/codex/cli/)），然后运行：

```powershell
codex -c 'cli_auth_credentials_store="file"' login
codex -c 'cli_auth_credentials_store="file"' login status
```

第一条命令会打开官方登录网页，完成后回到终端确认成功。浏览器用于官方登录回调，这是正常的；它不是 LAN BRIDGE 的主界面。
这里要求 `file` 存储，因为 LAN BRIDGE 目前不能读取操作系统 keyring。
建议在 Codex `config.toml` 的顶层保留 `cli_auth_credentials_store = "file"`，让后续 Codex 使用和刷新继续写入同一个文件；编辑前备份已有配置，不要覆盖整个文件。

默认缓存路径是 `%USERPROFILE%\.codex\auth.json`；设置过 `CODEX_HOME` 时是该目录下的 `auth.json`。
不要打印或上传这个文件，也不要复制到仓库。若命令提示设备登录更合适，可按官方文档使用 `login --device-auth`，是否可用取决于账号设置。

回到 LAN BRIDGE：

1. 打开 **设置 → 主机 Codex 登录注入**。
2. 勾选 **启用服务器端登录注入**。
3. `auth.json` 路径通常留空自动检测；自定义 `CODEX_HOME`、快捷方式未继承终端环境或缓存放在别处时，填入实际绝对路径。
4. 保存设置，确认显示“已找到登录缓存”。这只证明文件存在，不证明令牌有效。
5. 打开 **访问密匙** 创建一个 Bridge 密匙，立即保存在自己的客户端；原文只展示一次。
6. 在模型页面查看账号目录，并由客户端发送一次短请求确认实际可用。

LAN BRIDGE 只读登录缓存，不自行完成 OAuth 刷新。令牌过期时，在同一 Windows 用户下正常使用 Codex 让其刷新，或重新执行上面的文件登录，再重试。
不要为了“修 UI 登录”执行 `codex logout` 或删除缓存；Codex CLI 与扩展共享缓存时，退出会影响其他客户端。
登录缓存、自动刷新和文件存储的官方说明见 [OpenAI Authentication](https://learn.chatgpt.com/docs/auth)。

## 4. 使用其他 API 上游

如果只使用 DeepSeek、Qwen 或自己的 OpenAI 兼容服务，可以跳过 Codex 登录。
在“模型”中配置提供商/自定义槽位的 Base URL、上游 API Key、模型名称与别名，保存并测试连接。
上游 API Key 保存在桥接主机；客户端仍使用“访问密匙”创建的 Bridge 密匙。
不要把 Bridge 密匙当作上游 API Key。导入脱敏配置后需要重新填写上游凭据。

## 5. 连接客户端

同一台电脑：

```text
Base URL: http://127.0.0.1:8765/v1
API key: 在 LAN BRIDGE“访问密匙”页创建的密匙
Model: 账号可用的原生模型 ID，或你配置的自定义模型别名
```

其他自己的可信局域网设备：

1. 桥接主机“设置”把监听地址改为该电脑的局域网 IP 或 `0.0.0.0`，保留端口 8765，保存后停止并启动代理。
2. Windows 防火墙只允许可信客户端访问 TCP 8765；不要开放公网。
3. 客户端使用 `http://<桥接电脑的局域网IP>:8765/v1`，不能填客户端自己的 `127.0.0.1`，也不能填 `0.0.0.0`。

Codex 自定义提供商示例（合并到客户端自己的配置，保留已有其他设置）：

```toml
model_provider = "lan_bridge"
# model = "这里填实际可用的模型 ID 或别名"

[model_providers.lan_bridge]
name = "LAN BRIDGE"
base_url = "http://192.168.1.20:8765/v1"
wire_api = "responses"
env_key = "LAN_BRIDGE_API_KEY"
requires_openai_auth = false
```

把示例 IP 改成真实主机地址。在客户端设置 `LAN_BRIDGE_API_KEY` 为 Bridge 密匙，并重新打开客户端使环境变量生效。
普通 Chat 客户端应选择 OpenAI-compatible / Chat Completions，使用配置为支持该协议的模型别名。

仪表盘的“切换到统一/自定义路由”会修改桥接电脑上 Codex 的路由配置；启动桌面本身不需要点击它。
只希望为另一台设备提供服务时，先保持主机 Codex 官方登录配置。
“恢复官方直连”用于让主机 Codex 恢复官方路由，不负责创建登录缓存。

## 6. 按现象排查

| 现象 | 处理 |
| --- | --- |
| 只有终端、没有窗口 | 启动了内部后端；关闭自己启动的后端，打开 LAN BRIDGE 快捷方式 |
| Chrome 显示 5173 测试页 | 启动了前端调试服务；安装 Setup，或按开发指南 Build 后 Start |
| CLI 登录成功但桌面找不到缓存 | 核对同一 Windows 用户、`file` 存储、`CODEX_HOME` 与设置页实际路径 |
| 文件已找到但请求 401/令牌过期 | 让主机 Codex 刷新或重新文件登录；不要只刷新浏览器页面 |
| 请求提示尚未创建密匙/密匙无效 | 在“访问密匙”创建有效密匙并更新客户端，不要用 `any-value` |
| 本机可用，其他电脑不通 | 核对监听地址、主机局域网 IP、可信网络防火墙规则；管理页面只允许本机访问 |
| 启动失败/8765 占用 | 检查是否有另一个自己启动的后端或旧桌面实例；不要盲目结束不明进程 |
| 模型在列表里但不能调用 | 用一次实际请求确认权限；目录展示不代表账号一定有权限 |
| 修改配置后重开失效 | 以“设置”页 Config 路径为准，检查是否同时有项目 `config.yaml` 与用户配置 |

排查时提供版本、现象与脱敏错误即可，不要提供 `auth.json` 或完整凭据。

## 7. 升级和迁移

退出当前桌面后运行新 Setup。用户配置和 Codex 缓存位于用户目录，不应通过替换安装目录来迁移。
换电脑时重新登录 Codex、重新配置上游或导入脱敏配置，并重新创建 Bridge 密匙。
保留原有 VPN/代理设置；上游网络代理与 Bridge 的局域网监听地址是两种设置。

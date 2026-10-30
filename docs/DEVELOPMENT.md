# 开发与发布

## 唯一维护的产品

Windows x64 Electron 桌面 + 内置 Python 后端是同一个 LAN BRIDGE 产品。
源码中的 Python CLI 是桌面的后端入口和开发工具，不是另一套需要单独安装的桌面版本。
不再维护 macOS/Linux 打包目标，也不提供独立浏览器版。
旧的直接修改 `app.asar` 的两个补丁脚本已移除，后续一律修改源代码并重新构建。

## 从干净仓库构建

准备 Windows 10/11 x64、Python 3.10+（CI 使用 3.12）、Node.js 22.12+ 和 npm。
旧的 Node 18/20 不满足当前 Vite/Electron 工具链要求。Git 拉取本仓库后，在根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/windows.ps1 -Action Build
powershell -ExecutionPolicy Bypass -File scripts/windows.ps1 -Action Start
```

Build 创建 `.venv`，安装 Python 与 npm 依赖，检查版本，执行 Python 测试及桌面类型/生命周期测试，
构建 `dist-backend/lan-bridge.exe`，再构建 Electron Windows x64 安装包与便携包。
最终验证实际打包的桌面/后端/UI 版本、内部后端名称、CSP 和 SHA-256。
任何命令返回非零退出码立即停止，不能用目录里残留的旧 exe 当作构建成功。

输出均相对仓库目录，不依赖开发者用户名或仓库存放层级：

```text
agent/output/windows/<VERSION>/
  LAN BRIDGE Setup <VERSION>.exe    # 推荐对外安装包
  LAN BRIDGE <VERSION>.exe          # 同一个桌面的便携形式
  SHA256SUMS.txt                    # 安装包、便携包及 unpacked 核心文件哈希
  RELEASE.json                     # 版本、准确源提交号、构建时间与平台
  win-unpacked/
    LAN BRIDGE.exe
    resources/backend/lan-bridge.exe
    resources/app.asar
```

不提交二进制、node_modules、构建日志、备份或临时脚本。
普通用户下载 Releases 安装包；维护者才需要这里的工具链。

## 开发调试

完整开发桌面（脚本把仓库虚拟环境放入 PATH，后端随 Electron 启动）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/windows.ps1 -Action Dev
```

准备好环境后，也可以在 `desktop` 中执行 `npm run dev` / `npm run electron:dev`，二者都启动 Electron。
只运行 `npm run ui:dev` 只启动 Vite 前端，没有 Electron 预加载、托盘和后端监督能力。
`npm start` 仅供已编译的开发目录运行 Electron，普通使用仍走 Build/Start 或 Setup。
不要把浏览器预览当成正式交付结果。

单独调试后端可使用 `.venv/Scripts/python.exe -m code_cn_bridge.cli start -c <本地配置>`。
它不会打开桌面 UI，默认没有可用 Bridge 密匙时也不接受匿名推理请求。
不要同时运行独立后端与另一套正式桌面后端。

## 版本、测试与发布

版本只能通过脚本修改：

```powershell
python scripts/version.py --set X.Y.Z
python scripts/version.py --check
```

修改已有文件前按 AGENTS.md 备份。只提交本任务改动，不能夹带其他未提交功能。
从待发布提交的干净工作树运行完整 Build 后才能发布。脚本拒绝未提交或未跟踪的产品文件（agent 内记录除外），并在构建结束再检查一次。
检查全部待交付功能与新模块、依赖和测试，不能静默排除本机已实现功能后把旧子集当作“最新完整版”。
输出按版本隔离，RELEASE.json 记录准确源提交号；推送代码、发布下载包、部署本机运行目录分别记录结果。
发布包必须来自同一个提交，远端 tag 必须指向该提交，远端文件哈希必须与本地验证值相符。更换源码必须升级版本，不覆盖旧版本二进制。
完整测试通过也不能代替目标用户电脑上的登录与实际调用验收。

GitHub Actions **Windows desktop release** 支持两种方式：

- 手动 Run workflow：构建、测试并提供下载 artifact，不创建公开 Release。
- 推送与 VERSION 相同的 `vX.Y.Z` tag：同样构建验证后发布 Setup、便携包、SHA256SUMS 与 RELEASE.json。

CI 使用 Python 3.12、Node 22；发布流程不会部署或停止维护者本机服务。
本机生成的校验文件也可用来复核发布文件：`Get-FileHash -Algorithm SHA256 <安装包路径>`。

只有维护者明确需要部署到固定运行目录时，才按 AGENTS.md 先备份到 G: 再部署。
外部用户安装路径由 Setup 决定，不要把维护者的 D: 运行目录写成所有电脑的前提。

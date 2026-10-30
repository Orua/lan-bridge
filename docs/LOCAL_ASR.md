# 可选本机语音转写

LAN BRIDGE 包含语音网关，不包含识别引擎、模型权重或 PyTorch。默认关闭，不影响正常模型桥接。
已有独立、本机运行的兼容 ASR worker 时，才能启用；仅安装桌面包不会自动获得语音识别能力。
识别服务的模型下载、热词和升级由该独立安装管理。

在设置页显示的实际 YAML 配置中添加（修改前备份）：

```yaml
local_asr:
  enabled: true
  worker_url: http://127.0.0.1:8791
  auto_start: false
  worker_home: ""
  cpu_threads: 8
```

先自行启动 worker，再重启桥接器。worker 必须绑定本机 127.0.0.1；不接受远程 worker URL。
`auto_start: true` 只适用于兼容的独立安装目录，需有 `.venv/Scripts/python.exe` 与 `asr_demo/app.py`，
可通过 `python -m uvicorn asr_demo.app:app` 启动。`worker_home` 填该安装的真实绝对路径，不要照搬维护者电脑路径。
桥接器退出只关闭它自己启动的 worker，不关闭用户事先运行的识别服务。

客户端使用正常 Bridge 密匙，公开模型别名为 `paraformer-zh`：

| Bridge 接口 | 用途与 worker 约定 |
| --- | --- |
| `GET /v1/audio/status` | 查询 worker `GET /health`；返回 JSON 对象，包含 status、recognizer、hotword_count 等状态 |
| `POST /v1/audio/transcriptions` | multipart 音频上传，model=paraformer-zh；转发到 worker 同名接口，支持 JSON/text 输出；请求上限约 24 MiB |
| `WS /v1/audio/stream` | 16kHz 单声道 PCM16 小端裸音频；单帧最多 32,000 字节，总音频最多 90 秒 |

WebSocket 在握手的 `Authorization: Bearer <Bridge密匙>` 中鉴权。
先收 `ready`，发送 PCM 二进制帧，收 `partial` 临时文字；结束录音后发送 `{"type":"stop"}`，
等待 `correcting` 与 `final`，用 final 整段替换临时文字。可发送 `{"type":"cancel"}` 取消。
服务只在最终文字成功交付后记录成功，区分取消、录音闲置、输入错误、worker 故障与最终结果超时。
最终校正独立限时 120 秒，不套用录音阶段的 25 秒闲置限制。

网关不把 Bridge 密匙转发给 worker，也不把音频或转写文字保存到请求日志。
日志只记录请求标识、音频数量、识别器和耗时等诊断信息。
`/admin/api/audio/demo-session` 仅供本机测试 BFF 获取内存里的音频专用短期会话；该会话不能调用普通模型接口，桥接器退出即失效。
浏览器无法直接设置 WebSocket Authorization 时应使用这样的本机 BFF，不要把个人访问密匙写入前端文件。

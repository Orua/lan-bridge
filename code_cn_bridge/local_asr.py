"""Authenticated local ASR gateway. Heavy inference stays in an optional worker."""
from __future__ import annotations

import asyncio
import os
import subprocess
import hmac
import secrets
import time
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from .access_control import BridgePrincipal, BridgeAccessError, authenticate_bridge_headers
from .middleware import bridge_principal_context
from .asr_stream import AudioStreamError, relay_audio

MODEL = "paraformer-zh"
MAX_REQUEST_BYTES = 24 * 1024 * 1024 + 64 * 1024


def failure(message, status, code):
    return JSONResponse({"error": {"message": message, "type": "local_asr_error", "code": code}}, status)


class LocalASRService:
    def __init__(self, config_getter):
        self.config_getter = config_getter
        self.process = None
        self.demo_key = ""
        self.demo_key_id = ""
        self.start_lock = asyncio.Lock()
        self.worker_state = {}

    def authenticate_demo(self, headers, path):
        if path not in {"/v1/audio/status", "/v1/audio/transcriptions", "/v1/audio/stream"} or not self.demo_key:
            return None
        scheme, _, token = str(headers.get("authorization", "")).partition(" ")
        if scheme.lower() == "bearer" and hmac.compare_digest(token.strip(), self.demo_key):
            return BridgePrincipal(self.demo_key_id, "本机转写测试会话", "asr_demo", (MODEL,))
        return None

    def settings(self):
        value = self.config_getter().data.get("local_asr", {})
        return value if isinstance(value, dict) else {}

    def worker_url(self):
        url = str(self.settings().get("worker_url", "http://127.0.0.1:8791")).rstrip("/")
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
                or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise ValueError("local_asr.worker_url 必须是 http://127.0.0.1:端口")
        return url

    async def health(self):
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            response = await client.get(self.worker_url() + "/health")
            response.raise_for_status()
            state = response.json()
            if not isinstance(state, dict):
                raise ValueError("Invalid ASR worker health response")
            self.worker_state = state
            return self.worker_state

    async def ensure_started(self):
        if not self.settings().get("enabled"):
            return
        async with self.start_lock:
            try:
                await self.health()
                return
            except httpx.HTTPError:
                pass
            if not self.settings().get("auto_start") or (self.process and self.process.poll() is None):
                return
            home = Path(str(self.settings().get("worker_home") or "")).resolve()
            python = home / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            if not python.is_file() or not (home / "asr_demo/app.py").is_file():
                raise ValueError("本机 ASR 运行目录缺少 Python 环境或 asr_demo/app.py")
            port = urlsplit(self.worker_url()).port
            env = dict(os.environ, PYTHONUTF8="1", HF_HUB_OFFLINE="1", ASR_HOME=str(home))
            env["ASR_CPU_THREADS"] = str(self.settings().get("cpu_threads", 8))
            temp = home / "agent/temp/uploads"
            temp.mkdir(parents=True, exist_ok=True)
            env["TMP"] = env["TEMP"] = str(temp)
            self.process = subprocess.Popen(
                [str(python), "-m", "uvicorn", "asr_demo.app:app", "--host", "127.0.0.1", "--port", str(port)],
                cwd=home, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

    async def close(self):
        if self.demo_key_id:
            self.demo_key_id = self.demo_key = ""
        if self.process and self.process.poll() is None:
            if os.name == "nt":
                # The Windows venv launcher spawns a child interpreter.
                await asyncio.to_thread(subprocess.run,
                                        ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                self.process.terminate()
            try:
                await asyncio.to_thread(self.process.wait, timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait, timeout=5)


def create_asr_router(service, require_local_admin, record_request, audit_event=None):
    router = APIRouter()

    def access(request):
        if not service.settings().get("enabled"):
            return failure("桥接器尚未启用本机语音转写。", 503, "asr_disabled")
        principal = getattr(request.state, "bridge_principal", None)
        if principal is None or not principal.can_use_model(MODEL):
            return failure("当前访问密钥没有中文转写模型权限。", 403, "model_permission_denied")

    @router.post("/admin/api/audio/demo-session")
    async def demo_session(request: Request):
        require_local_admin(request)
        if not service.settings().get("enabled"):
            return failure("桥接器尚未启用本机语音转写。", 503, "asr_disabled")
        # This in-memory token is accepted only by ASR endpoints.
        if not service.demo_key:
            service.demo_key = "asr_demo_" + secrets.token_urlsafe(32)
            service.demo_key_id = "asr-demo-" + secrets.token_hex(8)
        return JSONResponse({"key": service.demo_key}, headers={"Cache-Control": "no-store"})

    @router.get("/v1/audio/status")
    async def audio_status(request: Request):
        denied = access(request)
        if denied is not None:
            return denied
        try:
            await service.ensure_started()
            state = await service.health()
            return {"status": state.get("status", "error"), "model": MODEL,
                    "device": "cpu", "offline": True, "route": "lan_bridge_local_asr",
                    "error": "本机识别模型加载失败，请检查 ASR 服务。" if state.get("error") else "",
                    "streaming_status": state.get("streaming_status", "unavailable"),
                    "recognizer": str(state.get("recognizer", ""))[:100],
                    "hotword_count": state.get("hotword_count", 0)}
        except (httpx.HTTPError, ValueError):
            return failure("本机识别服务正在启动或尚未连接，请稍后重试。", 503, "asr_worker_unavailable")

    @router.post("/v1/audio/transcriptions")
    async def transcription(request: Request):
        denied = access(request)
        if denied is not None:
            return denied
        started, status, error_message = time.time(), 502, ""
        details = {"request_id": "asr-" + secrets.token_hex(12), "outcome": "failed", "error_code": ""}
        target_model = MODEL
        def emit(event):
            if audit_event:
                principal = request.state.bridge_principal
                audit_event(event, details["request_id"], model=MODEL, endpoint="audio/transcriptions",
                            client_ip=request.client.host if request.client else "unknown",
                            access_key_id=principal.key_id, **{k:v for k,v in details.items() if k != "request_id"})
        def reject(message, code_status, code):
            nonlocal status, error_message
            status, error_message = code_status, message
            details["error_code"] = code
            return failure(message, status, code)
        emit("asr.started")
        try:
            content_type = request.headers.get("content-type", "")
            if "multipart/form-data" not in content_type.lower():
                return reject("语音转写需要 multipart/form-data 音频文件。", 415, "invalid_content_type")
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                    return reject("音频请求超过 24 MB 限制。", 413, "audio_too_large")
                body.extend(chunk)
            envelope = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body)
            if not envelope.is_multipart():
                return reject("无效的 multipart 音频请求。", 400, "invalid_multipart")
            models = [(part.get_payload(decode=True) or b"").decode("utf-8", errors="replace").strip()
                      for part in envelope.iter_parts()
                      if part.get_param("name", header="content-disposition") == "model"]
            if models and (len(models) != 1 or models[0] != MODEL):
                return reject("当前本机转写仅支持 paraformer-zh。", 400, "unsupported_model")
            formats = [(part.get_payload(decode=True) or b"").decode("utf-8", errors="replace").strip()
                       for part in envelope.iter_parts()
                       if part.get_param("name", header="content-disposition") == "response_format"]
            await service.ensure_started()
            target_model = str(service.worker_state.get("recognizer") or MODEL)[:100]
            async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(120, connect=5)) as client:
                upstream = await client.post(service.worker_url() + "/v1/audio/transcriptions",
                                             content=bytes(body), headers={"Content-Type": content_type})
            status = upstream.status_code
            if status == 200 and formats == ["text"] and upstream.headers.get("content-type", "").startswith("text/plain"):
                details["outcome"] = "completed"
            elif status == 200:
                try:
                    payload = upstream.json()
                    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
                        raise ValueError("missing text")
                    if isinstance(payload.get("recognizer"), str):
                        target_model = payload["recognizer"][:100]
                    for field in ("processing_ms", "hotword_count", "duration"):
                        if isinstance(payload.get(field), (int, float)):
                            details[field] = payload[field]
                except ValueError:
                    return reject("识别服务返回了无效结果。", 502, "asr_invalid_response")
                details["outcome"] = "completed"
            else:
                # Classify worker failures without writing response text/audio to audit logs.
                details["error_code"] = "asr_worker_rejected"
                error_message = "本机识别服务拒绝音频，请检查格式或稍后重试。"
            return Response(content=upstream.content, status_code=status,
                            headers={"Content-Type": upstream.headers.get("content-type", "application/json"),
                                     "X-LAN-Bridge-ASR": "local", "Cache-Control": "no-store",
                                     "X-LAN-Bridge-Request-ID": details["request_id"]})
        except httpx.TimeoutException:
            return reject("本机语音识别超时，请缩短录音后重试。", 504, "asr_timeout")
        except (httpx.HTTPError, ValueError):
            return reject("本机识别服务未连接，请稍后重试。", 503, "asr_worker_unavailable")
        except asyncio.CancelledError:
            status = 204
            details.update(outcome="cancelled", error_code="client_cancelled")
            raise
        finally:
            details.update(status_code=status, recognizer=target_model,
                           elapsed_ms=round((time.time() - started) * 1000, 1))
            emit("asr." + details["outcome"] if details["outcome"] in {"completed", "cancelled"} else "asr.failed")
            record_request(started, MODEL, "audio/transcriptions", status, False, error_message,
                           provider="local_asr", target_model=target_model,
                           upstream_api="audio/transcriptions", details=dict(details))

    @router.websocket("/v1/audio/stream")
    async def audio_stream(socket: WebSocket):
        from websockets.legacy.client import connect
        from websockets.exceptions import ConnectionClosed
        try:
            principal = service.authenticate_demo(socket.headers, socket.url.path)
            if principal is None:
                principal = authenticate_bridge_headers(socket.headers, service.config_getter())
            if not service.settings().get("enabled") or not principal.can_use_model(MODEL):
                await socket.close(code=1008)
                return
        except BridgeAccessError:
            await socket.close(code=1008)
            return
        await socket.accept()
        started = time.time()
        details = {"request_id": "asr-" + secrets.token_hex(12), "audio_bytes": 0,
                   "audio_frames": 0, "first_text_ms": None}
        client_ip = socket.client.host if socket.client else "unknown"
        result = {"status": 502, "outcome": "failed", "error_code": "asr_internal_error"}
        error_message = ""

        def emit(event):
            if audit_event:
                audit_event(event, details["request_id"], model=MODEL,
                            endpoint="audio/stream", client_ip=client_ip,
                            access_key_id=principal.key_id, **{k: v for k, v in details.items() if k != "request_id"})

        emit("asr.started")
        try:
            await service.ensure_started()
            url = service.worker_url().replace("http://", "ws://", 1) + "/v1/audio/stream"
            async with connect(url, max_size=65536, max_queue=4, open_timeout=8) as upstream:
                details["connect_ms"] = round((time.time() - started) * 1000, 1)
                result = await relay_audio(socket, upstream, details, emit)
        except AudioStreamError as exc:
            result = {"status": exc.status, "outcome": "failed", "error_code": exc.code}
            error_message = str(exc)
        except (OSError, ValueError, asyncio.TimeoutError, ConnectionClosed):
            result = {"status": 503, "outcome": "failed", "error_code": "asr_worker_unavailable"}
            error_message = "本机识别服务未连接，请稍后重试。"
        except WebSocketDisconnect:
            result = {"status": 204, "outcome": "cancelled", "error_code": "client_disconnected"}
        except asyncio.CancelledError:
            result = {"status": 204, "outcome": "cancelled", "error_code": "server_shutdown"}
            raise
        except Exception as exc:
            details["exception_type"] = type(exc).__name__
            error_message = "语音转写内部错误，请重试。"
        finally:
            if error_message:
                try:
                    await socket.send_json({"type": "error", "code": result["error_code"],
                                            "message": error_message, "request_id": details["request_id"]})
                except (RuntimeError, WebSocketDisconnect):
                    pass
            details.update(outcome=result["outcome"], error_code=result["error_code"],
                           status_code=result["status"], audio_ms=round(details["audio_bytes"] / 32, 1),
                           elapsed_ms=round((time.time() - started) * 1000, 1))
            emit("asr." + ("completed" if result["outcome"] == "completed" else
                            "cancelled" if result["outcome"] == "cancelled" else "failed"))
            with bridge_principal_context(principal):
                record_request(started, MODEL, "audio/stream", result["status"], True,
                               error_message if result["outcome"] == "failed" else "",
                               provider="local_asr", target_model=details.get("recognizer", MODEL),
                               upstream_api="audio/stream", client_ip=client_ip,
                               first_response_ms=details.get("first_text_ms"), details=dict(details))
            try:
                await socket.close()
            except RuntimeError:
                pass

    return router

"""Bounded audio WebSocket forwarding with explicit terminal outcomes."""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

IDLE_TIMEOUT = 25
RECORD_TIMEOUT = 95
FINAL_TIMEOUT = 120


class AudioStreamError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


async def relay_audio(socket, upstream, details, emit):
    """Return only after final delivery, cancellation, or a classified failure."""
    stop = asyncio.Event()
    began = time.monotonic()
    outcome = {"status": 502, "outcome": "failed", "error_code": "asr_missing_final"}
    def elapsed_ms():
        return details.get("connect_ms", 0) + (time.monotonic() - began) * 1000

    async def upload():
        while True:
            if stop.is_set():
                frame = await socket.receive()  # The final-result watchdog owns this phase.
            else:
                try:
                    frame = await asyncio.wait_for(socket.receive(), IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    raise AudioStreamError(408, "asr_audio_idle", "录音数据中断，请重新录音。")
            if frame["type"] == "websocket.disconnect":
                outcome.update(status=204, outcome="cancelled", error_code="client_disconnected")
                return
            raw = frame.get("bytes")
            if raw is not None:
                if stop.is_set() or len(raw) % 2:
                    raise AudioStreamError(400, "invalid_audio_frame", "需要 16kHz 单声道 PCM16 音频，停止后不能继续发送。")
                if len(raw) > 32000 or details["audio_bytes"] + len(raw) > 90 * 32000:
                    raise AudioStreamError(413, "audio_too_large", "音频数据超过单帧限制或 90 秒上限。")
                details["audio_bytes"] += len(raw)
                details["audio_frames"] += 1
                await upstream.send(raw)
            else:
                raw = frame.get("text") or ""
                try:
                    command = json.loads(raw) if len(raw) <= 1024 else None
                except ValueError:
                    command = None
                if not isinstance(command, dict) or command.get("type") not in {"stop", "cancel"}:
                    raise AudioStreamError(400, "invalid_audio_command", "仅支持 stop 或 cancel 录音指令。")
                if command["type"] == "cancel":
                    await socket.send_json({"type": "cancelled"})
                    outcome.update(status=204, outcome="cancelled", error_code="client_cancelled")
                    return
                if not stop.is_set():
                    details["stop_ms"] = round(elapsed_ms(), 1)
                    stop.set()
                    emit("asr.stopped")
                    await upstream.send(json.dumps({"type": "stop"}))

    async def download():
        async for raw in upstream:
            try:
                item = json.loads(raw)
            except (ValueError, TypeError):
                raise AudioStreamError(502, "asr_invalid_response", "识别服务返回了无效数据。")
            if not isinstance(item, dict):
                raise AudioStreamError(502, "asr_invalid_response", "识别服务返回了无效数据。")
            kind = item.get("type")
            if kind == "error":
                code = str(item.get("code", "asr_worker_error"))
                errors = {
                    "model_not_ready": (503, "识别模型尚未就绪，请稍后重试。"),
                    "asr_busy": (503, "识别服务忙，请稍后重试。"),
                    "invalid_audio": (400, "音频或录音指令无效，请重新录音。"),
                    "audio_idle": (408, "录音数据中断，请重新录音。"),
                }
                status, message = errors.get(code, (502, "本机识别失败，请重新录音。"))
                raise AudioStreamError(status, code if code in errors else "asr_worker_error", message)
            if kind not in {"ready", "partial", "correcting", "final"}:
                raise AudioStreamError(502, "asr_invalid_response", "识别服务返回了未知消息。")
            if kind in {"partial", "correcting", "final"} and not isinstance(item.get("text"), str):
                raise AudioStreamError(502, "asr_invalid_response", "识别服务缺少文字字段。")
            if kind == "final" and not stop.is_set():
                raise AudioStreamError(502, "asr_unexpected_final", "识别服务提前结束了录音。")
            # Copy only bounded model metadata, never recognized text, into audit fields.
            if isinstance(item.get("recognizer"), str):
                details["recognizer"] = item["recognizer"][:100]
            if isinstance(item.get("hotword_count"), int):
                details["hotword_count"] = max(0, min(item["hotword_count"], 10000))
            if kind == "final" and isinstance(item.get("processing_ms"), (float, int)):
                details["processing_ms"] = max(0, round(item["processing_ms"], 1))
            await socket.send_text(raw)
            if kind == "partial" and item["text"] and details.get("first_text_ms") is None:
                details["first_text_ms"] = round(elapsed_ms(), 1)
                emit("asr.first_text")
            if kind == "final":
                details["correction_ms"] = round(elapsed_ms() - details["stop_ms"], 1)
                outcome.update(status=200, outcome="completed", error_code="")
                return
        raise AudioStreamError(502, "asr_missing_final", "识别连接已结束，但没有收到最终文字，请重试。")

    async def watchdog():
        try:
            await asyncio.wait_for(stop.wait(), RECORD_TIMEOUT)
        except asyncio.TimeoutError:
            raise AudioStreamError(408, "asr_recording_timeout", "录音连接时间过长，请重新录音。")
        await asyncio.sleep(FINAL_TIMEOUT)
        raise AudioStreamError(504, "asr_final_timeout", "整段校正超时，临时文字可保留后重试。")

    tasks = [asyncio.create_task(upload()), asyncio.create_task(download()), asyncio.create_task(watchdog())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        # A final result already delivered to the client takes precedence over close races.
        if outcome["outcome"] != "completed":
            for task in done:
                task.result()
        return outcome
    except WebSocketDisconnect:
        return {"status": 204, "outcome": "cancelled", "error_code": "client_disconnected"}
    except RuntimeError as exc:
        if any(word in str(exc).lower() for word in ("close", "disconnect", "websocket.send")):
            return {"status": 204, "outcome": "cancelled", "error_code": "client_disconnected"}
        raise AudioStreamError(500, "asr_internal_error", "语音连接内部错误，请重试。") from exc
    except ConnectionClosed:
        raise AudioStreamError(502, "asr_upstream_closed", "识别服务连接中断，请重新录音。")
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

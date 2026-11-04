"""獨立單輪聊天與圖片提示詞轉換，不讀取工作階段、工具或主機脈絡。"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from _bot_prompts import load_prompt

ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"
MODEL = "deepseek-ai/deepseek-v4.1-flash"
MAX_PROMPT_CHARS = 8000
MAX_REPLY_CHARS = 12000
MAX_RESPONSE_BYTES = 262144
MAX_CONCURRENT = 4
TIMEOUT_SECONDS = 300
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
IMAGE_FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}
MAX_GENERATION_PROMPT_CHARS = 4000
_GENERATION_SYSTEM = (
    "Convert the user's request and optional reference image into one English image-generation prompt. "
    "Describe the subject, composition, appearance and style faithfully. Return only the prompt, "
    "without explanations, code fences, pipe characters, role labels or tool calls. "
    "The reference is used only for visual description, not pixel editing."
)
_LOG = logging.getLogger(__name__)
_HTTP_FAILURE_CATEGORIES = {
    401: "authentication",
    403: "permission",
    404: "model_or_endpoint",
    429: "rate_limit",
}
_UNSUCCESSFUL_FINISHES = {
    "content_filter": "filtered",
    "tool_calls": "unsupported_output",
    "function_call": "unsupported_output",
    "insufficient_system_resource": "resource_limit",
    "aborted": "aborted",
}
_active_requests = 0
_DEFAULT_SYSTEM = (
    "你是 DeepSeek chan，小鯨魚。你聰明、愛吃白飯、偶爾想摸魚，嘴硬但心軟。"
    "被叫大肥魚時會俏皮反駁，不羞辱對方。用繁體中文自然聊天，認真問題認真回答。"
    "你是 AI 聊天角色，可以理解本輪附圖，沒有主機工具或其他對話記憶，"
    "不假裝執行操作或查過資料。"
)
SYSTEM_PROMPT = load_prompt("deepseek_system.md", _DEFAULT_SYSTEM)


class ChatUnavailable(Exception):
    """只攜帶固定原因代碼，不包含遠端回應或憑證。"""


class _ProgressCallbackFailure(Exception):
    """保留回呼錯誤，避免外層把它當成連線或解析失敗。"""


@dataclass(frozen=True)
class ChatImage:
    """已驗證並重新編碼的單張圖片，只供本輪請求使用。"""

    data_url: str


def prepare_image(data: bytes, content_type: str) -> ChatImage:
    from PIL import Image, ImageOps

    expected = IMAGE_FORMATS.get(content_type)
    if expected is None or not data or len(data) > MAX_IMAGE_BYTES:
        raise ChatUnavailable("image")
    try:
        with Image.open(io.BytesIO(data), formats=tuple(IMAGE_FORMATS.values())) as original:
            if (original.format != expected or original.width * original.height > MAX_IMAGE_PIXELS
                    or getattr(original, "n_frames", 1) != 1):
                raise ChatUnavailable("image")
            original.load()
            with ImageOps.exif_transpose(original) as oriented:
                with oriented.convert("RGBA") as rgba, Image.new("RGB", oriented.size, "white") as image:
                    image.paste(rgba, mask=rgba.getchannel("A"))
                    image.thumbnail((2048, 2048))
                    output = io.BytesIO()
                    image.save(output, format="JPEG", quality=85)
        encoded = output.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError):
        raise ChatUnavailable("image") from None
    if len(encoded) > MAX_IMAGE_BYTES:
        raise ChatUnavailable("image")
    return ChatImage("data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii"))


def _api_key() -> str:
    key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if not key:
        filename = os.environ.get("NVIDIA_API_KEY_FILE", "").strip()
        if filename:
            try:
                with Path(filename).open(encoding="utf-8-sig") as stream:
                    key = stream.read(8193).strip()
            except (OSError, UnicodeError, ValueError):
                raise ChatUnavailable("configuration") from None
    if not key or len(key) > 8192 or any(c.isspace() for c in key):
        raise ChatUnavailable("configuration")
    return key


async def completion(prompt: str, *, image: ChatImage | None = None,
                     on_text: Callable[[str], None] | None = None) -> str:
    return await _request(prompt, image=image, on_text=on_text, for_generation=False)


async def generation_prompt(prompt: str, *, image: ChatImage | None = None) -> str:
    text = await _request(prompt, image=image, on_text=None, for_generation=True)
    if len(text) > MAX_GENERATION_PROMPT_CHARS or "```" in text or "|" in text:
        raise ChatUnavailable("generation")
    return " ".join(text.split())


async def _request(prompt: str, *, image: ChatImage | None,
                   on_text: Callable[[str], None] | None, for_generation: bool) -> str:
    global _active_requests
    if not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
        raise ChatUnavailable("input")
    try:
        key = _api_key()
    except ChatUnavailable:
        _LOG.warning("public_chat failure category=configuration")
        raise
    # 同一個事件迴圈內，檢查與取得名額之間不能 await，也不排無上限佇列。
    if _active_requests >= MAX_CONCURRENT:
        raise ChatUnavailable("busy")
    _active_requests += 1
    try:
        payload = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": _GENERATION_SYSTEM if for_generation else SYSTEM_PROMPT},
                {"role": "user", "content": prompt if image is None else [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image.data_url}},
                ]},
            ],
            "stream": True,
            "max_tokens": 2048,
            "temperature": 1,
            "top_p": 0.95,
        }
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                ENDPOINT, json=payload,
                headers={"Authorization": f"Bearer {key}"},
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    _LOG.warning(
                        "public_chat failure category=%s status=%d",
                        _HTTP_FAILURE_CATEGORIES.get(response.status, "upstream"),
                        response.status,
                    )
                    raise ChatUnavailable("busy" if response.status == 429 else "upstream")
                return await _stream_answer(response.content, on_text, require_complete=for_generation)
    except _ProgressCallbackFailure as error:
        raise error.args[0] from None
    except asyncio.TimeoutError:
        _LOG.warning("public_chat failure category=timeout")
        raise ChatUnavailable("timeout") from None
    except (aiohttp.ClientError, OSError, ValueError):
        _LOG.warning("public_chat failure category=transport")
        raise ChatUnavailable("upstream") from None
    finally:
        _active_requests -= 1


async def _stream_answer(content, on_text: Callable[[str], None] | None, *,
                         require_complete: bool = False) -> str:
    buffer = bytearray()
    event_lines: list[bytes] = []
    received = 0
    text = ""
    finish_reason = None
    done = False
    clipped = False
    async for chunk in content.iter_chunked(16384):
        received += len(chunk)
        if received > MAX_RESPONSE_BYTES:
            _LOG.warning("public_chat failure category=response_size")
            raise ChatUnavailable("response")
        buffer.extend(chunk)
        while b"\n" in buffer:
            raw_line, _, remainder = buffer.partition(b"\n")
            buffer = bytearray(remainder)
            line = raw_line.rstrip(b"\r")
            if line.startswith(b"data:"):
                event_lines.append(line[5:].lstrip(b" "))
            if line or not event_lines:
                continue
            event = b"\n".join(event_lines)
            event_lines.clear()
            if event == b"[DONE]":
                done = True
                break
            try:
                data = json.loads(event)
            except (ValueError, UnicodeError, RecursionError):
                _LOG.warning("public_chat failure category=invalid_json")
                raise ChatUnavailable("upstream") from None
            failure_detail = "choices_shape"
            try:
                choices = data["choices"]
                if not isinstance(choices, list):
                    raise TypeError
                if not choices:
                    continue
                failure_detail = "choice_shape"
                choice = choices[0]
                if not isinstance(choice, dict):
                    raise TypeError
                failure_detail = "delta_shape"
                delta = choice["delta"]
                if not isinstance(delta, dict):
                    raise TypeError
                piece = delta.get("content")
                reason = choice.get("finish_reason")
                failure_detail = "content_shape"
                if piece is not None and not isinstance(piece, str):
                    raise TypeError
                failure_detail = "finish_reason"
                if reason is not None and (not isinstance(reason, str)
                        or reason not in ("stop", "length") and reason not in _UNSUCCESSFUL_FINISHES):
                    raise ValueError
                failure_detail = "after_finish"
                if finish_reason is not None and (piece or reason is not None):
                    raise ValueError
            except (KeyError, IndexError, TypeError, AttributeError, ValueError):
                _LOG.warning("public_chat failure category=malformed_response detail=%s", failure_detail)
                raise ChatUnavailable("upstream") from None
            if reason in _UNSUCCESSFUL_FINISHES:
                category = _UNSUCCESSFUL_FINISHES[reason]
                _LOG.warning("public_chat failure category=%s", category)
                raise ChatUnavailable(category)
            if piece:
                available = MAX_REPLY_CHARS - len(text)
                clipped |= len(piece) > available
                text += piece[:available]
                if on_text is not None and text.strip():
                    try:
                        on_text(text)
                    except (aiohttp.ClientError, OSError, ValueError) as error:
                        raise _ProgressCallbackFailure(error) from None
            if reason is not None:
                finish_reason = reason
        if done:
            break
    if not done or finish_reason is None:
        _LOG.warning("public_chat failure category=incomplete_stream")
        raise ChatUnavailable("upstream")
    text = text.strip()
    if require_complete and (finish_reason != "stop" or clipped or not text):
        raise ChatUnavailable("generation")
    if finish_reason == "length" and not text:
        return "這次回答已達長度上限，還沒產生可顯示的答案。請縮小問題後再試。"
    if not text:
        _LOG.warning("public_chat failure category=empty_response")
        raise ChatUnavailable("response")
    if clipped:
        text += "\n（回覆過長，已截短。）"
    if finish_reason == "length":
        return "（回答已達本次長度上限，以下內容尚未完成。）\n\n" + text
    return text

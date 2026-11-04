"""公開聊天的請求隔離、失敗處理及傳送邊界。"""
import asyncio
import base64
import io
import json
import types
from collections import Counter
from unittest.mock import AsyncMock

import aiohttp
import pytest
from PIL import Image

import _deepseek_chat as chat
import discord_bot as bot


@pytest.mark.parametrize("owner", [False, True])
@pytest.mark.parametrize("channel_id", [bot.CHANNEL_ID, bot.CHANNEL_ID + 1])
def test_chat_command_gates_owner_and_streams_text(endpoint, monkeypatch, owner, channel_id):
    sent = types.SimpleNamespace(edit=AsyncMock())
    response = types.SimpleNamespace(
        is_done=lambda: False, defer=AsyncMock(), send_message=AsyncMock())
    interaction = types.SimpleNamespace(
        command=bot.slash_chat,
        user=types.SimpleNamespace(id=bot.OWNER_USER_ID if owner else bot.OWNER_USER_ID + 1),
        channel_id=channel_id, channel=types.SimpleNamespace(send=AsyncMock()),
        guild=None, id=123, extras={}, namespace=types.SimpleNamespace(prompt="嗨"),
        type=bot.discord.InteractionType.application_command,
        response=response, followup=types.SimpleNamespace(send=AsyncMock(return_value=sent)),
    )
    monkeypatch.setattr(bot, "_audit_log", lambda *args: None)
    monkeypatch.setattr(bot, "_METRICS_CMD_COUNTS", Counter())
    monkeypatch.setattr(bot, "_roles_configured", lambda: False)

    async def invoke():
        allowed = await bot.tree.interaction_check(interaction)
        assert allowed is owner
        if allowed:
            await bot.slash_chat.callback(interaction, "嗨")

    asyncio.run(invoke())
    if owner:
        response.defer.assert_awaited_once_with(thinking=True)
        assert endpoint.calls[0][1]["json"]["messages"][-1] == {"role": "user", "content": "嗨"}
        assert sent.edit.call_args.kwargs["content"] == "一碗白飯就夠啦。"
        assert sent.edit.call_args.kwargs["allowed_mentions"].to_dict()["parse"] == []
        assert interaction.followup.send.call_args.kwargs["allowed_mentions"].to_dict()["parse"] == []
        interaction.channel.send.assert_not_awaited()
    else:
        assert not endpoint.calls
        response.send_message.assert_awaited_once()
        response.defer.assert_not_awaited()
        interaction.followup.send.assert_not_awaited()


def sse(data):
    return b"data: " + json.dumps(data, ensure_ascii=False).encode() + b"\n\n"


def event(text=None, finish=None, **delta):
    return sse({"choices": [{"delta": {"content": text, **delta}, "finish_reason": finish}]})


@pytest.fixture
def endpoint(monkeypatch):
    calls = []
    state = types.SimpleNamespace(
        status=200, body={"choices": [{"message": {"content": "一碗白飯就夠啦。"}}]},
        error=None, wait=None, chunks=None, after_chunk=None,
    )

    class Response:
        @property
        def status(self):
            return state.status

        @property
        def content(self):
            return self

        async def iter_chunked(self, size):
            if state.wait:
                await state.wait.wait()
            body = state.body
            if not isinstance(body, bytes):
                body = dict(body)
                if isinstance(body.get("choices"), list):
                    body["choices"] = [dict(choice) for choice in body["choices"]]
                    for choice in body["choices"]:
                        choice["delta"] = choice.pop("message", {})
                        choice.setdefault("finish_reason", "stop")
                body = sse(body) + b"data: [DONE]\n\n"
            chunks = state.chunks if state.chunks is not None else [
                body[offset:offset + size] for offset in range(0, len(body), size)]
            for chunk in chunks:
                yield chunk
                if state.after_chunk is not None:
                    await state.after_chunk()

        async def __aenter__(self):
            if state.error:
                raise state.error
            return self

        async def __aexit__(self, *args):
            return False

    class Session(Response):
        def __init__(self, **kwargs):
            state.timeout = kwargs["timeout"]

        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    monkeypatch.delenv("NVIDIA_API_KEY_FILE", raising=False)
    monkeypatch.setattr(chat.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(chat, "_active_requests", 0)
    state.calls = calls
    return state


def test_request_contains_only_independent_persona_and_current_prompt(endpoint):
    assert asyncio.run(chat.completion("你好")) == "一碗白飯就夠啦。"
    url, request = endpoint.calls[0]
    assert url == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert request["headers"] == {"Authorization": "Bearer test-key"}
    assert request["allow_redirects"] is False
    payload = request["json"]
    assert payload["model"] == "deepseek-ai/deepseek-v4.1-flash"
    assert payload["messages"] == [
        {"role": "system", "content": chat.SYSTEM_PROMPT},
        {"role": "user", "content": "你好"},
    ]
    assert "小鯨魚" in payload["messages"][0]["content"]
    assert "tools" not in payload
    assert payload["stream"] is True
    assert endpoint.timeout.total == chat.TIMEOUT_SECONDS
    asyncio.run(chat.completion("另一個人"))
    assert endpoint.calls[1][1]["json"]["messages"][-1]["content"] == "另一個人"
    assert len(endpoint.calls[1][1]["json"]["messages"]) == 2


def image_bytes(format="PNG", size=(32, 32)):
    output = io.BytesIO()
    with Image.new("RGB", size, "red") as picture:
        picture.save(output, format=format)
    return output.getvalue()


@pytest.fixture
def chat_interaction(monkeypatch):
    sent = types.SimpleNamespace(edit=AsyncMock(), reply=AsyncMock())
    channel = types.SimpleNamespace(id=bot.CHANNEL_ID + 1, guild=None,
                                    send=AsyncMock(return_value=sent))
    interaction = types.SimpleNamespace(
        command=bot.slash_chat, user=types.SimpleNamespace(id=bot.OWNER_USER_ID),
        channel_id=channel.id, channel=channel, guild=None, id=123, extras={},
        namespace=types.SimpleNamespace(prompt="描述這張圖"),
        type=bot.discord.InteractionType.application_command,
        response=types.SimpleNamespace(is_done=lambda: False, defer=AsyncMock(), send_message=AsyncMock()),
        followup=types.SimpleNamespace(send=AsyncMock(return_value=sent)),
    )
    monkeypatch.setattr(bot, "_audit_log", lambda *args: None)
    monkeypatch.setattr(bot, "_METRICS_CMD_COUNTS", Counter())
    monkeypatch.setattr(bot, "_roles_configured", lambda: False)
    return interaction, sent


def attachment(data, content_type="image/png", **overrides):
    """附件替身。bot 用平台函式庫的 `Attachment.read()` 下載；`item.data` 是讀回來的
    位元組，放一個例外就是下載失敗，`item.reads` 記讀了幾次。"""
    fields = dict(size=len(data), content_type=content_type,
                  url="https://cdn.discordapp.com/attachments/123/456/reference.png")
    fields.update(overrides)
    item = types.SimpleNamespace(data=data, reads=0, **fields)

    async def read():
        item.reads += 1
        if isinstance(item.data, BaseException):
            raise item.data
        return item.data

    item.read = read
    return item


@pytest.mark.parametrize("format,mime", [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")])
def test_chat_image_attachment_reaches_api_as_validated_data(endpoint, chat_interaction, format, mime):
    interaction, sent = chat_interaction
    item = attachment(image_bytes(format), mime)
    async def invoke():
        assert await bot.tree.interaction_check(interaction)
        await bot.slash_chat.callback(interaction, "描述這張圖", item)
    asyncio.run(invoke())
    interaction.response.defer.assert_awaited_once_with(thinking=True)
    blocks = endpoint.calls[0][1]["json"]["messages"][-1]["content"]
    assert blocks[0] == {"type": "text", "text": "描述這張圖"}
    assert blocks[1]["type"] == "image_url"
    data_url = blocks[1]["image_url"]["url"]
    assert data_url.startswith("data:image/jpeg;base64,")
    with Image.open(io.BytesIO(base64.b64decode(data_url.split(",", 1)[1]))) as picture:
        assert picture.format == "JPEG" and picture.size == (32, 32)
    assert item.reads == 1
    assert sent.edit.call_args.kwargs["content"] == "一碗白飯就夠啦。"


def test_image_transparency_is_composited_on_white():
    output = io.BytesIO()
    with Image.new("RGBA", (64, 64), (0, 0, 0, 0)) as picture:
        for x in range(16, 48):
            for y in range(16, 48):
                picture.putpixel((x, y), (0, 0, 0, 255))
        picture.save(output, format="PNG")
    prepared = chat.prepare_image(output.getvalue(), "image/png")
    with Image.open(io.BytesIO(base64.b64decode(prepared.data_url.split(",", 1)[1]))) as picture:
        assert min(picture.getpixel((0, 0))) > 240
        assert max(picture.getpixel((32, 32))) < 15


@pytest.mark.parametrize("case", ["mime", "bad", "mismatch", "declared_size", "actual_size", "pixels", "url", "download"])
def test_invalid_attachment_never_reaches_chat_or_generation(endpoint, chat_interaction, monkeypatch, case):
    interaction, _ = chat_interaction
    data = image_bytes()
    item = attachment(data)
    if case == "mime":
        item.content_type = "image/svg+xml"
    elif case == "bad":
        data = b"not an image"
    elif case == "mismatch":
        item.content_type = "image/jpeg"
    elif case == "declared_size":
        item.size = chat.MAX_IMAGE_BYTES + 1
    elif case == "actual_size":
        monkeypatch.setattr(chat, "MAX_IMAGE_BYTES", len(data) - 1)
        item.size = 1
    elif case == "pixels":
        monkeypatch.setattr(chat, "MAX_IMAGE_PIXELS", 100)
    elif case == "url":
        item.url = "https://localhost/private"
    if case == "download":
        data = bot.discord.NotFound(types.SimpleNamespace(status=404, reason="Not Found"), "gone")
    item.data = data
    generate = AsyncMock()
    monkeypatch.setattr(bot, "mcmd_generate", generate)
    asyncio.run(bot.slash_chat.callback(interaction, "畫這張圖", item, "image"))
    assert not endpoint.calls
    generate.assert_not_awaited()
    assert "圖片無法讀取" in interaction.channel.send.call_args.kwargs["content"]


@pytest.mark.parametrize("answer,finish", [("a cat", "length"), ("", "stop"), ("cat | other", "stop"),
                                         ("```a cat```", "stop"), ("x" * 4001, "stop"),
                                         ("x" * 12001, "stop")])
def test_invalid_generation_prompt_is_never_enqueued(endpoint, chat_interaction, monkeypatch, answer, finish):
    interaction, _ = chat_interaction
    endpoint.body = {"choices": [{"message": {"content": answer}, "finish_reason": finish}]}
    generate = AsyncMock()
    monkeypatch.setattr(bot, "mcmd_generate", generate)
    asyncio.run(bot.slash_chat.callback(interaction, "畫一隻貓", mode="image"))
    generate.assert_not_awaited()
    assert "沒有排入產圖佇列" in interaction.channel.send.call_args.kwargs["content"]


@pytest.mark.parametrize("failure", ["http", "incomplete", "cancel"])
def test_failed_generation_request_never_enqueues(endpoint, chat_interaction, monkeypatch, failure):
    interaction, _ = chat_interaction
    if failure == "http":
        endpoint.status = 503
    elif failure == "incomplete":
        endpoint.chunks = [event("a partial cat")]
    else:
        endpoint.error = asyncio.CancelledError()
    generate = AsyncMock()
    monkeypatch.setattr(bot, "mcmd_generate", generate)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(bot.slash_chat.callback(interaction, "畫一隻貓", mode="image"))
    else:
        asyncio.run(bot.slash_chat.callback(interaction, "畫一隻貓", mode="image"))
        assert "這次請求失敗" in interaction.channel.send.call_args.kwargs["content"]
    generate.assert_not_awaited()
    assert chat._active_requests == 0


def test_non_owner_image_mode_cannot_download_or_generate(endpoint, chat_interaction, monkeypatch):
    interaction, _ = chat_interaction
    interaction.user.id = bot.OWNER_USER_ID + 1
    generate = AsyncMock()
    monkeypatch.setattr(bot, "mcmd_generate", generate)
    data = image_bytes()
    async def invoke():
        assert not await bot.tree.interaction_check(interaction)
        await bot.slash_chat.callback(interaction, "畫一隻貓", item, "image")
    item = attachment(data)
    asyncio.run(invoke())
    assert not endpoint.calls and item.reads == 0
    generate.assert_not_awaited()


@pytest.mark.parametrize("with_image", [False, True])
def test_chat_generation_uses_existing_queue_and_returns_completed_image(endpoint, chat_interaction, monkeypatch, tmp_path, with_image):
    interaction, sent = chat_interaction
    endpoint.body = {"choices": [{"message": {"content": "a red cat, watercolor"}}]}
    monkeypatch.setattr(bot, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(bot, "OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(bot, "SINGLE_IMAGE_REQUEST_FILE", tmp_path / "request.json")
    monkeypatch.setattr(bot, "_generate_queue", [])
    monkeypatch.setattr(bot, "_generate_inflight", None)
    monkeypatch.setattr(bot, "_single_image_pending", {})
    monkeypatch.setattr(bot, "_sweep_stale_single_image_state", lambda: None)
    monkeypatch.setattr(bot, "_generate_append_history", lambda record: None)
    monkeypatch.setattr(bot, "_generate_ensure_server", AsyncMock())
    monkeypatch.setattr(bot, "_schedule_coro", lambda coro: coro.close())
    monkeypatch.setattr(bot, "client", types.SimpleNamespace(get_channel=lambda channel_id: interaction.channel))
    async def invoke():
        assert await bot.tree.interaction_check(interaction)
        await bot.slash_chat.callback(interaction, "畫一隻貓",
                                      attachment(image_bytes()) if with_image else None, "image")
        request = json.loads(bot.SINGLE_IMAGE_REQUEST_FILE.read_text(encoding="utf-8"))
        assert request["prompt"] == "a red cat, watercolor" + bot.DEFAULT_GENERATE_PROMPT_SUFFIX
        assert set(request) == {"request_id", "prompt"}
        request_id = request["request_id"]
        assert bot._generate_inflight == request_id
        assert bot._single_image_pending[request_id]["placeholder"] is sent
        result = tmp_path / "output" / "result.png"
        result.parent.mkdir()
        result.write_bytes(image_bytes())
        await bot._handle_single_image_done(interaction.channel, {
            "request_id": request_id, "ok": True, "path": "output/result.png"})
        file = sent.reply.call_args.kwargs["file"]
        try:
            assert file.filename == "result.png"
            assert file.fp.read() == result.read_bytes()
        finally:
            file.close()
        assert not bot._single_image_pending and bot._generate_inflight is None
    asyncio.run(invoke())
    interaction.followup.send.assert_awaited_once()
    assert "已收下產圖請求" in interaction.followup.send.call_args.args[0]
    assert "Convert the user's request" in endpoint.calls[0][1]["json"]["messages"][0]["content"]
    if with_image:
        assert endpoint.calls[0][1]["json"]["messages"][-1]["content"][1]["type"] == "image_url"


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
def test_http_errors_are_sanitized_and_never_retried(endpoint, status):
    endpoint.status = status
    endpoint.body = {"error": "secret host-path credential"}
    with pytest.raises(chat.ChatUnavailable) as error:
        asyncio.run(chat.completion("你好"))
    assert str(error.value) == ("busy" if status == 429 else "upstream")
    assert len(endpoint.calls) == 1
    assert chat._active_requests == 0


@pytest.mark.parametrize("body", [
    b"not json secret", {}, {"choices": []}, {"choices": [{"message": {"content": None}}]},
    {"choices": [{"message": {"content": "  ", "reasoning_content": "private thought"}}]},
    b"x" * (chat.MAX_RESPONSE_BYTES + 1),
], ids=["invalid-json", "missing-choices", "empty-choices", "null-content", "reasoning-only", "oversize"])
def test_bad_responses_never_expose_raw_body_or_reasoning(endpoint, body):
    endpoint.body = body
    with pytest.raises(chat.ChatUnavailable) as error:
        asyncio.run(chat.completion("你好"))
    assert str(error.value) in {"response", "upstream"}
    assert chat._active_requests == 0


@pytest.mark.parametrize("failure,reason", [
    (asyncio.TimeoutError("private"), "timeout"),
    (aiohttp.ClientConnectionError("secret"), "upstream"),
])
def test_connection_failure_and_timeout_release_capacity(endpoint, failure, reason):
    endpoint.error = failure
    with pytest.raises(chat.ChatUnavailable, match=reason):
        asyncio.run(chat.completion("你好"))
    assert chat._active_requests == 0


def test_busy_requests_are_rejected_and_cancellation_releases_capacity(endpoint, monkeypatch):
    monkeypatch.setattr(chat, "MAX_CONCURRENT", 1)

    async def scenario():
        endpoint.wait = asyncio.Event()
        first = asyncio.create_task(chat.completion("第一個"))
        await asyncio.sleep(0)
        with pytest.raises(chat.ChatUnavailable, match="busy"):
            await chat.completion("第二個")
        assert len(endpoint.calls) == 1
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert chat._active_requests == 0
        endpoint.wait.set()
        assert await chat.completion("第三個")

    asyncio.run(scenario())


@pytest.mark.parametrize("prompt", ["", " " * 3, "x" * (chat.MAX_PROMPT_CHARS + 1)],
                         ids=["empty", "whitespace", "oversize"])
def test_invalid_prompt_does_not_make_a_request(endpoint, prompt):
    with pytest.raises(chat.ChatUnavailable, match="input"):
        asyncio.run(chat.completion(prompt))
    assert not endpoint.calls


def test_key_file_and_missing_credentials(endpoint, monkeypatch, tmp_path):
    monkeypatch.delenv("NVIDIA_API_KEY")
    with pytest.raises(chat.ChatUnavailable, match="configuration"):
        asyncio.run(chat.completion("你好"))
    key_file = tmp_path / "key.txt"
    key_file.write_text("file-key\n", encoding="utf-8-sig")
    monkeypatch.setenv("NVIDIA_API_KEY_FILE", str(key_file))
    asyncio.run(chat.completion("你好"))
    assert endpoint.calls[-1][1]["headers"]["Authorization"] == "Bearer file-key"
    key_file.write_text("bad\nkey", encoding="utf-8")
    with pytest.raises(chat.ChatUnavailable, match="configuration"):
        asyncio.run(chat.completion("你好"))


def test_reply_length_is_bounded(endpoint):
    endpoint.body = {"choices": [{"message": {"content": "文" * (chat.MAX_REPLY_CHARS + 10)}}]}
    answer = asyncio.run(chat.completion("你好"))
    assert len(answer) < chat.MAX_REPLY_CHARS + 30
    assert answer.endswith("（回覆過長，已截短。）")


@pytest.fixture
def mention(monkeypatch):
    monkeypatch.setattr(bot, "client", types.SimpleNamespace(user=types.SimpleNamespace(id=789)))
    message = types.SimpleNamespace(
        content="<@789> 嗨", author=types.SimpleNamespace(id=bot.OWNER_USER_ID + 1),
    )
    sent = []

    async def reply(_message, text, **kwargs):
        sent.append((text, kwargs))

    monkeypatch.setattr(bot, "safe_reply", reply)
    return message, sent


def test_long_chat_reply_is_chunked_without_pinging_anyone(mention, monkeypatch):
    message, sent = mention
    answer = "@everyone <@123>\n" + "x" * 5000

    async def complete(prompt, *, on_text=None):
        assert prompt == "嗨"
        return answer

    monkeypatch.setattr(chat, "completion", complete)
    asyncio.run(bot._handle_mention(message, question_entry=False))
    assert len(sent) >= 3
    assert all(len(text) <= 1900 for text, _ in sent)
    assert all(kwargs["allowed_mentions"].to_dict()["parse"] == [] for _, kwargs in sent)


def test_unknown_failure_reason_is_not_echoed(mention, monkeypatch):
    message, sent = mention

    async def complete(prompt, *, on_text=None):
        raise chat.ChatUnavailable("secret-key host-path")

    monkeypatch.setattr(chat, "completion", complete)
    asyncio.run(bot._handle_mention(message, question_entry=False))
    assert sent[-1][0] == "小鯨魚暫時回不了話，請稍後再試。"


@pytest.mark.parametrize("is_bot,webhook", [(True, None), (False, 123)])
def test_bot_and_webhook_mentions_do_not_start_chat(mention, monkeypatch, is_bot, webhook):
    message, sent = mention
    message.author.bot = is_bot
    message.webhook_id = webhook

    async def forbidden(prompt):
        pytest.fail("自動化訊息不可觸發公開聊天")

    monkeypatch.setattr(chat, "completion", forbidden)
    asyncio.run(bot._handle_mention(message, question_entry=False))
    assert sent == []

@pytest.mark.parametrize("content", ["def solve():\n    return", "```python\ndef solve():", "文" * 12020],
                         ids=["partial", "open-fence", "long-partial"])
def test_remote_length_limit_is_visible_before_partial_content(endpoint, content):
    endpoint.body = {"choices": [{"finish_reason": "length", "message": {"content": content}}]}
    answer = asyncio.run(chat.completion("private-prompt"))
    assert answer.startswith("（回答已達本次長度上限，以下內容尚未完成。）\n\n")
    assert content[:min(len(content), chat.MAX_REPLY_CHARS)] in answer
    assert len(endpoint.calls) == 1


@pytest.mark.parametrize("content", [None, "", "  "])
def test_length_limit_without_content_does_not_reveal_reasoning(endpoint, content):
    endpoint.body = {"choices": [{"finish_reason": "length", "message": {
        "content": content, "reasoning_content": "private-reasoning",
    }}]}
    answer = asyncio.run(chat.completion("private-prompt"))
    assert answer == "這次回答已達長度上限，還沒產生可顯示的答案。請縮小問題後再試。"
    assert len(endpoint.calls) == 1


def test_normal_stop_remains_unchanged(endpoint):
    endpoint.body = {"choices": [{"finish_reason": "stop", "message": {"content": "完整答案"}}]}
    assert asyncio.run(chat.completion("問題")) == "完整答案"


@pytest.mark.parametrize("status,category", [
    (401, "authentication"), (403, "permission"), (404, "model_or_endpoint"),
    (429, "rate_limit"), (503, "upstream"),
])
def test_http_diagnostics_keep_only_status_and_fixed_category(endpoint, caplog, status, category):
    endpoint.status = status
    endpoint.body = {"error": "private-upstream-body"}
    with pytest.raises(chat.ChatUnavailable):
        asyncio.run(chat.completion("private-prompt"))
    records = [r for r in caplog.records if r.name == chat.__name__]
    assert len(records) == 1
    assert records[0].getMessage() == f"public_chat failure category={category} status={status}"
    assert records[0].exc_info is None
    assert all(secret not in caplog.text for secret in (
        "test-key", "private-prompt", "private-upstream-body", "Authorization"))


@pytest.mark.parametrize("body,category", [
    (b"data: private-invalid-json\n\n", "invalid_json"), ({}, "malformed_response detail=choices_shape"),
    ({"choices": [{"message": {"content": None}}]}, "empty_response"),
    (b"x" * 262145, "response_size"),
], ids=["json", "shape", "empty", "size"])
def test_response_diagnostics_are_fixed_codes(endpoint, caplog, body, category):
    endpoint.body = body
    with pytest.raises(chat.ChatUnavailable):
        asyncio.run(chat.completion("private-prompt"))
    records = [r for r in caplog.records if r.name == chat.__name__]
    assert [r.getMessage() for r in records] == [f"public_chat failure category={category}"]
    assert "private" not in caplog.text


@pytest.mark.parametrize("wire,detail", [
    ({"choices": "private-choices"}, "choices_shape"),
    ({"choices": ["private-choice"]}, "choice_shape"),
    ({"choices": [{"delta": "private-delta"}]}, "delta_shape"),
    ({"choices": [{"delta": {"content": ["private-content"]}}]}, "content_shape"),
    ({"choices": [{"delta": {}, "finish_reason": "private-reason"}]}, "finish_reason"),
])
def test_malformed_stream_diagnostics_identify_shape_without_values(endpoint, caplog, wire, detail):
    endpoint.chunks = [sse(wire), b"data: [DONE]\n\n"]
    with pytest.raises(chat.ChatUnavailable):
        asyncio.run(chat.generation_prompt("private-prompt"))
    assert f"category=malformed_response detail={detail}" in caplog.text
    assert "private" not in caplog.text and "test-key" not in caplog.text


@pytest.mark.parametrize("reason,category", [("content_filter", "filtered"),
                                            ("tool_calls", "unsupported_output"),
                                            ("function_call", "unsupported_output"),
                                            ("insufficient_system_resource", "resource_limit"),
                                            ("aborted", "aborted")])
def test_valid_non_success_finish_is_not_malformed_or_enqueued(endpoint, chat_interaction, monkeypatch, caplog, reason, category):
    interaction, _ = chat_interaction
    endpoint.chunks = [event("partial prompt"), event(finish=reason), b"data: [DONE]\n\n"]
    generate = AsyncMock()
    monkeypatch.setattr(bot, "mcmd_generate", generate)
    asyncio.run(bot.slash_chat.callback(interaction, "畫一隻貓", mode="image"))
    generate.assert_not_awaited()
    assert f"category={category}" in caplog.text
    assert "malformed_response" not in caplog.text
    assert "沒有排入產圖佇列" in interaction.channel.send.call_args.kwargs["content"]


@pytest.mark.parametrize("ending,detail", [
    ([sse({"choices": [{"delta": None, "finish_reason": "stop"}]})], "delta_shape"),
    ([event(finish="stop"), event(finish="stop")], "after_finish"),
    ([event(finish="stop"), event("late text")], "after_finish"),
    ([event(finish="unknown-private-reason")], "finish_reason"),
    ([sse({"choices": [{"delta": {}, "finish_reason": []}]})], "finish_reason"),
])
def test_unverified_stream_shapes_still_fail_closed(endpoint, caplog, ending, detail):
    endpoint.chunks = [event("prompt"), *ending, b"data: [DONE]\n\n"]
    with pytest.raises(chat.ChatUnavailable, match="upstream"):
        asyncio.run(chat.generation_prompt("draw"))
    assert f"category=malformed_response detail={detail}" in caplog.text
    assert "private" not in caplog.text


@pytest.mark.parametrize("failure,category", [
    (asyncio.TimeoutError("private-timeout"), "timeout"),
    (aiohttp.ClientConnectionError("private-network"), "transport"),
])
def test_transport_diagnostics_never_include_exception_text(endpoint, caplog, failure, category):
    endpoint.error = failure
    with pytest.raises(chat.ChatUnavailable):
        asyncio.run(chat.completion("private-prompt"))
    records = [r for r in caplog.records if r.name == chat.__name__]
    assert [r.getMessage() for r in records] == [f"public_chat failure category={category}"]
    assert "private" not in caplog.text
    assert all(r.exc_info is None for r in records)


def test_configuration_failure_is_logged_without_path(endpoint, monkeypatch, tmp_path, caplog):
    monkeypatch.delenv("NVIDIA_API_KEY")
    monkeypatch.setenv("NVIDIA_API_KEY_FILE", str(tmp_path / "private-key-path"))
    with pytest.raises(chat.ChatUnavailable, match="configuration"):
        asyncio.run(chat.completion("private-prompt"))
    records = [r for r in caplog.records if r.name == chat.__name__]
    assert [r.getMessage() for r in records] == ["public_chat failure category=configuration"]
    assert "private" not in caplog.text
    assert not endpoint.calls


def test_cancellation_releases_capacity_without_failure_log(endpoint, caplog):
    async def scenario():
        endpoint.wait = asyncio.Event()
        task = asyncio.create_task(chat.completion("private-prompt"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert chat._active_requests == 0
    assert not [r for r in caplog.records if r.name == chat.__name__]


def test_fragmented_stream_exposes_only_answer_snapshots(endpoint):
    wire = (b": heartbeat\r\n\r\n" + event(reasoning_content="private-thought")
            + event("你好") + event("，世界") + event(finish="stop") + b"data: [DONE]\n\n")
    wire = wire.replace(b"\n", b"\r\n")
    endpoint.chunks = [wire[i:i + 1] for i in range(len(wire))]
    updates = []
    assert asyncio.run(chat.completion("嗨", on_text=updates.append)) == "你好，世界"
    assert updates == ["你好", "你好，世界"]


@pytest.mark.parametrize("ending", [b"", b"data: [DONE]\n\n", event(finish="stop")])
def test_incomplete_stream_does_not_claim_success(endpoint, ending, caplog):
    endpoint.chunks = [event("部分答案"), ending]
    updates = []
    with pytest.raises(chat.ChatUnavailable, match="upstream"):
        asyncio.run(chat.completion("嗨", on_text=updates.append))
    assert updates == ["部分答案"]
    assert "category=incomplete_stream" in caplog.text
    assert chat._active_requests == 0


def test_stream_timeout_preserves_updates_and_releases_capacity(endpoint):
    endpoint.chunks = [event("部分答案")]
    async def timeout():
        raise asyncio.TimeoutError("private-timeout")
    endpoint.after_chunk = timeout
    updates = []
    with pytest.raises(chat.ChatUnavailable, match="timeout"):
        asyncio.run(chat.completion("嗨", on_text=updates.append))
    assert updates == ["部分答案"]
    assert chat._active_requests == 0


def test_live_reply_edits_before_stream_finishes_without_mentions(mention, endpoint, monkeypatch):
    message, sent = mention
    edits = []
    async def scenario():
        edited = asyncio.Event()
        async def edit(**kwargs):
            edits.append(kwargs)
            edited.set()
        async def reply(_message, text, **kwargs):
            sent.append((text, kwargs))
            return types.SimpleNamespace(edit=edit)
        monkeypatch.setattr(bot, "safe_reply", reply)
        endpoint.chunks = [event("@everyone 部分答案"), event("完成", "stop"), b"data: [DONE]\n\n"]
        async def wait_for_preview():
            await asyncio.wait_for(edited.wait(), 2)
        endpoint.after_chunk = wait_for_preview
        await bot._handle_mention(message, question_entry=False)
        assert edits[0]["content"] == "@everyone 部分答案"
        assert edits[-1]["content"] == "@everyone 部分答案完成"
        assert all(item["allowed_mentions"].to_dict()["parse"] == [] for item in edits)
        assert len(sent) == 1
    asyncio.run(scenario())


def test_live_timeout_keeps_partial_and_sanitized_failure(mention, endpoint, monkeypatch):
    message, sent = mention
    edits = []
    async def edit(**kwargs):
        edits.append(kwargs)
    async def reply(_message, text, **kwargs):
        sent.append((text, kwargs))
        return types.SimpleNamespace(edit=edit)
    monkeypatch.setattr(bot, "safe_reply", reply)
    endpoint.chunks = [event("部分答案")]
    async def timeout():
        raise asyncio.TimeoutError("private-timeout")
    endpoint.after_chunk = timeout
    asyncio.run(bot._handle_mention(message, question_entry=False))
    final = edits[-1]["content"]
    assert "部分答案" in final and "尚未完成" in final and "等待逾時" in final
    assert "private" not in final
    assert chat._active_requests == 0


def test_live_cancel_closes_edit_task_and_releases_capacity(mention, endpoint, monkeypatch):
    message, _ = mention
    async def scenario():
        preview = asyncio.Event()
        hold = asyncio.Event()
        edits = []
        async def edit(**kwargs):
            edits.append(kwargs["content"])
            preview.set()
        async def reply(*args, **kwargs):
            return types.SimpleNamespace(edit=edit)
        monkeypatch.setattr(bot, "safe_reply", reply)
        endpoint.chunks = [event("部分答案")]
        endpoint.after_chunk = hold.wait
        task = asyncio.create_task(bot._handle_mention(message, question_entry=False))
        await asyncio.wait_for(preview.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert chat._active_requests == 0
        assert "部分答案" in edits[-1] and "尚未完成" in edits[-1]
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [ValueError("callback"), OSError("callback"), asyncio.TimeoutError("callback")])
def test_callback_failure_is_not_reported_as_upstream(endpoint, caplog, failure):
    def broken_callback(text):
        raise failure
    with pytest.raises(type(failure)) as caught:
        asyncio.run(chat.completion("嗨", on_text=broken_callback))
    assert caught.value is failure
    assert chat._active_requests == 0
    assert not [r for r in caplog.records if r.name == chat.__name__]


def test_failed_final_edit_resends_answer_without_mentions(mention, endpoint, monkeypatch):
    message, sent = mention
    async def edit(**kwargs):
        assert kwargs["allowed_mentions"].to_dict()["parse"] == []
        raise OSError("private-edit-failure")
    async def reply(_message, text, **kwargs):
        sent.append((text, kwargs))
        return types.SimpleNamespace(edit=edit)
    monkeypatch.setattr(bot, "safe_reply", reply)
    asyncio.run(bot._handle_mention(message, question_entry=False))
    assert sent[-1][0] == "一碗白飯就夠啦。"
    assert all(kwargs["allowed_mentions"].to_dict()["parse"] == [] for _, kwargs in sent)
    assert all("private" not in text for text, _ in sent)



@pytest.mark.parametrize("url", [
    "http://cdn.discordapp.com/attachments/1/2/a.png",
    "https://example.com/a.png",
    "https://cdn.discordapp.com:8443/a.png",
    "https://user:pw@cdn.discordapp.com/a.png",
])
def test_chat_image_from_an_unexpected_url_is_refused_before_reading(url):
    item = attachment(image_bytes(), url=url)
    with pytest.raises(chat.ChatUnavailable):
        asyncio.run(bot._read_chat_image(item))
    assert item.reads == 0


@pytest.mark.parametrize("failure", [
    aiohttp.ClientConnectionError("cdn"), OSError("cdn"), asyncio.TimeoutError(),
    bot.discord.NotFound(types.SimpleNamespace(status=404, reason="Not Found"), "gone"),
])
def test_chat_image_download_failure_is_reported_as_an_image_error(failure):
    item = attachment(image_bytes())
    item.data = failure
    with pytest.raises(chat.ChatUnavailable) as caught:
        asyncio.run(bot._read_chat_image(item))
    assert caught.value.args == ("image",)


def test_chat_image_download_is_bounded_by_a_named_timeout(monkeypatch):
    """下載卡住要在具名的 `CHAT_IMAGE_DOWNLOAD_TIMEOUT_SEC` 內收掉，不是一直等。"""
    monkeypatch.setattr(bot, "CHAT_IMAGE_DOWNLOAD_TIMEOUT_SEC", 0.05)
    item = attachment(image_bytes())

    async def never():
        await asyncio.sleep(5)
        return item.data

    item.read = never
    with pytest.raises(chat.ChatUnavailable):
        asyncio.run(asyncio.wait_for(bot._read_chat_image(item), 2))

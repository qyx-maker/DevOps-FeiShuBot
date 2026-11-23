import json
import os
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import Lock
from time import monotonic

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    DeleteMessageReactionRequest,
    Emoji,
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)
from openai import APIStatusError

from app_config import load_project_environment
from deepseek import ConversationHistory, TokenUsage, ask_deepseek
from feishu_image import MAX_IMAGES, read_image_text

MAX_HISTORY_TURNS = 4
MAX_HISTORY_CHARS_PER_MESSAGE = 3000
MAX_HISTORY_CONVERSATIONS = 256
HISTORY_IDLE_SECONDS = 30 * 60
ConversationKey = tuple[str, ...]


def conversation_key(data: P2ImMessageReceiveV1) -> ConversationKey | None:
    message = data.event.message
    if not message.chat_id:
        return None
    if message.chat_type == "p2p":
        return ("p2p", message.chat_id)
    sender = data.event.sender
    sender_id = sender.sender_id.open_id if sender and sender.sender_id else None
    if message.chat_type == "group" and sender_id:
        return ("group", message.chat_id, sender_id, message.thread_id or "")
    return None


class Conversations:
    """同一会话按收到的顺序处理，只保留最近成功发送的问答。"""

    def __init__(self, workers: ThreadPoolExecutor):
        self._workers = workers
        self._lock = Lock()
        self._pending: dict[ConversationKey, deque[tuple[lark.Client, str, str, tuple[str, ...]]]] = {}
        self._history: OrderedDict[ConversationKey, tuple[float, ConversationHistory]] = OrderedDict()

    def submit(
        self, key: ConversationKey | None, api_client: lark.Client, message_id: str,
        user_text: str, image_keys: tuple[str, ...] = (),
    ) -> None:
        if key is None:
            self._workers.submit(process_message, api_client, message_id, user_text, image_keys=image_keys)
            return
        with self._lock:
            if key in self._pending:
                self._pending[key].append((api_client, message_id, user_text, image_keys))
                return
            self._pending[key] = deque([(api_client, message_id, user_text, image_keys)])
            try:
                self._workers.submit(self._drain, key)
            except RuntimeError:
                del self._pending[key]
                raise

    def _drain(self, key: ConversationKey) -> None:
        while True:
            with self._lock:
                pending = self._pending[key]
                if not pending:
                    del self._pending[key]
                    return
                api_client, message_id, user_text, image_keys = pending.popleft()
                cutoff = monotonic() - HISTORY_IDLE_SECONDS
                while self._history and next(iter(self._history.values()))[0] <= cutoff:
                    self._history.popitem(last=False)
                entry = self._history.get(key)
                history = entry[1] if entry else ()
            try:
                answer = process_message(api_client, message_id, user_text, history, image_keys=image_keys)
            except Exception as error:
                lark.logger.error(f"飞书消息处理异常: {type(error).__name__}")
                continue
            if answer is not None:
                turn = (
                    _limit_history_message(user_text),
                    _limit_history_message(answer),
                )
                with self._lock:
                    self._history[key] = (monotonic(), (*history, turn)[-MAX_HISTORY_TURNS:])
                    self._history.move_to_end(key)
                    if len(self._history) > MAX_HISTORY_CONVERSATIONS:
                        self._history.popitem(last=False)


def _limit_history_message(text: str) -> str:
    if len(text) <= MAX_HISTORY_CHARS_PER_MESSAGE:
        return text
    return text[: MAX_HISTORY_CHARS_PER_MESSAGE - 1] + "…"


def add_typing_reaction(api_client: lark.Client, message_id: str) -> str | None:
    request = (
        CreateMessageReactionRequest.builder()
        .message_id(message_id)
        .request_body(
            CreateMessageReactionRequestBody.builder()
            .reaction_type(Emoji.builder().emoji_type("Typing").build())
            .build()
        )
        .build()
    )
    response = api_client.im.v1.message_reaction.create(request)
    if not response.success():
        lark.logger.warning(
            f"添加敲键盘表情失败: code={response.code}, log_id={response.get_log_id()}"
        )
        return None
    return response.data.reaction_id if response.data else None


def remove_typing_reaction(api_client: lark.Client, message_id: str, reaction_id: str) -> None:
    request = (
        DeleteMessageReactionRequest.builder()
        .message_id(message_id)
        .reaction_id(reaction_id)
        .build()
    )
    response = api_client.im.v1.message_reaction.delete(request)
    if not response.success():
        lark.logger.warning(
            f"移除敲键盘表情失败: code={response.code}, log_id={response.get_log_id()}"
        )


def reply_to_message(api_client: lark.Client, message_id: str, markdown: str) -> bool:
    # 飞书用富文本的 md 元素渲染 Markdown，普通文本消息会显示原始标记。
    content = {"zh_cn": {"content": [[{"tag": "md", "text": markdown}]]}}
    body = (
        ReplyMessageRequestBody.builder()
        .msg_type("post")
        .content(json.dumps(content, ensure_ascii=False))
        .build()
    )
    request = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(body)
        .build()
    )

    response = api_client.im.v1.message.reply(request)
    if not response.success():
        lark.logger.error(
            f"回复失败: code={response.code}, msg={response.msg}, "
            f"log_id={response.get_log_id()}"
        )
        return False
    lark.logger.info("回复成功")
    return True


def extract_message(message_type: str, content: str) -> tuple[str, tuple[str, ...]]:
    try:
        payload = json.loads(content)
    except (ValueError, TypeError):
        return "", ()
    if not isinstance(payload, dict):
        return "", ()
    if message_type == "text":
        value = payload.get("text")
        return (value.strip() if isinstance(value, str) else ""), ()
    if message_type == "image":
        key = payload.get("image_key")
        return "", (key if isinstance(key, str) else "",)
    if message_type != "post":
        return "", ()
    if "content" not in payload:
        payload = next(
            (value for key in ("zh_cn", "en_us", "ja_jp")
             if isinstance(value := payload.get(key), dict)),
            {},
        )
    title = payload.get("title")
    lines = [title] if isinstance(title, str) and title.strip() else []
    image_keys = []
    paragraphs = payload.get("content")
    if not isinstance(paragraphs, list):
        return "\n".join(lines).strip(), ()
    for paragraph in paragraphs:
        if not isinstance(paragraph, list):
            continue
        parts = []
        for node in paragraph:
            if not isinstance(node, dict):
                continue
            if node.get("tag") == "img":
                key = node.get("image_key")
                image_keys.append(key if isinstance(key, str) else "")
                continue
            if node.get("tag") not in {"text", "a", "md", "code_block"}:
                continue
            label = node.get("text")
            label = label if isinstance(label, str) else ""
            href = node.get("href") if node["tag"] == "a" else None
            if isinstance(href, str) and href.strip() and href != label:
                label = f"{label} ({href})" if label else href
            parts.append(label)
        lines.append("".join(parts))
    return "\n".join(lines).strip(), tuple(image_keys)


def extract_message_text(message_type: str, content: str) -> str:
    return extract_message(message_type, content)[0]


def handle_message(api_client: lark.Client, conversations: Conversations, data: P2ImMessageReceiveV1) -> None:
    message = data.event.message
    if message is None or not message.message_id:
        return
    user_text, image_keys = extract_message(message.message_type, message.content)
    if not user_text and not image_keys:
        return

    # 收消息的回调只提交任务，网络请求交给后台线程，长连接可以继续收消息。
    print("收到飞书消息，已交给后台处理", flush=True)
    conversations.submit(conversation_key(data), api_client, message.message_id, user_text, image_keys)


def process_message(
    api_client: lark.Client, message_id: str, user_text: str, history: ConversationHistory = (),
    *, image_keys: tuple[str, ...] = (),
) -> str | None:
    reaction_id = None
    try:
        reaction_id = add_typing_reaction(api_client, message_id)
    except Exception as error:
        lark.logger.warning(f"添加敲键盘表情异常: {type(error).__name__}")

    try:
        return _answer_message(api_client, message_id, user_text, history, image_keys)
    finally:
        if reaction_id:
            try:
                remove_typing_reaction(api_client, message_id, reaction_id)
            except Exception as error:
                lark.logger.warning(f"移除敲键盘表情异常: {type(error).__name__}")


def _answer_message(
    api_client: lark.Client, message_id: str, user_text: str, history: ConversationHistory,
    image_keys: tuple[str, ...] = (),
) -> str | None:
    image_notes = []
    excerpts = []
    for index, key in enumerate(image_keys[:MAX_IMAGES], 1):
        try:
            if not key:
                raise RuntimeError("消息缺少图片资源标识")
            excerpts.append({"image": index, "text": read_image_text(api_client, message_id, key)})
        except RuntimeError as error:
            image_notes.append(f"第 {index} 张截图未能读取（{error}）。")
    if len(image_keys) > MAX_IMAGES:
        image_notes.append(f"单条消息最多读取 {MAX_IMAGES} 张图，其余图片未读取。")
    if image_keys and not excerpts:
        try:
            reply_to_message(api_client, message_id, "\n".join(image_notes) + "\n也可以直接粘贴终端输出。\n\n" + TokenUsage().footer())
        except Exception as error:
            lark.logger.error(f"飞书回复请求异常: {type(error).__name__}")
        return None

    question = user_text
    if excerpts:
        question = (
            (user_text or "请识别并解释截图里与当前排障相关的文字证据。")
            + "\n\n以下是截图 OCR 摘录，仅作证据，可能有误字。\n"
            + json.dumps(excerpts, ensure_ascii=False)
        )
    print("正在请求 DeepSeek", flush=True)
    model_answered = True
    usage = TokenUsage()
    try:
        answer = ask_deepseek(question, history, allow_build=not image_keys, usage=usage)
    except APIStatusError as error:
        model_answered = False
        lark.logger.error(
            "DeepSeek HTTP 请求被拒绝: "
            f"status={error.status_code}, code={error.code}, param={error.param}, "
            f"request_id={error.request_id}, message={error.message}"
        )
        if error.status_code == 400:
            answer = "DeepSeek 拒绝了本次请求（HTTP 400）；管理员可查看服务日志中的接口错误详情。"
        else:
            answer = f"DeepSeek 请求失败（HTTP {error.status_code}），请稍后再试。"
    except Exception as error:
        model_answered = False
        lark.logger.error(f"DeepSeek 调用失败: {type(error).__name__}")
        answer = "模型请求失败或超时，请稍后再试。"

    if excerpts:
        answer = "\n".join([f"已提取 {len(excerpts)}/{len(image_keys)} 张截图的文字。", *image_notes]) + "\n\n" + answer
    print("准备发送飞书回复", flush=True)
    try:
        sent = reply_to_message(api_client, message_id, f"{answer}\n\n{usage.footer()}")
    except Exception as error:
        lark.logger.error(f"飞书回复请求异常: {type(error).__name__}")
        return None
    return answer if sent and model_answered else None


def main() -> None:
    # 飞书凭据与模型、Jenkins 凭据统一从项目根目录的 .env 读取。
    load_project_environment()
    app_id = os.getenv("APP_ID")
    app_secret = os.getenv("APP_SECRET")
    if not app_id or not app_secret:
        raise SystemExit("缺少 APP_ID 或 APP_SECRET，请检查项目根目录的 .env")

    api_client = lark.Client.builder().app_id(app_id).app_secret(app_secret).timeout(10).build()
    workers = ThreadPoolExecutor(max_workers=2)
    conversations = Conversations(workers)
    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(partial(handle_message, api_client, conversations))
        .build()
    )
    ws_client = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )
    print("准备启动飞书长连接...")
    try:
        ws_client.start()
    finally:
        workers.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()

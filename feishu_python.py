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
from openai import APIError, APIStatusError

from app_config import load_project_environment
from deepseek import ConversationHistory, ask_deepseek

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
        self._pending: dict[ConversationKey, deque[tuple[lark.Client, str, str]]] = {}
        self._history: OrderedDict[ConversationKey, tuple[float, ConversationHistory]] = OrderedDict()

    def submit(
        self, key: ConversationKey | None, api_client: lark.Client, message_id: str, user_text: str
    ) -> None:
        if key is None:
            self._workers.submit(process_message, api_client, message_id, user_text)
            return
        with self._lock:
            if key in self._pending:
                self._pending[key].append((api_client, message_id, user_text))
                return
            self._pending[key] = deque([(api_client, message_id, user_text)])
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
                api_client, message_id, user_text = pending.popleft()
                cutoff = monotonic() - HISTORY_IDLE_SECONDS
                while self._history and next(iter(self._history.values()))[0] <= cutoff:
                    self._history.popitem(last=False)
                entry = self._history.get(key)
                history = entry[1] if entry else ()
            try:
                answer = process_message(api_client, message_id, user_text, history)
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


def handle_message(api_client: lark.Client, conversations: Conversations, data: P2ImMessageReceiveV1) -> None:
    message = data.event.message
    if message is None or not message.message_id or message.message_type != "text":
        return

    # 飞书把正文包在 JSON 字符串里，解析后才拿到用户实际输入的文字。
    try:
        user_text = json.loads(message.content)["text"]
    except (ValueError, TypeError, KeyError):
        return
    if not isinstance(user_text, str) or not user_text.strip():
        return

    # 收消息的回调只提交任务，网络请求交给后台线程，长连接可以继续收消息。
    print("收到飞书文本消息，已交给后台处理", flush=True)
    conversations.submit(conversation_key(data), api_client, message.message_id, user_text.strip())


def process_message(
    api_client: lark.Client, message_id: str, user_text: str, history: ConversationHistory = ()
) -> str | None:
    reaction_id = None
    try:
        reaction_id = add_typing_reaction(api_client, message_id)
    except Exception as error:
        lark.logger.warning(f"添加敲键盘表情异常: {type(error).__name__}")

    try:
        return _answer_message(api_client, message_id, user_text, history)
    finally:
        if reaction_id:
            try:
                remove_typing_reaction(api_client, message_id, reaction_id)
            except Exception as error:
                lark.logger.warning(f"移除敲键盘表情异常: {type(error).__name__}")


def _answer_message(
    api_client: lark.Client, message_id: str, user_text: str, history: ConversationHistory
) -> str | None:
    print("正在请求 DeepSeek", flush=True)
    model_answered = True
    try:
        answer = ask_deepseek(user_text, history)
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
    except (APIError, RuntimeError) as error:
        model_answered = False
        lark.logger.error(f"DeepSeek 调用失败: {type(error).__name__}")
        answer = "模型请求失败或超时，请稍后再试。"

    # 原消息 ID 告诉飞书回复哪一条，answer 则是回复的内容。
    print("准备发送飞书回复", flush=True)
    try:
        sent = reply_to_message(api_client, message_id, answer)
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
        log_level=lark.LogLevel.DEBUG,
    )
    print("准备启动飞书长连接...")
    try:
        ws_client.start()
    finally:
        workers.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()

"""HolmesGPT 环境诊断工具的调用封装。"""

import json
import logging
import os

from app_config import load_project_environment

TOOL_NAME = "ask_holmes"
LOGGER = logging.getLogger(__name__)


def run_holmes_tool(arguments: dict[str, str]) -> str:
    """从已校验的工具参数中取出问题，并随诊断结果交回 DeepSeek。"""
    question = arguments["question"].strip()
    try:
        analysis = ask_holmes(question)
    except Exception as error:
        LOGGER.warning("Holmes 查询失败: %s", type(error).__name__)
        return json.dumps(
            {"status": "UNAVAILABLE", "question": question, "message": "Holmes 未取得诊断结果，请检查服务日志。"},
            ensure_ascii=False,
        )
    return json.dumps(
        {"status": "OK", "question": question, "analysis": analysis},
        ensure_ascii=False,
    )


def ask_holmes(question: str) -> str:
    """调用 HolmesGPT 检查运行环境，并返回可供 DeepSeek 归纳的结果。"""
    # DeepSeek 与 Holmes 共用项目根目录 .env 中的 API Key。
    load_project_environment()
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY，请检查项目根目录的 .env")

    try:
        from holmes.config import Config
        from holmes.core.prompt import build_initial_ask_messages
        from holmes.core.tools import ToolsetTag
    except ImportError as error:
        raise RuntimeError(
            "未安装 HolmesGPT，请先运行 pip install -r requirements.txt"
        ) from error

    # Holmes 内部工具只在 DeepSeek 明确选择本工具后才会启动。
    config = Config(
        api_key=api_key,
        model="deepseek/deepseek-flash",
    )
    ai = config.create_toolcalling_llm(
        toolset_tag_filter=[ToolsetTag.CORE, ToolsetTag.CLI],
        enable_all_toolsets_possible=True,
    )
    messages = build_initial_ask_messages(
        initial_user_prompt=question,
        file_paths=None,
        tool_executor=ai.tool_executor,
        system_prompt_additions=None,
    )

    # 返回 Holmes 的诊断正文，由外层对照原问题组织飞书回复。
    response = ai.call(messages)
    if not isinstance(response.result, str) or not response.result.strip():
        raise RuntimeError("Holmes 没有返回有效的诊断结果")
    return response.result

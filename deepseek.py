# Please install OpenAI SDK first: `pip3 install openai`
import json
import os
import time
from dataclasses import dataclass

from openai import OpenAI

from app_config import PROJECT_ROOT, load_project_environment
import jenkins_tools
import prometheus_tools
import tool

MAX_TOOL_ROUNDS = 8
MAX_BUILD_POLL_ATTEMPTS = 120
BUILD_POLL_INTERVAL_SECONDS = 2
ConversationHistory = tuple[tuple[str, str], ...]


@dataclass
class TokenUsage:
    requests: int = 0
    reported: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def record(self, response) -> None:
        usage = getattr(response, "usage", None)
        prompt = getattr(usage, "prompt_tokens", None)
        completion = getattr(usage, "completion_tokens", None)
        total = getattr(usage, "total_tokens", None)
        if not all(type(value) is int and value >= 0 for value in (prompt, completion, total)):
            return
        self.reported += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += total

    def footer(self) -> str:
        if not self.requests:
            return "本条未调用 DeepSeek。"
        if not self.reported:
            return f"本条 DeepSeek Token 用量未返回（请求 {self.requests} 次）。"
        coverage = f"{self.requests} 次请求"
        if self.reported < self.requests:
            coverage = f"仅统计有用量的 {self.reported}/{self.requests} 次请求"
        return (
            f"本条 DeepSeek 用量（{coverage}）：输入 {self.prompt_tokens:,}，"
            f"输出 {self.completion_tokens:,}，合计 {self.total_tokens:,} Token。"
        )


def _request_completion(client: OpenAI, messages: list[dict]):
    """向 DeepSeek 发送一轮对话，并开放当前注册的工具。"""
    return client.chat.completions.create(
        model="deepseek-flash",
        messages=messages,
        tools=tool.tools,
        tool_choice="auto",
        stream=False,
        reasoning_effort="high",
    )


def _assistant_tool_calls_message(message, calls_and_arguments) -> dict:
    """保留同一条 assistant 消息中的全部调用及其推理上下文。"""
    assistant_message = {
        "role": "assistant",
        "content": message.content,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": arguments,
                },
            }
            for call, arguments in calls_and_arguments
        ],
    }
    reasoning_content = getattr(message, "reasoning_content", None)
    if isinstance(reasoning_content, str):
        assistant_message["reasoning_content"] = reasoning_content
    return assistant_message


def _read_tool_result(name: str, arguments: str) -> str:
    try:
        return tool.execute_tool(name, arguments)
    except Exception:
        return json.dumps({"status": "UNAVAILABLE", "message": "工具执行失败，未取得证据。"}, ensure_ascii=False)


def _wait_for_build(job_name: str, build_number: int) -> tuple[str, str | None]:
    """用已注册的状态工具轮询指定构建，直到终态或达到等待上限。"""
    arguments = json.dumps(
        {"job_name": job_name, "build_number": build_number},
        ensure_ascii=False,
    )
    for attempt in range(MAX_BUILD_POLL_ATTEMPTS):
        result_text = tool.execute_tool(jenkins_tools.TOOL_NAME, arguments)
        try:
            result = json.loads(result_text)
        except (TypeError, ValueError):
            return "UNAVAILABLE", None
        if not isinstance(result, dict):
            return "UNAVAILABLE", None

        status = result.get("status")
        if status in {"SUCCESS", "FAILURE"}:
            return status, result_text
        if status not in {"RUNNING", "UNKNOWN"}:
            return str(status or "UNAVAILABLE"), result_text
        if attempt + 1 < MAX_BUILD_POLL_ATTEMPTS:
            time.sleep(BUILD_POLL_INTERVAL_SECONDS)
    return "TIMEOUT", None


def _include_trigger_result(answer: str, triggered_build: tuple[str, int, str | None] | None) -> str:
    """在最终答复前补上本次触发的构建编号和地址。"""
    if triggered_build is None:
        return answer
    job_name, build_number, build_url = triggered_build
    notice = f"已触发「{job_name}」构建 #{build_number}。"
    if build_url:
        notice += f"\n构建地址：{build_url}"
    return f"{notice}\n\n{answer}"


def ask_deepseek(
    user_text: str, history: ConversationHistory = (), *,
    allow_build: bool = True, usage: TokenUsage | None = None,
) -> str:
    # 输入是用户发的正文，输出是模型的回答。这里不需要飞书的消息 ID。
    load_project_environment()
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY，请检查项目根目录的 .env")

    client = OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
        timeout=20.0,
        max_retries=0,
    )

    agent_rules = (PROJECT_ROOT / "agent.md").read_text(encoding="utf-8")
    messages = [
        {"role": "system", "content": agent_rules},
    ]
    for previous_question, previous_answer in history:
        messages.extend([
            {"role": "user", "content": previous_question},
            {"role": "assistant", "content": previous_answer},
        ])
    messages.append({"role": "user", "content": user_text})
    def complete():
        if usage is not None:
            usage.requests += 1
        response = _request_completion(client, messages)
        if usage is not None:
            usage.record(response)
        return response

    response = complete()
    tool_rounds = 0
    prometheus_queries = 0
    triggered_build = None

    while True:
        message = response.choices[0].message
        if not message.tool_calls:
            answer = message.content
            if not answer or not answer.strip():
                raise RuntimeError("DeepSeek 没有返回正文")
            return _include_trigger_result(answer, triggered_build)

        if tool_rounds >= MAX_TOOL_ROUNDS:
            return "工具调用次数已达到上限，无法完成本次操作。"
        if len(message.tool_calls) != 1:
            calls = message.tool_calls
            if any(not call.id for call in calls) or len({call.id for call in calls}) != len(calls):
                return "模型工具调用编号无效，无法对应查询结果。"
            rounds_before_batch = tool_rounds
            messages.append(_assistant_tool_calls_message(
                message, [(call, call.function.arguments) for call in calls]
            ))
            for call in calls:
                name = call.function.name
                if name == jenkins_tools.TRIGGER_TOOL_NAME:
                    allow_build = False
                    result = "此批次只执行查询。如需构建，请用户另发明确请求。"
                elif tool_rounds >= MAX_TOOL_ROUNDS:
                    result = "工具调用次数已达到上限。"
                elif triggered_build is not None and name in {jenkins_tools.TOOL_NAME, jenkins_tools.LOGS_TOOL_NAME}:
                    result = "请单独查询本次触发的构建，以便校验构建编号。"
                elif name == prometheus_tools.TOOL_NAME and prometheus_queries >= prometheus_tools.MAX_QUERIES_PER_REQUEST:
                    result = "本轮 Prometheus 查询次数已用完。"
                else:
                    tool_rounds += 1
                    if name == prometheus_tools.TOOL_NAME:
                        prometheus_queries += 1
                    result = _read_tool_result(name, call.function.arguments)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            tool_rounds = max(tool_rounds, rounds_before_batch + 1)
            response = complete()
            continue

        call = message.tool_calls[0]
        if not call.id:
            return "模型工具调用缺少编号，操作已停止。"
        if call.function.name == jenkins_tools.TRIGGER_TOOL_NAME and not allow_build:
            return "当前请求只执行查询。要触发构建，请单独发送明确的文字请求。"
        if call.function.name == jenkins_tools.TRIGGER_TOOL_NAME and triggered_build is not None:
            return "本次对话已触发过构建，为避免重复启动，停止后续触发请求。"

        tool_arguments = call.function.arguments
        if triggered_build is not None and call.function.name in {
            jenkins_tools.TOOL_NAME,
            jenkins_tools.LOGS_TOOL_NAME,
        }:
            try:
                parsed_arguments = json.loads(tool_arguments)
            except (TypeError, ValueError):
                return "模型返回的查询参数无法读取，无法继续确认本次构建结果。"
            if not isinstance(parsed_arguments, dict):
                return "模型返回的查询参数格式无效，无法继续确认本次构建结果。"
            parsed_arguments["job_name"] = triggered_build[0]
            parsed_arguments["build_number"] = triggered_build[1]
            tool_arguments = json.dumps(parsed_arguments, ensure_ascii=False)

        tool_rounds += 1
        messages.append(_assistant_tool_calls_message(message, [(call, tool_arguments)]))
        if call.function.name == prometheus_tools.TOOL_NAME:
            prometheus_queries += 1
        if call.function.name == prometheus_tools.TOOL_NAME and prometheus_queries > prometheus_tools.MAX_QUERIES_PER_REQUEST:
            tool_result = json.dumps({"status": "LIMIT_REACHED", "message": "本轮 Prometheus 查询次数已用完。"}, ensure_ascii=False)
        elif call.function.name == jenkins_tools.TRIGGER_TOOL_NAME:
            tool_result = tool.execute_tool(call.function.name, tool_arguments)
        else:
            tool_result = _read_tool_result(call.function.name, tool_arguments)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": tool_result,
            }
        )

        if call.function.name == jenkins_tools.LOGS_TOOL_NAME:
            try:
                log_result = json.loads(tool_result)
            except (TypeError, ValueError):
                return "日志查询结果无法读取，无法根据日志确定失败原因。"
            if not isinstance(log_result, dict) or log_result.get("status") != "ready":
                message_text = (
                    log_result.get("message", "没有可供分析的失败构建日志。")
                    if isinstance(log_result, dict)
                    else "没有可供分析的失败构建日志。"
                )
                return _include_trigger_result(
                    f"{message_text}目前无法确定失败原因。",
                    triggered_build,
                )
            log_text = log_result.get("log")
            if not isinstance(log_text, str) or not log_text.strip():
                return _include_trigger_result(
                    "没有取得有效的失败构建日志，无法确定失败原因。",
                    triggered_build,
                )

        if call.function.name == jenkins_tools.TRIGGER_TOOL_NAME:
            try:
                trigger_result = json.loads(tool_result)
            except (TypeError, ValueError):
                return "构建触发结果无法读取，请检查 Jenkins。"
            if not isinstance(trigger_result, dict) or trigger_result.get("status") != "TRIGGERED":
                message_text = (
                    trigger_result.get("message", "未能触发构建。")
                    if isinstance(trigger_result, dict)
                    else "未能触发构建。"
                )
                return message_text

            job_name = trigger_result.get("job_name")
            build_number = trigger_result.get("build_number")
            build_url = trigger_result.get("build_url")
            if not isinstance(job_name, str) or type(build_number) is not int:
                return "Jenkins 已返回触发结果，但缺少有效构建编号，无法查询状态。"
            triggered_build = (job_name, build_number, build_url if isinstance(build_url, str) else None)
            build_status, status_result = _wait_for_build(job_name, build_number)
            if build_status == "SUCCESS":
                return _include_trigger_result(f"「{job_name}」构建成功。", triggered_build)
            if build_status == "FAILURE" and status_result is not None:
                status_call_id = f"build-status-{build_number}"
                status_arguments = json.dumps(
                    {"job_name": job_name, "build_number": build_number},
                    ensure_ascii=False,
                )
                messages.extend(
                    [
                        {
                            "role": "assistant",
                            "content": None,
                            "reasoning_content": "",
                            "tool_calls": [
                                {
                                    "id": status_call_id,
                                    "type": "function",
                                    "function": {
                                        "name": jenkins_tools.TOOL_NAME,
                                        "arguments": status_arguments,
                                    },
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": status_call_id,
                            "content": status_result,
                        },
                    ]
                )
            elif build_status == "TIMEOUT":
                return _include_trigger_result(
                    "等待结果超时；当前无法确定最终状态。",
                    triggered_build,
                )
            else:
                return _include_trigger_result(
                    f"构建状态为 {build_status}，未确认构建成功。",
                    triggered_build,
                )

        response = complete()

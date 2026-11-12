"""模型可调用的 Jenkins 工具层：校验参数与凭据，调用 HTTP 层并整理回复。"""

import json
import os
import re

import requests

from models import (
    GetBuildLogsArguments,
    QueryJenkinsBuildArguments,
    TriggerJenkinsArguments,
)
from trigger_jenkins import (
    JenkinsBuildAcceptedError,
    JenkinsBuildOutcomeUnknownError,
    JenkinsBuildParametersRequiredError,
    get_jenkins_jobs,
    query_jenkins_build,
    query_latest_failed_build_logs,
    trigger_jenkins,
)

TOOL_NAME = "query_jenkins_build"
LOGS_TOOL_NAME = "get_build_logs"
TRIGGER_TOOL_NAME = "trigger_jenkins"
ERROR_PATTERN = re.compile(
    r"\b(error|exception|failed|failure|fatal|traceback|caused by)\b",
    re.IGNORECASE,
)
MAX_LOG_OUTPUT_CHARS = 6000
MAX_LOG_TAIL_LINES = 40


def list_jobs(arguments: dict) -> str:
    """动态列出 Jenkins 中所有可构建任务。"""
    if arguments:
        return json.dumps(
            {"status": "UNAVAILABLE", "message": "任务列表工具不接受参数。"},
            ensure_ascii=False,
        )

    username = os.getenv("JENKINS_USERNAME")
    token = os.getenv("JENKINS_API_TOKEN")
    if not username or not token:
        return json.dumps(
            {"status": "UNAVAILABLE", "message": "还没有配置 Jenkins 查询账号或 API Token。"},
            ensure_ascii=False,
        )

    try:
        jobs = get_jenkins_jobs(username, token)
    except (requests.RequestException, TypeError, ValueError):
        return json.dumps(
            {"status": "UNAVAILABLE", "message": "未能读取 Jenkins 任务列表，请检查 Jenkins 和查询权限。"},
            ensure_ascii=False,
        )
    return json.dumps({"status": "AVAILABLE", "jobs": list(jobs)}, ensure_ascii=False)


def query_build_status(arguments: dict) -> str:
    """校验参数并返回最近一次或指定构建的结构化状态。"""
    # 模型只负责提出参数，任务范围和凭据由 Python 检查。
    if not {"job_name"} <= set(arguments) or set(arguments) - {"job_name", "build_number"}:
        return json.dumps({"status": "UNAVAILABLE", "message": "查询参数无效。"}, ensure_ascii=False)
    job_name = arguments["job_name"]
    if not isinstance(job_name, str) or not job_name.strip():
        return json.dumps(
            {"status": "UNAVAILABLE", "message": "job_name 必须是非空任务全名。"},
            ensure_ascii=False,
        )

    build_number = arguments.get("build_number")
    if build_number is not None and (type(build_number) is not int or build_number < 1):
        return json.dumps({"status": "UNAVAILABLE", "message": "build_number 必须是正整数。"})

    # 凭据只在本地用于 HTTP 认证，不作为模型参数。
    username = os.getenv("JENKINS_USERNAME")
    token = os.getenv("JENKINS_API_TOKEN")
    if not username or not token:
        return json.dumps(
            {"status": "UNAVAILABLE", "message": "还没有配置 Jenkins 查询账号或 API Token。"},
            ensure_ascii=False,
        )

    try:
        build = query_jenkins_build(job_name, username, token, build_number)
        number = build["number"]
        status = "RUNNING" if build["building"] else build["result"] or "UNKNOWN"
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return json.dumps(
            {"status": "UNAVAILABLE", "message": f"未能读取「{job_name}」的构建状态，请检查 Jenkins 和查询权限。"},
            ensure_ascii=False,
        )
    result = {"status": status, "job_name": job_name, "build_number": number}
    if type(build.get("timestamp")) is int:
        result["build_timestamp_unix_ms"] = build["timestamp"]
    if type(build.get("duration")) is int:
        result["duration_ms"] = build["duration"]
    if isinstance(build.get("url"), str) and build["url"]:
        result["build_url"] = build["url"]
    return json.dumps(result, ensure_ascii=False)


def definition() -> dict:
    """生成最近构建状态查询工具的模型侧名称、说明和参数 schema。"""
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "查询 Jenkins 构建状态，或排障时核对已确认关联任务的构建时间与结果。"
                "可传 build_number 查询某次构建；未传时查询最近一次。"
                "job_name 使用 Jenkins 任务全名；不确定任务全名时先调用任务列表工具。"
                "目标不明确时先追问。"
                "失败构建日志请使用专用日志工具。"
            ),
            "parameters": QueryJenkinsBuildArguments.model_json_schema(),
        },
    }


def logs_definition() -> dict:
    """生成失败构建日志查询工具的模型侧名称、说明和参数 schema。"""
    return {
        "type": "function",
        "function": {
            "name": LOGS_TOOL_NAME,
            "description": (
                "用户明确要求查看失败日志，或本次用户要求的构建已失败并需要分析时使用。"
                "可按 build_number 读取指定失败构建；省略时查找最近一次失败构建。"
                "job_name 使用 Jenkins 任务全名；不确定任务全名时先调用任务列表工具。"
                "目标不明确时先追问。"
            ),
            "parameters": GetBuildLogsArguments.model_json_schema(),
        },
    }


def trigger_build(arguments: dict) -> str:
    """仅在用户明确要求时触发 Jenkins 任务，并返回构建编号和地址。"""
    if "job_name" not in arguments or set(arguments) - {"job_name", "parameters"}:
        return json.dumps(
            {
                "status": "UNAVAILABLE",
                "message": "触发参数应包含 job_name，可选 parameters。",
            },
            ensure_ascii=False,
        )
    job_name = arguments["job_name"]
    if not isinstance(job_name, str) or not job_name.strip():
        return json.dumps(
            {"status": "UNAVAILABLE", "message": "job_name 必须是非空任务全名。"},
            ensure_ascii=False,
        )
    parameters = arguments.get("parameters")
    if parameters is not None and not isinstance(parameters, dict):
        return json.dumps(
            {
                "status": "UNAVAILABLE",
                "message": "parameters 必须是参数名到参数值的对象。",
            },
            ensure_ascii=False,
        )

    username = os.getenv("JENKINS_USERNAME")
    token = os.getenv("JENKINS_API_TOKEN")
    if not username or not token:
        return json.dumps(
            {"status": "UNAVAILABLE", "message": "还没有配置 Jenkins 查询账号或 API Token。"},
            ensure_ascii=False,
        )

    try:
        build_number, build_url = trigger_jenkins(
            job_name, username, token, parameters
        )
    except JenkinsBuildAcceptedError as error:
        return json.dumps(
            {
                "status": "QUEUED",
                "job_name": job_name,
                "queue_url": error.queue_url,
                "message": str(error),
            },
            ensure_ascii=False,
        )
    except JenkinsBuildOutcomeUnknownError as error:
        return json.dumps(
            {"status": "OUTCOME_UNKNOWN", "job_name": job_name, "message": str(error)},
            ensure_ascii=False,
        )
    except JenkinsBuildParametersRequiredError as error:
        return json.dumps(
            {
                "status": "PARAMETERS_REQUIRED",
                "job_name": job_name,
                "parameters": error.parameter_names,
                "message": str(error),
            },
            ensure_ascii=False,
        )
    except (requests.RequestException, RuntimeError, TimeoutError, TypeError, ValueError) as error:
        response = getattr(error, "response", None)
        detail = str(error) if response is not None else type(error).__name__
        return json.dumps(
            {
                "status": "UNAVAILABLE",
                "message": f"Jenkins 未确认接收「{job_name}」构建请求：{detail}",
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "status": "TRIGGERED",
            "job_name": job_name,
            "build_number": build_number,
            "build_url": build_url,
        },
        ensure_ascii=False,
    )


def trigger_definition() -> dict:
    """生成 Jenkins 构建触发工具的模型侧定义。"""
    return {
        "type": "function",
        "function": {
            "name": TRIGGER_TOOL_NAME,
            "description": (
                "仅当用户明确要求启动或重新运行 Jenkins 构建时使用；不要仅因用户询问状态而触发。"
                "job_name 使用 Jenkins 任务全名；不确定任务全名时先调用任务列表工具。"
            ),
            "parameters": TriggerJenkinsArguments.model_json_schema(),
        },
    }


def get_build_logs(job_name: str, build_number: int | None = None) -> dict[str, str | int]:
    """读取最近失败构建日志，返回可分析数据或明确的不可用原因。"""
    if not isinstance(job_name, str) or not job_name.strip():
        return {
            "status": "unavailable",
            "message": "job_name 必须是非空任务全名。",
        }

    username = os.getenv("JENKINS_USERNAME")
    token = os.getenv("JENKINS_API_TOKEN")
    if not username or not token:
        return {
            "status": "unavailable",
            "message": "还没有配置 Jenkins 查询账号或 API Token。",
        }

    try:
        build_logs = query_latest_failed_build_logs(job_name, username, token, build_number)
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return {
            "status": "unavailable",
            "message": f"未能读取「{job_name}」的失败构建日志，请检查 Jenkins 和查询权限。",
        }

    if build_logs is None:
        if build_number is not None:
            return {
                "status": "unavailable",
                "message": f"「{job_name}」构建 #{build_number} 不是失败状态，不能按失败日志分析。",
            }
        return {
            "status": "unavailable",
            "message": f"「{job_name}」最近 100 次构建中没有失败记录，无法分析失败原因。",
        }

    build_number, log_text = build_logs
    log_lines = log_text.splitlines()
    error_lines = [line for line in log_lines if ERROR_PATTERN.search(line)]
    if error_lines:
        relevant_log = "\n".join(error_lines)
    else:
        relevant_log = "\n".join(log_lines[-MAX_LOG_TAIL_LINES:])

    if not relevant_log.strip():
        return {
            "status": "unavailable",
            "message": f"「{job_name}」最近失败构建 #{build_number} 的日志为空，无法确定失败原因。",
        }

    if len(relevant_log) > MAX_LOG_OUTPUT_CHARS:
        relevant_log = "...\n" + relevant_log[-(MAX_LOG_OUTPUT_CHARS - 4):]
    return {
        "status": "ready",
        "job_name": job_name,
        "build_number": build_number,
        "log": relevant_log,
    }


def query_build_logs(arguments: dict) -> str:
    """校验日志查询参数，并以 JSON 状态对象返回日志或不可用原因。"""
    if not {"job_name"} <= set(arguments) or set(arguments) - {"job_name", "build_number"}:
        result = {
            "status": "unavailable",
            "message": "查询参数应包含 job_name，且只允许附加 build_number。",
        }
    else:
        build_number = arguments.get("build_number")
        if build_number is not None and (type(build_number) is not int or build_number < 1):
            result = {"status": "unavailable", "message": "build_number 必须是正整数。"}
        else:
            result = get_build_logs(arguments["job_name"], build_number)
    return json.dumps(result, ensure_ascii=False)

"""工具注册入口，把模型看到的名字对应到本地执行方法。"""

import json

import holmes_ask
import feishu_knowledge
import jenkins_tools
import prometheus_tools
from app_config import load_project_environment
from models import (
    GetBuildLogsArguments,
    FeishuDocumentReadArguments,
    FeishuKnowledgeSearchArguments,
    HolmesAskArguments,
    ListJenkinsJobsArguments,
    PrometheusMetricArguments,
    QueryJenkinsBuildArguments,
    TriggerJenkinsArguments,
)
from pydantic import ValidationError

# 工具说明和执行方法在同一条注册记录里，避免只注册名字而没有真正调用。
TOOL_REGISTRY = {
    prometheus_tools.TOOL_NAME: {
        "definition": {
            "type": "function",
            "function": {
                "name": prometheus_tools.TOOL_NAME,
                "description": (
                    "为目标指标生成 PromQL，查询已配置环境的指标趋势。"
                    "服务查询尽量用已确认的命名空间、job 或 service 标签限定范围。"
                    "可重试的语法错误或空结果应先修正表达式，再做一次有针对性的查询。"
                    "连续查询多个指标时沿用第一个结果的 end_unix_seconds。"
                ),
                "parameters": PrometheusMetricArguments.model_json_schema(),
            },
        },
        "handler": prometheus_tools.query_prometheus_metric,
        "arguments_model": PrometheusMetricArguments,
    },
    "list_jenkins_jobs": {
        "definition": {
            "type": "function",
            "function": {
                "name": "list_jenkins_jobs",
                "description": "动态获取 Jenkins 中当前所有可构建任务的全名。",
                "parameters": ListJenkinsJobsArguments.model_json_schema(),
            },
        },
        "handler": jenkins_tools.list_jobs,
        "arguments_model": ListJenkinsJobsArguments,
    },
    jenkins_tools.TOOL_NAME: {
        "definition": jenkins_tools.definition(),
        "handler": jenkins_tools.query_build_status,
        "arguments_model": QueryJenkinsBuildArguments,
    },
    jenkins_tools.LOGS_TOOL_NAME: {
        "definition": jenkins_tools.logs_definition(),
        "handler": jenkins_tools.query_build_logs,
        "arguments_model": GetBuildLogsArguments,
    },
    jenkins_tools.TRIGGER_TOOL_NAME: {
        "definition": jenkins_tools.trigger_definition(),
        "handler": jenkins_tools.trigger_build,
        "arguments_model": TriggerJenkinsArguments,
    },
    holmes_ask.TOOL_NAME: {
        "definition": {
            "type": "function",
            "function": {
                "name": holmes_ask.TOOL_NAME,
                "description": (
                    "仅当用户需要检查当前 Kubernetes 集群、服务或运行环境的实时状态、"
                    "事件、日志或故障原因时使用。将用户要调查的问题作为 question，"
                    "保留原问题中的目标和查询范围，缺少关键目标时先追问；"
                    "不用于一般知识问答或 Jenkins 构建查询。"
                ),
                "parameters": HolmesAskArguments.model_json_schema(),
            },
        },
        "handler": holmes_ask.run_holmes_tool,
        "arguments_model": HolmesAskArguments,
    },
    feishu_knowledge.TOOL_NAME: {
        "definition": {
            "type": "function",
            "function": {
                "name": feishu_knowledge.TOOL_NAME,
                "description": (
                    "当用户询问企业制度、操作流程、内部约定或历史记录时，"
                    "按相关性搜索飞书云文档的正文片段。query 填具体主题或关键词，"
                    "已知准确文档标题时用 title 限定范围。需要总结整篇时改用 read_feishu_document；"
                    "不要用于实时集群状态或 Jenkins 构建查询。"
                ),
                "parameters": FeishuKnowledgeSearchArguments.model_json_schema(),
            },
        },
        "handler": feishu_knowledge.search_feishu_knowledge,
        "arguments_model": FeishuKnowledgeSearchArguments,
    },
    feishu_knowledge.READ_TOOL_NAME: {
        "definition": {
            "type": "function",
            "function": {
                "name": feishu_knowledge.READ_TOOL_NAME,
                "description": (
                    "总结整篇飞书文档、列出全部项目或完整核对时使用。按准确标题从 offset=0 读正文。"
                    "若 next_offset 非空，继续读取同一篇文档直至 next_offset 为 null。"
                    "遇到多个同名候选时，按已确认的原文链接选择或询问用户。"
                ),
                "parameters": FeishuDocumentReadArguments.model_json_schema(),
            },
        },
        "handler": feishu_knowledge.read_feishu_document,
        "arguments_model": FeishuDocumentReadArguments,
    },
}

tools = [entry["definition"] for entry in TOOL_REGISTRY.values()]


def execute_tool(name: str, raw_arguments: str) -> str:
    # 所有工具共用项目根目录的 .env 配置。
    load_project_environment()
    entry = TOOL_REGISTRY.get(name)
    if entry is None:
        return "请求的工具尚未开放。"
    try:
        arguments = json.loads(raw_arguments)
    except (TypeError, ValueError):
        return "没有读懂工具参数，请重新说明查询目标。"
    if not isinstance(arguments, dict):
        return "工具参数需要是一个对象，请重新说明查询目标。"

    try:
        validated_arguments = entry["arguments_model"].model_validate(
            arguments,
            strict=True,
        )
    except ValidationError as error:
        details = []
        for issue in error.errors(include_input=False):
            path = ".".join(str(part) for part in issue["loc"]) or "参数"
            details.append(f"{path}: {issue['msg']}")
        return json.dumps(
            {
                "status": "INVALID_ARGUMENTS",
                "message": "工具参数校验失败：" + "；".join(details),
            },
            ensure_ascii=False,
        )

    # 这里只负责分发；handler 只会收到通过对应参数模型校验的数据。
    return entry["handler"](validated_arguments.model_dump(exclude_unset=True))

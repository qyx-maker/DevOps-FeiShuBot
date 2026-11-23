"""Pydantic 参数模型，用于生成 Jenkins 工具 schema 并校验模型输出。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PublicWebSearchArguments(StrictToolArguments):
    query: str = Field(min_length=1, max_length=400, description="仅包含公开信息的精简搜索词，可指定 site: 或 lang:zh。")
    freshness: Literal["day", "week", "month", "year"] | None = Field(
        default=None, description="仅当用户要求近期消息时设置。没有时间要求就省略。",
    )

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value.split()) > 75:
            raise ValueError("搜索词不能为空或超过 75 个词")
        return value


class PrometheusMetricArguments(StrictToolArguments):
    environment: str = Field(min_length=1, max_length=80, description="已确认的目标环境，与 Prometheus 配置对应")
    promql: str = Field(
        min_length=1, max_length=2000, description="为当前问题编写的 PromQL，优先用已知标签限定范围",
    )
    window_minutes: int = Field(default=30, ge=5, le=120, description="查询窗口，单位分钟")
    end_unix_seconds: int | None = Field(
        default=None, ge=0, le=253402300799,
        description="窗口结束时间，Unix 秒。首次通常省略；对齐多个指标时沿用上次返回值",
    )

    @field_validator("promql")
    @classmethod
    def promql_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("promql 不能为空")
        return value.strip()


class ListJenkinsJobsArguments(StrictToolArguments):
    """列出可构建任务时不接受任何参数。"""


class JenkinsJobArguments(StrictToolArguments):
    job_name: str = Field(
        description="Jenkins 任务全名；嵌套任务用 / 分隔文件夹名",
        min_length=1,
    )

    @field_validator("job_name")
    @classmethod
    def job_name_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("job_name 必须是非空任务全名")
        return value


class QueryJenkinsBuildArguments(JenkinsJobArguments):
    build_number: int | None = Field(
        default=None,
        ge=1,
        description="可选的构建编号；省略时查询最近一次构建",
    )


class GetBuildLogsArguments(JenkinsJobArguments):
    build_number: int | None = Field(
        default=None,
        ge=1,
        description="可选的失败构建编号；省略时查询最近失败构建",
    )


class TriggerJenkinsArguments(JenkinsJobArguments):
    parameters: dict[str, str] | None = Field(
        default=None,
        description="可选 Jenkins 参数名和值；未提供时使用 Jenkins 配置的默认值",
    )


class HolmesAskArguments(StrictToolArguments):
    """Holmes 环境诊断工具的输入参数。"""

    question: str = Field(
        description="需要 Holmes 检查的集群、服务或运行环境问题",
        min_length=1,
    )
    namespace: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
        description="目标命名空间。未知时省略，先用 overview 定位。",
    )
    service: str | None = Field(
        default=None, pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
        description="已知的 Service 名。查询访问异常时尽量提供。",
    )
    host: str | None = Field(
        default=None, min_length=1, max_length=253,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9.-]*$",
        description="URL 中的主机名，不含协议、端口或路径。只用于匹配集群路由，不向该主机发请求。",
    )
    mode: Literal["overview", "network", "workload", "pod", "logs", "dependencies"] | None = Field(
        default=None,
        description="不明确先用 overview；访问故障用 network；发布查 workload；单 Pod 用 pod；需要日志用 logs；存储/网络策略用 dependencies。",
    )
    pod: str | None = Field(default=None, max_length=253, pattern=r"^[a-z0-9][a-z0-9.-]*$")
    container: str | None = Field(default=None, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$")
    previous: bool = False

    @model_validator(mode="after")
    def validate_diagnostic_scope(self):
        mode = self.mode or ("network" if self.namespace else "overview")
        if mode != "overview" and not self.namespace:
            raise ValueError("定向诊断需要 namespace")
        if mode in {"pod", "logs"} and not self.pod:
            raise ValueError("Pod 详情和日志查询需要 pod")
        if mode == "logs" and not self.container:
            raise ValueError("日志查询需要已确认的 container")
        if mode != "logs" and (self.container or self.previous):
            raise ValueError("container 和 previous 仅用于 logs")
        return self

    @field_validator("question")
    @classmethod
    def question_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question 不能为空")
        return value


class FeishuKnowledgeSearchArguments(StrictToolArguments):
    """飞书本地知识库检索工具的输入参数。"""

    query: str = Field(
        description="要从已同步的飞书云文档中检索的关键词或问题",
        min_length=2,
        max_length=500,
    )
    title: str | None = Field(default=None, min_length=1, max_length=500, description="已知准确标题时，只在该文档中检索")

    @field_validator("query")
    @classmethod
    def query_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query 不能为空")
        return value


class FeishuDocumentReadArguments(StrictToolArguments):
    title: str = Field(min_length=1, max_length=500, description="要通读的飞书文档准确标题")
    url: str | None = Field(default=None, max_length=2048, description="同名文档时使用已确认的原文链接")
    offset: int = Field(default=0, ge=0, description="首读为 0，续读使用上次返回的 next_offset")

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title 不能为空")
        return value.strip()

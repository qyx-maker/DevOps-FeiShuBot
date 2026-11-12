"""从配置好的 Prometheus 执行模型提出的只读 PromQL 区间查询。"""

import json
import math
import os
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

import requests

TOOL_NAME = "query_prometheus_metric"
STEP_SECONDS = 30
MAX_SERIES = 8
MAX_QUERIES_PER_REQUEST = 4
RESULT_LABELS = ("__name__", "cluster", "environment", "namespace", "job", "service",
                 "deployment", "pod", "container", "instance", "status", "code", "le")


def _utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


def _summarize(series: dict) -> dict:
    points = []
    samples = series.get("values", [])
    for timestamp, value in samples:
        number = float(value)
        if math.isfinite(number):
            points.append((timestamp, number))
    summary = {
        "labels": {key: series["metric"][key] for key in RESULT_LABELS if key in series["metric"]},
        "sample_count": len(samples),
        "finite_count": len(points),
    }
    if points:
        high = max(points, key=lambda point: point[1])
        low = min(points, key=lambda point: point[1])
        for label, (timestamp, value) in (
            ("first", points[0]), ("last", points[-1]), ("min", low), ("max", high)
        ):
            summary[label] = {"at_utc": _utc(timestamp), "value": value}
        if any(value == 0 for _, value in points):
            summary["zero_sample_count"] = sum(value == 0 for _, value in points)
    return summary


def query_prometheus_metric(arguments: dict) -> str:
    def output(status: str, **data) -> str:
        return json.dumps({"status": status, **data}, ensure_ascii=False, allow_nan=False)

    base_url = os.getenv("PROMETHEUS_URL", "").strip().rstrip("/")
    environment = os.getenv("PROMETHEUS_ENVIRONMENT", "").strip()
    if not base_url or not environment:
        return output("UNAVAILABLE", message="尚未配置 Prometheus 地址或环境。")
    if arguments["environment"] != environment:
        return output(
            "UNAVAILABLE", configured_environment=environment,
            message="目标环境与已配置的 Prometheus 不一致，请确认目标环境。",
        )
    try:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
            or parsed.query or parsed.fragment
        ):
            return output("UNAVAILABLE", message="Prometheus 地址配置无效。")
    except ValueError:
        return output("UNAVAILABLE", message="Prometheus 地址配置无效。")

    now = int(time.time())
    end = arguments.get("end_unix_seconds")
    if end is None:
        end = now - now % STEP_SECONDS
    start = end - arguments.get("window_minutes", 30) * 60
    if end > now or start < 0:
        return output("INVALID_ARGUMENTS", message="查询窗口不能早于 Unix 纪元或晚于当前时间。")
    query = arguments["promql"]
    context = {
        "environment": environment,
        "promql": query,
        "start_utc": _utc(start),
        "end_utc": _utc(end),
        "end_unix_seconds": end,
        "step_seconds": STEP_SECONDS,
    }
    token = os.getenv("PROMETHEUS_BEARER_TOKEN", "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        response = requests.get(
            f"{base_url}/api/v1/query_range",
            params={"query": query, "start": start, "end": end, "step": STEP_SECONDS,
                    "timeout": "8s", "limit": MAX_SERIES + 1},
            headers=headers,
            timeout=(3, 10),
            allow_redirects=False,
        )
        if response.status_code in (400, 422):
            try:
                error = response.json()
            except ValueError:
                error = {}
            if isinstance(error, dict) and error.get("errorType") in {"bad_data", "execution"}:
                return output(
                    "QUERY_ERROR", **context, http_status=response.status_code, retryable=True,
                    error_type=error["errorType"], message=str(error.get("error") or "查询无法执行")[:500],
                )
        if response.status_code != 200:
            return output("UNAVAILABLE", **context, http_status=response.status_code)
        payload = response.json()
        if payload["status"] != "success" or payload["data"]["resultType"] != "matrix":
            return output("UNAVAILABLE", **context, message="Prometheus 未返回区间查询结果。")
        result = payload["data"]["result"]
        series = [_summarize(item) for item in result[:MAX_SERIES]]
    except (requests.RequestException, KeyError, ValueError, TypeError, OverflowError, OSError):
        return output("UNAVAILABLE", **context, message="Prometheus 请求失败或返回格式无效。")

    if not any(item["finite_count"] for item in series):
        return output(
            "NO_DATA", **context, retryable=True, series_truncated=len(result) > MAX_SERIES,
            message="返回的序列没有有效数值样本；请核对指标名、标签和采集状态。直方图需先计算分位数等数值。",
        )
    return output(
        "OK", **context, series=series, series_truncated=len(result) > MAX_SERIES,
        has_query_warnings=bool(payload.get("warnings")),
    )

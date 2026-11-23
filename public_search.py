"""通过 Tavily Search API 检索公开网页。"""

from html import unescape
import json
import os
from urllib.parse import urlsplit

import requests

TOOL_NAME = "search_public_web"
SEARCH_URL = "https://api.tavily.com/search"
MAX_RESULTS = 5


def _result(row: dict) -> dict | None:
    url = row.get("url")
    if not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username is not None:
            return None
    except ValueError:
        return None
    title = row.get("title")
    snippet = row.get("content")
    date = row.get("published_date")
    return {
        "title": unescape(title)[:240] if isinstance(title, str) else parts.hostname,
        "url": url,
        "snippet": unescape(snippet)[:700] if isinstance(snippet, str) else "",
        "published_date": date[:40] if isinstance(date, str) else None,
    }


def search_public_web(arguments: dict) -> str:
    key = os.getenv("TAVILY_API_KEY", "").strip()
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    else:
        headers["X-Tavily-Access-Mode"] = "keyless"
    params = {
        "query": arguments["query"],
        "max_results": MAX_RESULTS,
        "search_depth": "basic",
        "include_answer": False,
        "include_raw_content": False,
    }
    freshness = arguments.get("freshness")
    if freshness:
        params.update(time_range=freshness, include_published_date=True)
    try:
        response = requests.post(
            SEARCH_URL, json=params, headers=headers,
            timeout=(3, 8), allow_redirects=False,
        )
        if response.status_code in (401, 403):
            return json.dumps({
                "status": "AUTH_ERROR" if key else "NOT_CONFIGURED",
                "http_status": response.status_code,
                "message": (
                    "Tavily 拒绝了当前 API Key，请检查 TAVILY_API_KEY，更新后重启机器人。"
                    if key else "Tavily 无密钥搜索不可用，请配置 TAVILY_API_KEY 后重启机器人。"
                ),
            }, ensure_ascii=False)
        if response.status_code in (429, 432, 433):
            message = (
                "Tavily 已达到搜索频率或额度限制，本轮停止重试。"
                if key else "Tavily 无密钥搜索已达频率限制。可配置 TAVILY_API_KEY 使用账号额度。"
            )
            retry_after = response.headers.get("Retry-After", "").strip()
            return json.dumps({
                "status": "LIMIT_REACHED", "http_status": response.status_code,
                "auth_mode": "api_key" if key else "keyless",
                "retry_after_seconds": int(retry_after) if retry_after.isascii() and retry_after.isdecimal() and len(retry_after) <= 8 else None,
                "message": message,
            }, ensure_ascii=False)
        if response.status_code != 200:
            return json.dumps({
                "status": "UNAVAILABLE", "http_status": response.status_code,
                "message": "Tavily 搜索服务拒绝或未完成请求。",
            }, ensure_ascii=False)
        payload = response.json()
        rows = payload["results"]
        if not isinstance(rows, list):
            raise ValueError("Invalid results")
        results = [
            item for row in rows[:MAX_RESULTS]
            if isinstance(row, dict) and (item := _result(row)) is not None
        ]
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return json.dumps({"status": "UNAVAILABLE", "message": "公开搜索服务连接失败或结果无法解析。"}, ensure_ascii=False)

    return json.dumps({
        "status": "OK" if results else "NO_RESULTS",
        "source": "Tavily Search",
        "query": arguments["query"],
        "results": results,
    }, ensure_ascii=False)

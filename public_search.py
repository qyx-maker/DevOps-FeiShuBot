"""通过 Brave Search API 检索公开网页。"""

from html import unescape
import json
import os
from urllib.parse import urlsplit

import requests

TOOL_NAME = "search_public_web"
SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
MAX_RESULTS = 5
FRESHNESS = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}


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
    snippet = row.get("description")
    date = row.get("page_age")
    return {
        "title": unescape(title)[:240] if isinstance(title, str) else parts.hostname,
        "url": url,
        "snippet": unescape(snippet)[:700] if isinstance(snippet, str) else "",
        "indexed_page_date": date[:40] if isinstance(date, str) else None,
    }


def search_public_web(arguments: dict) -> str:
    key = os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
    if not key:
        return json.dumps({"status": "NOT_CONFIGURED", "message": "公开网页搜索尚未配置 BRAVE_SEARCH_API_KEY。"}, ensure_ascii=False)

    params = {"q": arguments["query"], "count": MAX_RESULTS, "text_decorations": "false"}
    freshness = FRESHNESS.get(arguments.get("freshness"))
    if freshness:
        params["freshness"] = freshness
    try:
        response = requests.get(
            SEARCH_URL, params=params,
            headers={"Accept": "application/json", "X-Subscription-Token": key},
            timeout=(3, 8), allow_redirects=False,
        )
        if response.status_code != 200:
            return json.dumps({
                "status": "UNAVAILABLE",
                "http_status": response.status_code,
                "message": "公开搜索服务拒绝或未完成请求。",
            }, ensure_ascii=False)
        payload = response.json()
        rows = payload.get("web", {}).get("results", [])
        results = [
            item for row in rows[:MAX_RESULTS]
            if isinstance(row, dict) and (item := _result(row)) is not None
        ]
    except (requests.RequestException, ValueError, AttributeError, TypeError):
        return json.dumps({"status": "UNAVAILABLE", "message": "公开搜索服务连接失败或结果无法解析。"}, ensure_ascii=False)

    return json.dumps({
        "status": "OK" if results else "NO_RESULTS",
        "source": "Brave Search",
        "query": arguments["query"],
        "results": results,
    }, ensure_ascii=False)

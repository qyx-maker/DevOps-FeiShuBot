"""Feishu Drive 文档同步与本地 SQLite 全文检索。"""

import json
import os
import re
import sqlite3
import time
from threading import Lock
from typing import Any

import lark_oapi as lark
from lark_oapi.api.docx.v1 import RawContentDocumentRequest
from lark_oapi.api.drive.v1 import ListFileRequest
import requests

from app_config import PROJECT_ROOT, load_project_environment

TOOL_NAME = "search_feishu_knowledge"
READ_TOOL_NAME = "read_feishu_document"
DATABASE_PATH = PROJECT_ROOT / "feishu_knowledge.sqlite3"
SYNC_INTERVAL_SECONDS = 15 * 60
MAX_SEARCH_RESULTS = 5
CHUNK_CHARS = 1000
CHUNK_OVERLAP = 120
DOCUMENT_PAGE_CHARS = 18000
CHUNK_INDEX_VERSION = "1"
_SYNC_LOCK = Lock()

_CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_WORD_PATTERN = re.compile(r"[a-zA-Z0-9_]{3,}")


def _connect() -> sqlite3.Connection:
    """打开本地索引，并确保全文检索表已初始化。"""
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_documents (
            id INTEGER PRIMARY KEY,
            token TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            folder_path TEXT NOT NULL,
            url TEXT NOT NULL,
            content TEXT NOT NULL,
            modified_time INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_sync_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks
        USING fts5(document_id UNINDEXED, chunk_no UNINDEXED, title, content, tokenize='trigram')
        """
    )
    return connection


def _split_content(content: str):
    """切成带少量重叠的片段，尽量在换行处结束。"""
    start = 0
    while start < len(content):
        end = min(start + CHUNK_CHARS, len(content))
        if end < len(content):
            boundary = content.rfind("\n", end - 250, end)
            if boundary >= 0:
                end = boundary + 1
        yield content[start:end]
        if end == len(content):
            break
        start = end - CHUNK_OVERLAP


def _rebuild_chunk_index(connection: sqlite3.Connection) -> None:
    """用本地正文重建片段索引，与同步事务一起提交。"""
    connection.execute("DELETE FROM knowledge_chunks")
    documents = connection.execute("SELECT id, title, content FROM knowledge_documents")
    connection.executemany(
        "INSERT INTO knowledge_chunks (document_id, chunk_no, title, content) VALUES (?, ?, ?, ?)",
        (
            (row["id"], number, row["title"], chunk)
            for row in documents
            for number, chunk in enumerate(_split_content(row["content"]), 1)
            if chunk.strip()
        ),
    )
    connection.execute("DROP TABLE IF EXISTS knowledge_search")
    connection.execute(
        "INSERT OR REPLACE INTO knowledge_sync_state (key, value) VALUES ('chunk_index_version', ?)",
        (CHUNK_INDEX_VERSION,),
    )


def _get_feishu_client() -> lark.Client:
    """构造使用应用身份访问飞书的客户端。"""
    app_id = os.getenv("APP_ID", "").strip()
    app_secret = os.getenv("APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise RuntimeError("缺少 APP_ID 或 APP_SECRET，请检查项目根目录的 .env")
    return (
        lark.Client.builder()
        .app_id(app_id)
        .app_secret(app_secret)
        .timeout(20)
        .build()
    )


def _list_folder_files(
    client: lark.Client,
    folder_token: str,
) -> list[Any]:
    """读取指定文件夹的全部分页文件列表。"""
    files = []
    page_token = None
    while True:
        builder = (
            ListFileRequest.builder()
            .folder_token(folder_token)
            .page_size(200)
        )
        if page_token:
            builder.page_token(page_token)
        response = client.drive.v1.file.list(builder.build())
        if not response.success():
            raise RuntimeError(
                f"读取飞书文件夹失败（code={response.code}, "
                f"log_id={response.get_log_id()}）"
            )
        body = response.data
        if body is None:
            raise RuntimeError("飞书文件列表返回为空")
        files.extend(body.files or [])
        if not body.has_more:
            return files
        if not body.next_page_token or body.next_page_token == page_token:
            raise RuntimeError("飞书文件列表分页状态无效，已停止同步")
        page_token = body.next_page_token


def _collect_docx_files(
    client: lark.Client,
    root_folder_token: str,
) -> tuple[list[tuple[str, str, str, str, int, str]], int]:
    """递归收集指定文件夹树下的 Docx 文档。"""
    pending_folders = [(root_folder_token, "")]
    visited_folders = set()
    documents = []
    skipped_file_count = 0

    while pending_folders:
        folder_token, folder_path = pending_folders.pop()
        if folder_token in visited_folders:
            continue
        visited_folders.add(folder_token)

        for item in _list_folder_files(client, folder_token):
            token = item.token
            name = item.name
            file_type = item.type
            if not token or not name or not file_type:
                skipped_file_count += 1
                continue
            if file_type == "folder":
                nested_path = f"{folder_path}/{name}".strip("/")
                pending_folders.append((token, nested_path))
                continue
            if file_type != "docx":
                skipped_file_count += 1
                continue

            response = client.docx.v1.document.raw_content(
                RawContentDocumentRequest.builder()
                .document_id(token)
                .build()
            )
            if not response.success():
                raise RuntimeError(
                    f"读取飞书文档「{name}」失败（code={response.code}, "
                    f"log_id={response.get_log_id()}）"
                )
            body = response.data
            content = body.content if body is not None else None
            if not isinstance(content, str):
                raise RuntimeError(f"飞书文档「{name}」没有返回文本内容")
            documents.append(
                (
                    token,
                    name,
                    folder_path,
                    item.url or "",
                    int(item.modified_time or 0),
                    content,
                )
            )

    return documents, skipped_file_count


def sync_feishu_knowledge(force: bool = False) -> dict[str, int | str]:
    """将配置的飞书文件夹内 Docx 文档同步到本地索引。"""
    load_project_environment()
    folder_token = os.getenv("FEISHU_KNOWLEDGE_FOLDER_TOKEN", "").strip()
    if not folder_token:
        raise RuntimeError(
            "缺少 FEISHU_KNOWLEDGE_FOLDER_TOKEN，请在 .env 设置知识库文件夹 token"
        )

    with _SYNC_LOCK:
        connection = _connect()
        try:
            state = dict(
                connection.execute(
                    "SELECT key, value FROM knowledge_sync_state"
                ).fetchall()
            )
            last_synced = float(state.get("last_synced", "0"))
            if (
                not force
                and state.get("folder_token") == folder_token
                and time.time() - last_synced < SYNC_INTERVAL_SECONDS
            ):
                if state.get("chunk_index_version") != CHUNK_INDEX_VERSION:
                    with connection:
                        _rebuild_chunk_index(connection)
                count = connection.execute(
                    "SELECT COUNT(*) FROM knowledge_documents"
                ).fetchone()[0]
                return {"documents_indexed": count, "status": "cached"}

            client = _get_feishu_client()
            documents, skipped_file_count = _collect_docx_files(
                client,
                folder_token,
            )

            # 只在完整读取成功后替换索引，避免同步中断时丢失已有知识。
            with connection:
                connection.execute("DELETE FROM knowledge_documents")
                connection.executemany(
                    """
                    INSERT INTO knowledge_documents
                        (token, title, folder_path, url, modified_time, content)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    documents,
                )
                _rebuild_chunk_index(connection)
                connection.execute(
                    """
                    INSERT INTO knowledge_sync_state (key, value)
                    VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    ("folder_token", folder_token),
                )
                connection.execute(
                    """
                    INSERT INTO knowledge_sync_state (key, value)
                    VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    ("last_synced", str(time.time())),
                )
        finally:
            connection.close()

    return {
        "documents_indexed": len(documents),
        "skipped_files": skipped_file_count,
        "status": "synced",
    }


def _search_terms(query: str) -> list[str]:
    """生成 FTS5 三元组检索词，兼顾中英文问题。"""
    terms = set(_WORD_PATTERN.findall(query.lower()))
    for segment in _CJK_PATTERN.findall(query):
        if len(segment) >= 3:
            terms.update(segment[index : index + 3] for index in range(len(segment) - 2))
    return sorted(terms)


def _find_chunks(query: str, title: str | None = None) -> list[sqlite3.Row]:
    """按片段而不是整篇文档排名；正文命中权重高于标题。"""
    connection = _connect()
    select = """
        SELECT d.title, d.folder_path, d.url, c.chunk_no, c.content
        FROM knowledge_chunks AS c
        JOIN knowledge_documents AS d ON d.id = c.document_id
    """
    try:
        terms = _search_terms(query)
        if terms:
            match_query = " OR ".join(f'"{term}"' for term in terms)
            rows = connection.execute(
                select + """
                WHERE knowledge_chunks MATCH ? AND (? IS NULL OR d.title = ?)
                ORDER BY bm25(knowledge_chunks, 0, 0, 0.1, 1), d.id, c.chunk_no
                LIMIT ?
                """,
                (match_query, title, title, MAX_SEARCH_RESULTS),
            ).fetchall()
            if rows:
                return rows

        # 三元组不支持两个字的词；按字面包含兜底，正文命中优先。
        return connection.execute(
            select + """
            WHERE (? IS NULL OR d.title = ?)
                AND (instr(lower(c.content), lower(?)) > 0 OR instr(lower(d.title), lower(?)) > 0)
            ORDER BY instr(lower(c.content), lower(?)) = 0, d.id, c.chunk_no
            LIMIT ?
            """,
            (title, title, query, query, query, MAX_SEARCH_RESULTS),
        ).fetchall()
    finally:
        connection.close()


def search_feishu_knowledge(arguments: dict[str, str]) -> str:
    """刷新过期索引并返回飞书文档中的本地检索片段。"""
    query = arguments["query"].strip()
    title = arguments.get("title")
    try:
        sync_result = sync_feishu_knowledge()
        chunks = _find_chunks(query, title.strip() if title else None)
    except (RuntimeError, sqlite3.Error, requests.RequestException) as error:
        return json.dumps(
            {"status": "UNAVAILABLE", "message": str(error)},
            ensure_ascii=False,
        )

    if not chunks:
        return json.dumps(
            {
                "status": "NO_RESULTS",
                "message": "已检索本地飞书知识库，没有找到相关文档。",
                "documents_indexed": sync_result["documents_indexed"],
            },
            ensure_ascii=False,
        )

    results = [
        {
            "title": row["title"],
            "folder": row["folder_path"],
            "url": row["url"],
            "chunk_number": row["chunk_no"],
            "excerpt": row["content"],
        }
        for row in chunks
    ]
    return json.dumps(
        {
            "status": "OK",
            "coverage": "matched_chunks_only",
            "documents_indexed": sync_result["documents_indexed"],
            "sync_status": sync_result["status"],
            "skipped_files": sync_result.get("skipped_files", 0),
            "results": results,
        },
        ensure_ascii=False,
    )


def read_feishu_document(arguments: dict[str, str | int]) -> str:
    """按标题定位原文，按顺序读取有限长度的正文页。"""
    title = str(arguments["title"]).strip()
    url = arguments.get("url")
    offset = int(arguments.get("offset", 0))
    try:
        sync_feishu_knowledge()
        connection = _connect()
        try:
            rows = connection.execute(
                """
                SELECT title, folder_path, url, length(content) AS total_chars,
                       substr(content, ? + 1, ?) AS page
                FROM knowledge_documents
                WHERE title = ? AND (? IS NULL OR url = ?)
                ORDER BY id
                """,
                (offset, DOCUMENT_PAGE_CHARS, title, url, url),
            ).fetchall()
        finally:
            connection.close()
    except (RuntimeError, sqlite3.Error, requests.RequestException) as error:
        return json.dumps({"status": "UNAVAILABLE", "message": str(error)}, ensure_ascii=False)

    if not rows:
        return json.dumps({"status": "NO_RESULTS", "message": "未找到指定文档，可先用知识检索确认标题。"}, ensure_ascii=False)
    if len(rows) > 1:
        return json.dumps(
            {
                "status": "AMBIGUOUS",
                "candidates": [
                    {"title": row["title"], "folder": row["folder_path"], "url": row["url"]}
                    for row in rows
                ],
            },
            ensure_ascii=False,
        )
    row = rows[0]
    total_chars = row["total_chars"]
    if offset and offset >= total_chars:
        return json.dumps({"status": "INVALID_OFFSET", "total_chars": total_chars}, ensure_ascii=False)

    end = offset + len(row["page"])
    return json.dumps(
        {
            "status": "OK",
            "title": row["title"],
            "url": row["url"],
            "offset": offset,
            "end_offset": end,
            "total_chars": total_chars,
            "next_offset": end if end < total_chars else None,
            "content": row["page"],
        },
        ensure_ascii=False,
    )


if __name__ == "__main__":
    print(json.dumps(sync_feishu_knowledge(force=True), ensure_ascii=False))

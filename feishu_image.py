"""下载当前飞书消息的图片，并用 macOS Vision OCR 提取截图文字。"""

import json
import re
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import lark_oapi as lark
from lark_oapi.api.im.v1 import GetMessageResourceRequest

from app_config import PROJECT_ROOT

MAX_IMAGES = 3
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_OCR_CHARS = 3500


def _missing_scopes(response) -> list[str]:
    # SDK 的资源响应模型可能只反序列化 code，权限详情仍在原始响应中。
    try:
        violations = json.loads(response.raw.content)["error"]["permission_violations"]
    except (AttributeError, KeyError, TypeError, ValueError):
        return []
    if not isinstance(violations, list):
        return []
    return sorted({
        item["subject"] for item in violations
        if isinstance(item, dict) and item.get("type") == "action_scope_required"
        and isinstance(item.get("subject"), str)
        and re.fullmatch(r"[a-z][a-z0-9_.:-]{1,100}", item["subject"])
    })


def read_image_text(api_client: lark.Client, message_id: str, image_key: str) -> str:
    if sys.platform != "darwin":
        raise RuntimeError("截图识别需要 macOS Vision")
    request = (GetMessageResourceRequest.builder()
               .message_id(message_id).file_key(image_key).type("image").build())
    try:
        response = api_client.im.v1.message_resource.get(request)
    except Exception as error:
        raise RuntimeError("飞书图片下载请求失败") from error
    if not response.success() or response.file is None:
        if response.code in (99991672, 234009):
            scopes = _missing_scopes(response)
            scope_hint = "、".join(scopes) if scopes else "获取消息中的资源文件所需权限"
            lark.logger.warning(
                f"飞书图片下载缺少权限: code={response.code}, scopes={scopes}, log_id={response.get_log_id()}"
            )
            raise RuntimeError(f"飞书应用缺少下载权限，错误码 {response.code}。请在应用权限管理中检查 {scope_hint}，版本生效后重发")
        raise RuntimeError(f"飞书图片无法下载（错误码 {response.code}）")
    try:
        with response.file as stream:
            image = stream.read(MAX_IMAGE_BYTES + 1)
    except OSError as error:
        raise RuntimeError("读取飞书图片数据失败") from error
    if len(image) > MAX_IMAGE_BYTES:
        raise RuntimeError("图片超过 8 MiB")
    try:
        with TemporaryDirectory(prefix="feishu-ocr-") as directory:
            executable = str(Path(directory) / "screenshot-ocr")
            subprocess.run(
                ["/usr/bin/clang", "-fobjc-arc", "-framework", "Foundation", "-framework", "Vision",
                 str(PROJECT_ROOT / "screenshot_ocr.m"), "-o", executable],
                capture_output=True, check=True, timeout=15,
            )
            result = subprocess.run(
                [executable], input=image, capture_output=True, check=True, timeout=15,
            )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("本地 OCR 执行失败，请检查 macOS 命令行工具或重发截图") from error
    text = result.stdout.decode("utf-8", errors="replace").strip()
    if not text:
        raise RuntimeError("图片中没有识别到文字")
    return text if len(text) <= MAX_OCR_CHARS else text[:MAX_OCR_CHARS] + "\n[识别文字过长，摘录已截断]"

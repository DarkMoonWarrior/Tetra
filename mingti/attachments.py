"""聊天里的附件：粘贴、拖进输入框或者点回形针选的图片、PDF、文本文件。

- 上传：/api/uploads 收下一个文件（data URL），存在内存里（最多 MAX_ITEMS 个、TTL 秒），返回编号、种类、
  PDF 的页数；缩略图由界面生成后一起传上来，存进聊天记录里，重新打开对话时显示。
- 使用：助手按编号取——看图回答时取每页的图片（PDF 按页渲染成 PNG），存进题库时把原文件交给导入
  （importer.py），文本文件直接取文字。
"""

from __future__ import annotations

import base64
import re
import threading
import time
import uuid
from collections import OrderedDict

MAX_BYTES = 25 * 1024 * 1024
MAX_ITEMS = 200
TTL = 4 * 3600
MAX_TEXT = 30_000  # 文本文件最多取这么多字
IMAGE_TYPES = {"png", "jpeg", "jpg", "webp", "gif", "bmp"}
TEXT_TYPES = {"txt", "md", "markdown", "csv", "tex"}
_DATA_URL = re.compile(r"data:([\w.+/-]*);base64,(.*)", re.DOTALL)


class AttachmentError(ValueError):
    """附件有问题：消息直接显示给用户。"""


def _kind(name: str, mime: str, head: bytes) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if head[:4] == b"%PDF" or ext == "pdf" or mime == "application/pdf":
        return "pdf"
    if mime.startswith("image/") or ext in IMAGE_TYPES:
        return "image"
    if mime.startswith("text/") or ext in TEXT_TYPES:
        return "text"
    return ""


class Uploads:
    def __init__(self) -> None:
        self._items: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    def add(self, name: str, data_url: str, thumb: str = "") -> dict:
        name = (name or "文件").strip()[:120] or "文件"
        match = _DATA_URL.fullmatch(data_url or "")
        if not match:
            raise AttachmentError(f"{name} 没读出来（文件内容不完整）")
        mime = match.group(1).lower()
        try:
            content = base64.b64decode(match.group(2), validate=False)
        except ValueError as exc:
            raise AttachmentError(f"{name} 没读出来（文件内容不完整）") from exc
        if len(content) > MAX_BYTES:
            raise AttachmentError(f"{name} 太大了：一个文件最多 {MAX_BYTES // 1024 // 1024} MB")
        kind = _kind(name, mime, content[:8])
        if not kind:
            raise AttachmentError(f"{name} 的格式不支持：可以传图片（PNG、JPG、WebP）、PDF 或文本文件（.txt、.md）")
        item = {
            "id": f"up_{uuid.uuid4().hex[:12]}",
            "name": name,
            "kind": kind,
            "mime": mime or ("application/pdf" if kind == "pdf" else ""),
            "size": len(content),
            "content": content,
            "pages": 1,
            "thumb": thumb if thumb.startswith("data:image/") and len(thumb) < 120_000 else "",
            "created": time.time(),
        }
        if kind == "pdf":
            import pymupdf

            try:
                with pymupdf.open(stream=content, filetype="pdf") as document:
                    item["pages"] = document.page_count
            except Exception as exc:  # noqa: BLE001 - 打不开的 PDF 说清楚
                raise AttachmentError(f"{name} 打不开：文件可能损坏了") from exc
        if kind == "text":
            item["text"] = content.decode("utf-8", errors="replace")[:MAX_TEXT]
        with self._lock:
            self._items[item["id"]] = item
            self._prune()
        return self.public(item)

    def _prune(self) -> None:
        now = time.time()
        for key in [key for key, item in self._items.items() if now - item["created"] > TTL]:
            del self._items[key]
        while len(self._items) > MAX_ITEMS:
            self._items.popitem(last=False)

    @staticmethod
    def public(item: dict) -> dict:
        return {key: item[key] for key in ("id", "name", "kind", "size", "pages", "thumb")}

    def get(self, ids: list[str]) -> list[dict]:
        """按编号取附件；过期或者不存在的抛 AttachmentError（服务重启过、隔了好几个小时）。"""
        with self._lock:
            found = [self._items.get(upload_id) for upload_id in ids]
        missing = [upload_id for upload_id, item in zip(ids, found) if item is None]
        if missing:
            raise AttachmentError("附件已经过期（服务重启过，或者传上来太久了），请重新添加一次")
        return found

    def summary(self, ids: list[str]) -> list[dict]:
        """给模型看的附件说明：名字、种类、页数。"""
        return [{"name": item["name"], "kind": item["kind"], "pages": item["pages"]} for item in self.get(ids)]

    def files(self, ids: list[str]) -> list[dict]:
        """图片和 PDF：交给导入（importer.render_pages）的格式 [{name, data}]。"""
        out = []
        for item in self.get(ids):
            if item["kind"] in ("image", "pdf"):
                mime = item["mime"] or ("application/pdf" if item["kind"] == "pdf" else "image/png")
                out.append({"name": item["name"], "data": f"data:{mime};base64," + base64.b64encode(item["content"]).decode("ascii")})
        return out

    def texts(self, ids: list[str]) -> list[dict]:
        return [{"name": item["name"], "text": item["text"]} for item in self.get(ids) if item["kind"] == "text"]

    def page_images(self, ids: list[str], limit: int = 8) -> tuple[list[str], int]:
        """看图回答用：图片和 PDF 每页一张图（data URL），最多 limit 张；返回（图片，一共有几页）。
        不太大的 PNG、JPG、WebP 原样发；太大的图片和 PDF 的页面用 PyMuPDF 渲染、缩到最长边 2000 像素。"""
        import pymupdf

        out: list[str] = []
        total = 0
        for item in self.get(ids):
            if item["kind"] not in ("image", "pdf"):
                continue
            if item["kind"] == "image":
                total += 1
                if len(out) >= limit:
                    continue
                if item["mime"] in ("image/png", "image/jpeg", "image/webp") and item["size"] <= 3_500_000:
                    out.append(f"data:{item['mime']};base64," + base64.b64encode(item["content"]).decode("ascii"))
                    continue
            filetype = "pdf" if item["kind"] == "pdf" else (item["name"].rsplit(".", 1)[-1].lower() or "png")
            try:
                with pymupdf.open(stream=item["content"], filetype=filetype) as document:
                    if item["kind"] == "pdf":
                        total += document.page_count
                    for page in document:
                        if len(out) >= limit:
                            break
                        side = max(page.rect.width, page.rect.height) or 1
                        scale = min(150 / 72 if item["kind"] == "pdf" else 1.0, 2000 / side)
                        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                        out.append("data:image/png;base64," + base64.b64encode(pixmap.tobytes("png")).decode("ascii"))
            except Exception as exc:  # noqa: BLE001 - 打不开的文件说清楚是哪个
                raise AttachmentError(f"{item['name']} 打不开：图片太大或者格式不支持，换成 PNG、JPG 再试") from exc
        return out, total

"""
PDF 遮盖工作台 —— FastAPI + PyMuPDF 后端。

核心安全模型
------------
下载的副本不是在原页面上叠加色块，而是把每一页（无论是否有遮盖框）
重新光栅化为位图，再以这些位图组成一份全新的 PDF：

* 新 PDF 中不存在原文字层、批注、超链接、表单域、附件、书签、XMP/Info 元数据；
* 遮盖框是在位图像素上就地填充的不透明矩形，框内原像素被直接覆写、不可恢复；
* 新页面大小与原页面 CropBox 的显示尺寸（MediaBox 经 rotation 后的可见尺寸）一致，
  方向沿用原页方向。

遮盖框坐标一律使用"显示坐标系"（用户在预览中看到的、未经旋转的点，1/72 英寸）。
PyMuPDF 的 get_pixmap 输出已经是显示方向（旋转被烘焙进像素、裁切体现在尺寸上），
因此显示坐标乘以渲染缩放即像素坐标，旋转页 / 裁切页都能准确覆盖同一内容。
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import fitz  # PyMuPDF
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

MAX_PAGES = 8                 # 最多接受的页数
MAX_UPLOAD_BYTES = 100 * 1024 * 1024   # 上传大小上限 100MB
RENDER_DPI = 200              # 副本位图渲染分辨率（像素/英寸）
RENDER_ZOOM = RENDER_DPI / 72.0
MAX_SESSIONS = 8              # 内存中同时保留的会话数量（单机单人使用）
SESSION_TTL = 60 * 60         # 会话闲置 1 小时后丢弃

# 允许的遮盖颜色（前端拾色器之外的输入一律拒绝）
ALLOWED_COLORS: dict[str, tuple[int, int, int]] = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "red": (220, 38, 38),
    "blue": (37, 99, 235),
    "green": (22, 163, 74),
    "yellow": (227, 180, 30),
}


# ---------------------------------------------------------------------------
# 会话存储（进程内；原始 PDF 字节，绝不在上传时覆盖正在编辑的文档）
# ---------------------------------------------------------------------------

@dataclass
class Rect:
    """显示坐标系下的遮盖矩形，单位为点（1/72 英寸）。"""
    x0: float
    y0: float
    x1: float
    y1: float
    color: str = "black"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def normalized(self) -> "Rect":
        return Rect(min(self.x0, self.x1), min(self.y0, self.y1),
                    max(self.x0, self.x1), max(self.y0, self.y1),
                    self.color, self.id)


@dataclass
class Confirmation:
    """某一页的复核确认：绑定确认当时的遮盖版本与框数。"""
    version: int
    box_count: int


@dataclass
class Delivery:
    """一份定版交付包（ZIP 字节 + 其中 PDF 的 SHA-256），只对应某一版确认快照。"""
    sig: tuple[tuple[int, int, int], ...]   # (page, version, box_count) × N，升序
    pdf_sha256: str
    blob: bytes


@dataclass
class Session:
    pdf_bytes: bytes
    pages: list[dict[str, Any]]
    boxes: dict[int, list[Rect]]
    box_version: dict[int, int]             # 每页遮盖内容的单调版本号（初版 1）
    confirmations: dict[int, Confirmation]  # 已复核页 -> 确认（含版本绑定）
    delivery: Delivery | None = None        # 当前定版交付包；任何编辑立即作废
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_used: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_used = time.time()


_sessions: dict[str, Session] = {}
_sessions_guard = threading.Lock()


def _evict_sessions() -> None:
    """超出容量或过期时清理最久未使用的会话。"""
    now = time.time()
    with _sessions_guard:
        stale = [sid for sid, s in _sessions.items()
                 if now - s.last_used > SESSION_TTL]
        for sid in stale:
            _sessions.pop(sid, None)
        while len(_sessions) >= MAX_SESSIONS:
            oldest = min(_sessions, key=lambda k: _sessions[k].last_used)
            _sessions.pop(oldest, None)


def get_session(sid: str) -> Session:
    with _sessions_guard:
        session = _sessions.get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="编辑会话不存在或已过期，请重新上传 PDF。")
    session.touch()
    return session


# ---------------------------------------------------------------------------
# PDF 校验
# ---------------------------------------------------------------------------

def _validate_pdf(data: bytes) -> fitz.Document:
    """
    完整校验上传内容，返回打开的文档（调用方负责 close）。

    任何失败都抛出 400；调用方在校验全部通过之后才创建会话，
    因而不会覆盖正在编辑的文档。
    """
    if not data:
        raise HTTPException(status_code=400, detail="上传的文件为空。")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="文件超过 100MB 上限。")

    doc: fitz.Document
    try:
        doc = fitz.open(stream=io.BytesIO(data), filetype="pdf")
    except Exception:
        raise HTTPException(status_code=400, detail="文件不是有效的 PDF（无法解析）。")

    if doc.needs_pass:
        raise HTTPException(
            status_code=400,
            detail="该 PDF 已加密/设置了密码，本工具不支持加密文档。")
    if doc.page_count == 0:
        raise HTTPException(status_code=400, detail="PDF 中没有任何页面。")
    if doc.page_count > MAX_PAGES:
        raise HTTPException(
            status_code=400,
            detail=f"PDF 共 {doc.page_count} 页，超过 {MAX_PAGES} 页上限。")
    # 强制解析每一页并做一次极小光栅化，捕获损坏/受保护/内容流异常的页面
    for i in range(doc.page_count):
        page = doc.load_page(i)
        try:
            pix = page.get_pixmap(matrix=fitz.Matrix(0.02, 0.02), alpha=False)
            if pix.width < 1 or pix.height < 1:
                raise ValueError("bad pixmap")
        except Exception:
            raise HTTPException(
                status_code=400, detail=f"第 {i + 1} 页已损坏或内容无法渲染。")
    return doc


# ---------------------------------------------------------------------------
# 几何与光栅化
# ---------------------------------------------------------------------------

def _validate_rect(rect: Rect, page: fitz.Page, index: int | None = None) -> Rect:
    """校验矩形坐标为有限数且落在显示页面范围内；返回标准化矩形。"""
    vals = (rect.x0, rect.y0, rect.x1, rect.y1)
    if any(not math.isfinite(v) for v in vals):
        raise HTTPException(status_code=400, detail="遮盖框坐标无效。")
    r = rect.normalized()
    if r.x1 - r.x0 < 0.5 or r.y1 - r.y0 < 0.5:
        raise HTTPException(status_code=400, detail="遮盖框过小。")
    dw = page.rect.width          # rect 已是显示方向（含 rotation）
    dh = page.rect.height
    if r.x0 < -0.5 or r.y0 < -0.5 or r.x1 > dw + 0.5 or r.y1 > dh + 0.5:
        where = f"（第 {index + 1} 个框）" if index is not None else ""
        raise HTTPException(
            status_code=400,
            detail=f"遮盖框超出页面显示范围{where}：页面 {dw:.0f}×{dh:.0f} 点。")
    r.x0 = max(0.0, min(r.x0, dw)); r.y0 = max(0.0, min(r.y0, dh))
    r.x1 = max(0.0, min(r.x1, dw)); r.y1 = max(0.0, min(r.y1, dh))
    if r.color not in ALLOWED_COLORS:
        raise HTTPException(status_code=400, detail="不支持的遮盖颜色。")
    return r


def _fill_pixmap_rect(pix: fitz.Pixmap, px0: int, py0: int,
                      px1: int, py1: int, color: tuple[int, int, int]) -> None:
    """
    就地覆写 Pixmap 中矩形覆盖的全部像素（不透明，含 alpha 通道时写成 255）。

    优先使用 PyMuPDF 原生 set_rect；老版本没有该接口时通过可写的
    samples_mv 直接改像素行。框内原始像素被完全覆盖，
    新 PDF 中不存在可被还原的底层内容。
    """
    px0 = max(0, min(px0, pix.width)); px1 = max(0, min(px1, pix.width))
    py0 = max(0, min(py0, pix.height)); py1 = max(0, min(py1, pix.height))
    if px1 <= px0 or py1 <= py0:
        return

    if hasattr(pix, "set_rect"):
        # 本工具所有 pixmap 均以 alpha=False 渲染，无需处理透明通道
        pix.set_rect(fitz.IRect(px0, py0, px1, py1), color)
        return

    # 后备路径：直接改像素字节（samples 在新版只读，但 samples_mv 可写）
    n = pix.n
    stride = pix.stride
    cell = bytes(color[: n - (1 if pix.alpha else 0)])
    if pix.alpha:
        cell += b"\xff"
    row = cell * (px1 - px0)
    view = pix.samples_mv if hasattr(pix, "samples_mv") else memoryview(pix.samples)
    start_off = px0 * n
    end_off = px1 * n
    for y in range(py0, py1):
        line = y * stride
        view[line + start_off: line + end_off] = row


def render_page_png(session: Session, page_index: int, zoom: float,
                    redacted: bool) -> tuple[bytes, int]:
    """
    把指定页面光栅化为 PNG。

    redacted=False：干净预览底图（遮盖框由前端 canvas 叠加）。
    redacted=True ：把遮盖框烘焙进像素（用于"最终效果预览/复核"，与下载同一条渲染路径）。

    返回 (PNG 字节, 该页当前遮盖版本)。烘焙图与版本号在同一把锁内取得，
    前端确认该页时必须回传这个版本，框在两者之间被改过则版本失配、必须重新预览。
    """
    if not (0.1 <= zoom <= 8.0):
        raise HTTPException(status_code=400, detail="缩放比例超出允许范围。")
    with session.lock:
        doc = fitz.open(stream=io.BytesIO(session.pdf_bytes), filetype="pdf")
        try:
            page = doc.load_page(page_index)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            # get_pixmap 不会更新 pixmap 的 DPI 元数据（恒为 96），
            # 不修正的话导出的 PNG pHYs 错误，会被部分解码器按错误尺寸解释；
            # 填充像素坐标与编码 DPI 都以实际缩放为准，保证预览==下载
            dpi = int(round(zoom * 72))
            pix.set_dpi(dpi, dpi)
            if redacted:
                # get_pixmap 输出的位图轴与显示页面轴一致（旋转已烘焙进像素），
                # 显示坐标 ×zoom 即像素坐标；旋转页/裁切页同理
                for rect in session.boxes.get(page_index, []):
                    _fill_pixmap_rect(pix,
                                      int(math.floor(rect.x0 * zoom)),
                                      int(math.floor(rect.y0 * zoom)),
                                      int(math.ceil(rect.x1 * zoom)),
                                      int(math.ceil(rect.y1 * zoom)),
                                      ALLOWED_COLORS[rect.color])
            version = session.box_version[page_index]
            return pix.tobytes("png"), version
        finally:
            doc.close()


def render_redacted_pdf(pdf_bytes: bytes,
                        boxes_snapshot: dict[int, list[Rect]]) -> tuple[bytes, int]:
    """
    用给定的原始 PDF 字节与遮盖框快照重新生成副本（纯函数，不触碰会话锁）：
    每一页（含无遮盖的页）都渲染为 RENDER_DPI 位图，遮盖框就地写入像素，
    再以位图铺满新页面（显示尺寸 / 方向与原页一致）。返回 (PDF 字节, 页数)。
    """
    src = fitz.open(stream=io.BytesIO(pdf_bytes), filetype="pdf")
    out = fitz.open()
    try:
        for i in range(src.page_count):
            page = src.load_page(i)
            pix = page.get_pixmap(matrix=fitz.Matrix(RENDER_ZOOM, RENDER_ZOOM),
                                  alpha=False, colorspace=fitz.csRGB)
            pix.set_dpi(RENDER_DPI, RENDER_DPI)
            # 位图为显示方向，显示坐标 ×RENDER_ZOOM 直接得到像素坐标
            for rect in boxes_snapshot.get(i, []):
                _fill_pixmap_rect(pix,
                                  int(math.floor(rect.x0 * RENDER_ZOOM)),
                                  int(math.floor(rect.y0 * RENDER_ZOOM)),
                                  int(math.ceil(rect.x1 * RENDER_ZOOM)),
                                  int(math.ceil(rect.y1 * RENDER_ZOOM)),
                                  ALLOWED_COLORS[rect.color])

            # page.rect 的宽高即"显示尺寸"（rotation 后），新页方向自动保持
            new_page = out.new_page(width=page.rect.width,
                                    height=page.rect.height)
            new_page.insert_image(new_page.rect, pixmap=pix, keep_proportion=True)

        # 不携带任何原文档元数据 / XMP
        out.set_metadata({k: "" for k in (
            "title", "author", "subject", "keywords", "creator",
            "producer", "creationDate", "modDate", "trapped")})
        try:
            out.del_xml_metadata()
        except Exception:
            pass
        buf = out.tobytes(deflate=True, garbage=4, clean=True)
        return buf, out.page_count
    finally:
        out.close()
        src.close()


def build_redacted_pdf(session: Session) -> tuple[bytes, int]:
    """在会话锁内取遮盖框快照后渲染副本（普通"下载遮盖副本"按钮使用）。"""
    with session.lock:
        pdf_bytes = session.pdf_bytes
        snapshot = {p: list(rects) for p, rects in session.boxes.items()}
    return render_redacted_pdf(pdf_bytes, snapshot)


# ---------------------------------------------------------------------------
# 逐页复核 / 定版交付
# ---------------------------------------------------------------------------

def _invalidate_page_after_edit(session: Session, page: int,
                                changed: bool = True) -> None:
    """
    某页加框 / 删框 / 清空后的统一处理（调用方须持有 session.lock）：

    * 遮盖内容真的变化时版本 +1 —— 此前的烘焙预览与确认全部对不上新版；
      内容无变化（如清空本就为空的页）则不动版本、不撤销确认；
    * 只删除该页的复核确认，其他页确认保持有效；
    * 立即撤销旧定版交付包（ZIP 缓存），任何在途/旧链接都拿不到过期包。
    """
    if not changed:
        return
    session.box_version[page] = session.box_version.get(page, 1) + 1
    session.confirmations.pop(page, None)
    session.delivery = None


def _review_snapshot(session: Session
                     ) -> tuple[bytes, dict[int, list[Rect]],
                                tuple[tuple[int, int, int], ...]]:
    """
    调用方须持有 session.lock。取出与同一版"确认"严格对应的：
    原始 PDF 字节、各页框列表快照、定版签名 ((page, version, box_count) × N)。
    """
    snapshot = {p: list(rects) for p, rects in session.boxes.items()}
    sig = tuple(
        (p, session.box_version[p], len(session.boxes.get(p, [])))
        for p in sorted(session.boxes)
    )
    return session.pdf_bytes, snapshot, sig


def _confirmation_sig(session: Session) -> tuple[tuple[int, int, int], ...] | None:
    """
    调用方须持有 session.lock。若全部页都已复核，返回确认所绑定的签名；
    否则返回 None。"""
    if len(session.confirmations) != len(session.pages):
        return None
    return tuple(
        (p, session.confirmations[p].version, session.confirmations[p].box_count)
        for p in sorted(session.confirmations)
    )


def build_review_zip(pdf_bytes: bytes,
                     boxes_snapshot: dict[int, list[Rect]],
                     sig: tuple[tuple[int, int, int], ...],
                     page_count: int, source_filename: str,
                     source_sha256: str) -> tuple[bytes, str]:
    """
    根据一版已全部确认的遮盖快照生成定版交付 ZIP（纯函数）：

    * <base>-redacted.pdf —— 位图遮盖 PDF；
    * manifest.json       —— 每页确认的框数、烘焙参数与 PDF 的 SHA-256。

    ZIP 条目不写入时间戳等非确定字段，且 PDF 渲染对同一快照是确定性的，
    因而不再修改时重复下载得到字节完全相同的 ZIP。
    """
    pdf_blob, _pages = render_redacted_pdf(pdf_bytes, boxes_snapshot)
    pdf_sha = hashlib.sha256(pdf_blob).hexdigest()

    base = (source_filename or "document.pdf").rsplit(".pdf", 1)[0]
    base = "".join(c for c in base if c not in '\\/:*?"<>|').strip()[:80] or "document"
    pdf_name = f"{base}-redacted.pdf"
    manifest = {
        "tool": "pdf-redaction-workbench",
        "package": "final-reviewed-delivery",
        "source_filename": source_filename or "document.pdf",
        "source_pdf_sha256": source_sha256,
        "render_dpi": RENDER_DPI,
        "page_count": page_count,
        "redacted_pdf": pdf_name,
        "redacted_pdf_sha256": pdf_sha,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pages": [
            {
                "page": p + 1,
                "confirmed_box_count": next(
                    n for (pp, _v, n) in sig if pp == p),
            }
            for p in range(page_count)
        ],
    }
    manifest_blob = json.dumps(
        manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in ((pdf_name, pdf_blob), ("manifest.json", manifest_blob)):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            zf.writestr(info, data)
    return buf.getvalue(), pdf_sha


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class RectIn(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float
    color: str = "black"


class BoxAddIn(BaseModel):
    page: int = Field(ge=0)
    rect: RectIn


class BoxDeleteIn(BaseModel):
    page: int = Field(ge=0)
    id: str


class PageOnlyIn(BaseModel):
    page: int = Field(ge=0)


class ConfirmIn(BaseModel):
    page: int = Field(ge=0)
    version: int = Field(ge=1)


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

app = FastAPI(title="PDF 遮盖工作台")


@app.post("/api/upload")
def upload(file: UploadFile = File(...)) -> dict[str, Any]:
    data = file.file.read()

    # 先做完整校验；任何错误都在新会话创建之前抛出 —— 不覆盖正在编辑的文档
    doc = _validate_pdf(data)
    pages: list[dict[str, Any]] = []
    try:
        for i in range(doc.page_count):
            page = doc.load_page(i)
            pages.append({
                "index": i,
                "width": round(page.rect.width, 2),
                "height": round(page.rect.height, 2),
                "rotation": page.rotation,
            })
    finally:
        doc.close()

    _evict_sessions()
    sid = uuid.uuid4().hex
    with _sessions_guard:
        _sessions[sid] = Session(
            pdf_bytes=data,
            pages=pages,
            boxes={i: [] for i in range(len(pages))},
            box_version={i: 1 for i in range(len(pages))},
            confirmations={},
        )
    return {"session": sid, "filename": file.filename or "document.pdf",
            "pageCount": len(pages), "pages": pages}


@app.get("/api/{sid}/state")
def state(sid: str) -> dict[str, Any]:
    s = get_session(sid)
    with s.lock:
        return {
            "pages": s.pages,
            "boxes": {
                str(p): [{"id": b.id, **{k: getattr(b, k)
                                         for k in ("x0", "y0", "x1", "y1", "color")}}
                         for b in rects]
                for p, rects in s.boxes.items()
            },
            "review": {
                "allConfirmed": len(s.confirmations) == len(s.pages),
                "pages": {
                    str(p): {
                        "boxVersion": s.box_version[p],
                        "boxCount": len(s.boxes.get(p, [])),
                        "confirmed": p in s.confirmations,
                        "confirmedVersion":
                            s.confirmations[p].version if p in s.confirmations else None,
                    }
                    for p in range(len(s.pages))
                },
            },
        }


@app.get("/api/{sid}/page/{index}.png")
def page_image(sid: str, index: int, zoom: float = 1.5,
               redacted: bool = False) -> Response:
    s = get_session(sid)
    if not (0 <= index < len(s.pages)):
        raise HTTPException(status_code=404, detail="页码不存在。")
    png, version = render_page_png(s, index, zoom, redacted)
    # X-Box-Version：此画面（尤其烘焙画面）对应的遮盖版本；
    # 前端确认该页时必须回传同一版本，期间框被改动则确认被拒、须重新预览。
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "no-store",
                             "X-Box-Version": str(version)})


@app.post("/api/{sid}/boxes")
def add_box(sid: str, payload: BoxAddIn) -> dict[str, Any]:
    s = get_session(sid)
    if not (0 <= payload.page < len(s.pages)):
        raise HTTPException(status_code=400, detail="页码不存在。")
    with s.lock:
        doc = fitz.open(stream=io.BytesIO(s.pdf_bytes), filetype="pdf")
        try:
            page = doc.load_page(payload.page)
            rect = _validate_rect(
                Rect(payload.rect.x0, payload.rect.y0,
                     payload.rect.x1, payload.rect.y1, payload.rect.color),
                page)
        finally:
            doc.close()
        s.boxes[payload.page].append(rect)
        _invalidate_page_after_edit(s, payload.page)
        return {"id": rect.id, "rect": {k: getattr(rect, k)
                                        for k in ("x0", "y0", "x1", "y1", "color")}}


@app.post("/api/{sid}/boxes/delete")
def delete_box(sid: str, payload: BoxDeleteIn) -> dict[str, Any]:
    s = get_session(sid)
    if not (0 <= payload.page < len(s.pages)):
        raise HTTPException(status_code=400, detail="页码不存在。")
    with s.lock:
        rects = s.boxes[payload.page]
        for idx, rect in enumerate(rects):
            if rect.id == payload.id:
                removed = rects.pop(idx)
                _invalidate_page_after_edit(s, payload.page)
                return {"deleted": True, "id": removed.id}
        raise HTTPException(status_code=404, detail="遮盖框不存在。")


@app.post("/api/{sid}/boxes/clear")
def clear_page_boxes(sid: str, payload: PageOnlyIn) -> dict[str, Any]:
    s = get_session(sid)
    if not (0 <= payload.page < len(s.pages)):
        raise HTTPException(status_code=400, detail="页码不存在。")
    with s.lock:
        had_boxes = bool(s.boxes.get(payload.page))
        s.boxes[payload.page] = []
        _invalidate_page_after_edit(s, payload.page, changed=had_boxes)
    return {"cleared": True}


@app.post("/api/{sid}/review/confirm")
def confirm_page(sid: str, payload: ConfirmIn) -> dict[str, Any]:
    """
    确认某页复核通过。必须回传"打开服务器烘焙预览"时响应头里的遮盖版本：

    * 版本与当前一致才登记确认（确认绑定当时展示的那一版遮盖画面）；
    * 期间该页框被任何请求改过（版本递增）则 409，必须重新烘焙预览后再确认，
      不得把旧画面标为已复核；
    * 每页含 0 个框的页面同样需要确认。
    """
    s = get_session(sid)
    if not (0 <= payload.page < len(s.pages)):
        raise HTTPException(status_code=400, detail="页码不存在。")
    with s.lock:
        current = s.box_version[payload.page]
        if payload.version != current:
            raise HTTPException(
                status_code=409,
                detail="该页遮盖内容在预览之后已被修改，请重新打开服务器烘焙预览再确认。")
        count = len(s.boxes.get(payload.page, []))
        s.confirmations[payload.page] = Confirmation(version=current, box_count=count)
        # 防御：确认与当前状态一致时旧交付本就不可能再匹配（缺这一页的旧确认），
        # 这里显式撤销，确保任何旧包立即不可再取。
        s.delivery = None
        return {"confirmed": True, "page": payload.page,
                "boxVersion": current, "boxCount": count,
                "allConfirmed": len(s.confirmations) == len(s.pages)}


@app.get("/api/{sid}/review/delivery")
def review_delivery(sid: str, filename: str = "document.pdf") -> Response:
    """
    定版交付：全部页面（含无遮盖框的页）都已复核后，下载包含

    * 位图遮盖 PDF（与确认时逐页烘焙预览、普通下载同一条渲染路径）；
    * manifest.json（每页确认的框数、烘焙 DPI 与 PDF 的 SHA-256）

    的 ZIP。编辑会改变确认签名并撤销旧包；交错编辑期间本接口要么拒绝（尚有未复核页），
    要么只返回与当前确认、当前 PDF 严格对应的新包。停止修改后重复下载直接复用
    同一份缓存包，包内 PDF 字节与其 SHA-256 摘要逐次相同。
    """
    s = get_session(sid)

    # 渲染是重活，不持会话锁；按"锁内取快照 -> 锁外渲染 -> 锁内复核签名"循环，
    # 只有签名在渲染前后保持一致才缓存/下发，杜绝把过期画面打进交付包。
    for _ in range(3):
        with s.lock:
            sig = _confirmation_sig(s)
            if sig is None:
                raise HTTPException(
                    status_code=409,
                    detail="尚有页面未完成复核，不能生成定版交付包。")
            if s.delivery is not None and s.delivery.sig == sig:
                blob, pdf_sha = s.delivery.blob, s.delivery.pdf_sha256
                break
            pdf_bytes, snapshot, state_sig = _review_snapshot(s)
            source_sha = hashlib.sha256(pdf_bytes).hexdigest()
        # 确认版本必须与当前遮盖版本一致（编辑已清确认，这里属防御性校验）
        if sig != state_sig:
            raise HTTPException(status_code=409,
                                detail="遮盖内容与复核版本不一致，请重新复核受影响页。")
        blob, pdf_sha = build_review_zip(
            pdf_bytes, snapshot, sig, len(s.pages), filename, source_sha)
        with s.lock:
            cur_sig = _confirmation_sig(s)
            if cur_sig != sig:
                continue   # 渲染期间发生过编辑；丢弃本次结果重来
            s.delivery = Delivery(sig=sig, pdf_sha256=pdf_sha, blob=blob)
            break
    else:  # 连续重试仍被编辑打断
        raise HTTPException(status_code=409,
                            detail="遮盖内容正在被修改，请停止编辑后再生成定版交付包。")

    safe_base = filename.rsplit(".pdf", 1)[0][:80].strip() or "document"
    zip_name = f"{safe_base}-reviewed.zip"
    return Response(
        content=blob,
        media_type="application/zip",
        headers={
            "Content-Disposition":
                f"attachment; filename*=UTF-8''{quote(zip_name)}",
            "Cache-Control": "no-store",
            "X-Redacted-Pdf-Sha256": pdf_sha,
        },
    )


@app.get("/api/{sid}/download")
def download(sid: str, filename: str = "document.pdf") -> Response:
    s = get_session(sid)
    pdf_bytes, _pages = build_redacted_pdf(s)

    safe_base = filename.rsplit(".pdf", 1)[0][:80].strip() or "document"
    safe_name = f"{safe_base}-redacted.pdf"
    quoted = quote(safe_name)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition":
                f"attachment; filename*=UTF-8''{quoted}",
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "engine": f"PyMuPDF {fitz.version[0]}"}


# 静态前端（/ 与 /index.html）
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

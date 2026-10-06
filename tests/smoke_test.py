"""端到端冒烟测试（不进镜像，仅开发环境用）。"""
import hashlib
import io
import json
import sys
import zipfile
import fitz
from fastapi.testclient import TestClient

sys.path.insert(0, "/workspace")
from app.main import app, MAX_PAGES  # noqa: E402

client = TestClient(app)
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail else ""))


def make_pdf(path, *, pages=3, rotations=(0, 90, 270), cropbox=None,
             encrypted=False, annotate=False):
    doc = fitz.open()
    for i in range(pages):
        w, h = 595, 842
        page = doc.new_page(width=w, height=h)
        # 整页浅色底，便于断言遮盖像素确实变黑
        page.draw_rect(fitz.Rect(0, 0, w, h), color=(0.9, 0.9, 0.9),
                       fill=(0.9, 0.9, 0.9), width=0)
        # 一些文字（文字层）
        for j in range(6):
            page.insert_text((72, 72 + j * 40),
                             f"SECRET page {i + 1} line {j} TOPSECRET",
                             fontsize=24, color=(0.1, 0.1, 0.5))
        rot = rotations[i % len(rotations)]
        page.set_rotation(rot)
        if cropbox is not None and i == 0:
            page.set_cropbox(fitz.Rect(*cropbox))
        if annotate and i == 0:
            page.add_text_annot(fitz.Point(300, 100), "hidden annotation text")
    doc.set_metadata({
        "title": "Secret Title", "author": "Secret Author",
        "subject": "Secret Subject", "keywords": "secret,keywords",
        "creator": "Secret Creator", "producer": "Secret Producer",
    })
    if encrypted:
        doc.save(path, encryption=fitz.PDF_ENCRYPT_AES_256,
                 owner_pw="ownerpw", user_pw="userpw")
    else:
        doc.save(path)
    doc.close()


print("== 1. 上传正常 3 页 PDF（含旋转页/文字层/批注/元数据） ==")
make_pdf("/tmp/test.pdf", annotate=True)
with open("/tmp/test.pdf", "rb") as f:
    r = client.post("/api/upload", files={"file": ("test.pdf", f, "application/pdf")})
check("上传 200", r.status_code == 200, str(r.status_code))
up = r.json()
sid = up["session"]
check("页数 3", up["pageCount"] == 3)
# 第 2 页旋转 90 -> 显示尺寸应为 842x595
check("旋转页显示尺寸交换",
      up["pages"][1]["width"] == 842 and up["pages"][1]["height"] == 595,
      str(up["pages"][1]))
check("旋转标记回传", up["pages"][1]["rotation"] == 90)

print("== 2. 无遮盖时下载：页数/尺寸/方向保留，无文字层/批注/元数据 ==")
r = client.get(f"/api/{sid}/download?filename=test.pdf")
check("下载 200", r.status_code == 200)
plain_bytes = r.content
out = fitz.open(stream=plain_bytes, filetype="pdf")
check("副本页数=3", out.page_count == 3)
check("第1页显示尺寸 595x842",
      (round(out[0].rect.width), round(out[0].rect.height)) == (595, 842))
check("第2页显示尺寸 842x595（旋转保留）",
      (round(out[1].rect.width), round(out[1].rect.height)) == (842, 595))
# 新页以页面尺寸本身表达横向方向（位图本身已是横向内容），视觉方向与原页一致
rp2 = out[1].get_pixmap(matrix=fitz.Matrix(0.5, 0.5), alpha=False)
check("第2页副本位图横向（宽>高，方向一致）", rp2.width > rp2.height,
      f"{rp2.width}x{rp2.height}")
txt = "".join(p.get_text() for p in out)
check("副本无任何文字层（TOPSECRET 不可提取）", "TOPSECRET" not in txt and txt.strip() == "",
      f"len={len(txt.strip())}")
check("无批注", all(list(out[i].annots() or []) == [] for i in range(3)))
md = {k: v for k, v in out.metadata.items() if v and k != "format"}
check("元数据为空（format 为库自动报告）", md == {}, str(out.metadata))
xmp = out.xref_xml_metadata() if hasattr(out, "xref_xml_metadata") else b""
check("无 XMP 元数据", not xmp)
check("每页为整页图片",
      all(len(p.get_images(full=True)) == 1 for p in out))
# 无链接
check("无链接注解", all(not p.get_links() for p in out))
out.close()

print("== 3. 添加遮盖框并验证像素被覆写 ==")
# 在第 1 页显示坐标 60,52 -> 470,110 处加框（覆盖第一行 SECRET 区域）
r = client.post(f"/api/{sid}/boxes", json={
    "page": 0,
    "rect": {"x0": 60, "y0": 52, "x1": 470, "y1": 110, "color": "black"}})
check("加框 200", r.status_code == 200, str(r.status_code))
box_id = r.json()["id"]

# 烘焙预览：框内像素应为黑，框外应为浅灰（直接解码 PNG 原始像素）
png_bytes = client.get(f"/api/{sid}/page/0.png?zoom=2&redacted=true").content
check("烘焙预览 200", True)
pix = fitz.Pixmap(io.BytesIO(png_bytes))
check("预览像素尺寸=595*2 宽", pix.width == round(595 * 2), str(pix.width))
# 点 (200,80) 在框内 -> 像素近黑；点 (200,300) 在框外 -> 浅灰
def px(pix, x_pt, y_pt, zoom=2):
    x, y = int(x_pt * zoom), int(y_pt * zoom)
    off = y * pix.stride + x * pix.n
    return tuple(pix.samples[off:off + 3])
inside = px(pix, 200, 80)
outside = px(pix, 200, 300)
check("框内像素=黑", inside == (0, 0, 0), str(inside))
check("框外像素=浅灰(未动)", outside[0] > 200, str(outside))

# 干净预览对应位置应是浅灰（框只在烘焙时写入）
pix0 = fitz.Pixmap(io.BytesIO(client.get(
    f"/api/{sid}/page/0.png?zoom=2&redacted=false").content))
check("干净预览无黑色块", px(pix0, 200, 80)[0] > 200)

r = client.get(f"/api/{sid}/download?filename=test.pdf")
red = fitz.open(stream=r.content, filetype="pdf")
rpix = red[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
check("下载副本框内=黑", px(rpix, 200, 80) == (0, 0, 0), str(px(rpix, 200, 80)))
check("下载副本框外=浅灰", px(rpix, 200, 300)[0] > 200)
# 200 DPI: 页宽 595pt -> 像素约 1653
check("副本为 200DPI 位图",
      red[0].get_images(full=True)[0][2] > 1500, str(red[0].get_images(full=True)[0][2:4]))
red.close()

print("== 4. 旋转页（90°）上的框：显示坐标精确映射到内容 ==")
r = client.post(f"/api/{sid}/boxes", json={
    "page": 1,
    "rect": {"x0": 60, "y0": 52, "x1": 470, "y1": 110, "color": "black"}})
check("旋转页加框 200", r.status_code == 200)
pix = fitz.Pixmap(io.BytesIO(client.get(
    f"/api/{sid}/page/1.png?zoom=2&redacted=true").content))
check("90°页预览像素横向", pix.width > pix.height, f"{pix.width}x{pix.height}")
check("90°页框内像素=黑", px(pix, 200, 80) == (0, 0, 0))
check("90°页框外像素=浅灰", px(pix, 200, 400)[0] > 200)
red = fitz.open(stream=client.get(f"/api/{sid}/download").content, filetype="pdf")
check("下载与预览一致（旋转页框内黑）",
      px(red[1].get_pixmap(matrix=fitz.Matrix(2, 2)), 200, 80) == (0, 0, 0))
red.close()

print("== 5. 删除框后立即生效 ==")
r = client.post(f"/api/{sid}/boxes/delete", json={"page": 0, "id": box_id})
check("删框 200", r.status_code == 200 and r.json()["deleted"] is True)
pix = fitz.Pixmap(io.BytesIO(client.get(
    f"/api/{sid}/page/0.png?zoom=2&redacted=true").content))
check("删框后原位置恢复为页面内容（浅灰）", px(pix, 200, 80)[0] > 200)
r = client.post(f"/api/{sid}/boxes/delete", json={"page": 0, "id": box_id})
check("重复删除返回 404", r.status_code == 404)

print("== 6. 加密 / 无效 / 超页数 PDF 的错误处理 ==")
make_pdf("/tmp/enc.pdf", encrypted=True)
with open("/tmp/enc.pdf", "rb") as f:
    r = client.post("/api/upload", files={"file": ("enc.pdf", f, "application/pdf")})
check("加密 PDF 400", r.status_code == 400 and "加密" in r.json()["detail"])
r = client.post("/api/upload", files={"file": ("x.pdf", b"not a pdf at all", "application/pdf")})
check("垃圾内容 400", r.status_code == 400 and "有效" in r.json()["detail"])
r = client.post("/api/upload", files={"file": ("x.pdf", b"", "application/pdf")})
check("空文件 400", r.status_code == 400)
make_pdf("/tmp/big.pdf", pages=MAX_PAGES + 1, rotations=(0,))
with open("/tmp/big.pdf", "rb") as f:
    r = client.post("/api/upload", files={"file": ("big.pdf", f, "application/pdf")})
check(f"{MAX_PAGES + 1} 页 PDF 400", r.status_code == 400 and "8 页" in r.json()["detail"])

print("== 7. 上传失败不覆盖正在编辑的会话 ==")
r = client.get(f"/api/{sid}/state")
check("旧会话仍可访问", r.status_code == 200)
# 第 1 页的旋转框还在（未被破坏）
check("旧遮盖框保留", len(r.json()["boxes"]["1"]) == 1)

print("== 8. CropBox 裁切页 ==")
make_pdf("/tmp/crop.pdf", cropbox=(100, 100, 500, 700))
with open("/tmp/crop.pdf", "rb") as f:
    r = client.post("/api/upload", files={"file": ("crop.pdf", f, "application/pdf")})
check("裁切页上传 200", r.status_code == 200)
csid = r.json()["session"]
cw, ch = r.json()["pages"][0]["width"], r.json()["pages"][0]["height"]
check("显示尺寸=裁切尺寸 400x600", (round(cw), round(ch)) == (400, 600), f"{cw}x{ch}")
client.post(f"/api/{csid}/boxes", json={
    "page": 0, "rect": {"x0": 10, "y0": 10, "x1": 390, "y1": 50, "color": "black"}})
d = fitz.open(stream=client.get(f"/api/{csid}/download").content, filetype="pdf")
check("裁切副本尺寸 400x600",
      (round(d[0].rect.width), round(d[0].rect.height)) == (400, 600))
p = d[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
check("裁切页框内=黑", px(p, 200, 30) == (0, 0, 0))
check("裁切页框外=浅灰", px(p, 200, 300)[0] > 200)
d.close()

print("== 9. 越界/非法框 ==")
r = client.post(f"/api/{sid}/boxes", json={
    "page": 0, "rect": {"x0": -10, "y0": 0, "x1": 100, "y1": 100, "color": "black"}})
check("越界框 400", r.status_code == 400)
r = client.post(f"/api/{sid}/boxes", json={
    "page": 0, "rect": {"x0": 0, "y0": 0, "x1": 100, "y1": 100, "color": "hotpink"}})
check("非法颜色 400", r.status_code == 400)
r = client.post(f"/api/{sid}/boxes", json={
    "page": 99, "rect": {"x0": 0, "y0": 0, "x1": 100, "y1": 100}})
check("非法页码 400/422", r.status_code in (400, 422))

print("== 10. 健康检查 / 首页 ==")
check("/api/health", client.get("/api/health").json()["status"] == "ok")
check("/ 返回 HTML", client.get("/").status_code == 200 and
      "PDF" in client.get("/").text)
check("未知会话 404", client.get("/api/deadbeef/state").status_code == 404)

print("== 11. 逐页复核：版本令牌与 409 拒绝旧画面 ==")
make_pdf("/tmp/rev.pdf", pages=2, rotations=(0, 0))
with open("/tmp/rev.pdf", "rb") as f:
    r = client.post("/api/upload", files={"file": ("rev.pdf", f, "application/pdf")})
check("复核用 PDF 上传 200", r.status_code == 200)
rid = r.json()["session"]

st = client.get(f"/api/{rid}/state").json()["review"]
check("初始两页均待复核",
      all(not st["pages"][str(i)]["confirmed"] for i in range(2))
      and st["allConfirmed"] is False, str(st))
check("初始版本号=1", all(st["pages"][str(i)]["boxVersion"] == 1 for i in range(2)))

# 干净预览不用于确认，但也带版本头
r0 = client.get(f"/api/{rid}/page/0.png?zoom=2&redacted=false")
check("预览响应带 X-Box-Version", r0.headers.get("X-Box-Version") == "1",
      r0.headers.get("X-Box-Version"))

# 烘焙第 1 页（0 框）并确认
rb = client.get(f"/api/{rid}/page/0.png?zoom=2&redacted=true")
v0 = int(rb.headers["X-Box-Version"])
check("烘焙第1页版本=1", v0 == 1)
r = client.post(f"/api/{rid}/review/confirm", json={"page": 0, "version": v0})
check("确认第1页（无框页也要确认）200",
      r.status_code == 200 and r.json()["boxCount"] == 0
      and r.json()["allConfirmed"] is False, str(r.status_code))

# 全部确认前定版交付 -> 409
r = client.get(f"/api/{rid}/review/delivery")
check("未全部确认时交付 409", r.status_code == 409)

# 第 2 页加一个框（第 1 页确认不应受影响）
client.post(f"/api/{rid}/boxes", json={
    "page": 1, "rect": {"x0": 60, "y0": 52, "x1": 470, "y1": 110, "color": "black"}})
st = client.get(f"/api/{rid}/state").json()["review"]
check("他页编辑不影响已确认页", st["pages"]["0"]["confirmed"] is True)
check("第2页版本升至 2", st["pages"]["1"]["boxVersion"] == 2
      and st["pages"]["1"]["boxCount"] == 1)

# 用加框前的旧版本号确认第 2 页 -> 409
r = client.post(f"/api/{rid}/review/confirm", json={"page": 1, "version": 1})
check("旧版本号确认 409", r.status_code == 409 and "重新" in r.json()["detail"])

# 重新烘焙（拿到 v2）再确认
rb2 = client.get(f"/api/{rid}/page/1.png?zoom=2&redacted=true")
v1 = int(rb2.headers["X-Box-Version"])
check("重新烘焙版本=2", v1 == 2)
# 烘焙画面框内确实是黑的（确认的就是这张图）
check("复核画面框内=黑", px(fitz.Pixmap(io.BytesIO(rb2.content)), 200, 80) == (0, 0, 0))
r = client.post(f"/api/{rid}/review/confirm", json={"page": 1, "version": v1})
check("确认第2页 200 且全部完成",
      r.status_code == 200 and r.json()["allConfirmed"] is True, str(r.status_code))

# 非法页码
r = client.post(f"/api/{rid}/review/confirm", json={"page": 99, "version": 1})
check("确认非法页码 400/422", r.status_code in (400, 422))

print("== 12. 定版 ZIP：PDF + 清单，SHA-256 与确定性 ==")
r = client.get(f"/api/{rid}/review/delivery?filename=rev.pdf")
check("定版交付 200", r.status_code == 200, str(r.status_code))
zip1 = r.content
header_sha = r.headers.get("X-Redacted-Pdf-Sha256")
zf = zipfile.ZipFile(io.BytesIO(zip1))
check("ZIP 含 PDF 与 manifest",
      sorted(zf.namelist()) == ["manifest.json", "rev-redacted.pdf"],
      str(zf.namelist()))
mani = json.loads(zf.read("manifest.json").decode("utf-8"))
pdf_in_zip = zf.read("rev-redacted.pdf")
calc_sha = hashlib.sha256(pdf_in_zip).hexdigest()
check("清单 PDF 文件名正确", mani["redacted_pdf"] == "rev-redacted.pdf")
check("清单 SHA-256 与包内 PDF 实际摘要一致",
      mani["redacted_pdf_sha256"] == calc_sha, calc_sha[:16])
check("响应头 SHA-256 与清单一致", header_sha == calc_sha, header_sha)
check("清单记录每页确认框数 0/1",
      [p["confirmed_box_count"] for p in mani["pages"]] == [0, 1],
      str(mani["pages"]))
check("清单记录页数/DPI", mani["page_count"] == 2 and mani["render_dpi"] == 200)
d = fitz.open(stream=pdf_in_zip, filetype="pdf")
check("交付 PDF 框内=黑/框外=灰",
      px(d[1].get_pixmap(matrix=fitz.Matrix(2, 2)), 200, 80) == (0, 0, 0)
      and px(d[1].get_pixmap(matrix=fitz.Matrix(2, 2)), 200, 300)[0] > 200)
check("交付 PDF 无文字层",
      "".join(p.get_text() for p in d).strip() == "")
d.close()

# 不做任何修改，重复下载 -> 完全相同的 ZIP（同一版确认与 PDF）
zip2 = client.get(f"/api/{rid}/review/delivery?filename=rev.pdf").content
check("无修改重复下载 ZIP 字节相同", zip1 == zip2)
check("重复下载摘要一致",
      hashlib.sha256(zip2).hexdigest() == hashlib.sha256(zip1).hexdigest())

print("== 13. 复核后编辑：仅受影响页失效，旧交付包立即撤销 ==")
# 起点：第1页已确认 v1(0 框)，第2页已确认 v2(1 框)，且 12 节已生成交付包。
# (a) 在第 1 页加红框
client.post(f"/api/{rid}/boxes", json={
    "page": 0, "rect": {"x0": 10, "y0": 10, "x1": 100, "y1": 100, "color": "red"}})
st = client.get(f"/api/{rid}/state").json()["review"]
check("受影响页确认失效", st["pages"]["0"]["confirmed"] is False
      and st["pages"]["0"]["boxVersion"] == 2
      and st["pages"]["0"]["boxCount"] == 1)
check("其他页确认保持有效", st["pages"]["1"]["confirmed"] is True)
check("整体不再全部确认", st["allConfirmed"] is False)
# 旧 ZIP 不得再被下发（尚未全部复核 -> 409），过期包不可下载
r = client.get(f"/api/{rid}/review/delivery")
check("失效期间旧交付包不可下载 409", r.status_code == 409)

# (b) 删掉刚加的框：仍然只失效第 1 页（版本继续递增），第 2 页确认不动
bid = client.get(f"/api/{rid}/state").json()["boxes"]["0"][0]["id"]
client.post(f"/api/{rid}/boxes/delete", json={"page": 0, "id": bid})
st = client.get(f"/api/{rid}/state").json()["review"]
check("删框只失效该页",
      st["pages"]["0"]["confirmed"] is False
      and st["pages"]["0"]["boxVersion"] == 3
      and st["pages"]["0"]["boxCount"] == 0
      and st["pages"]["1"]["confirmed"] is True
      and st["allConfirmed"] is False)

# (c) 重新烘焙并确认第 1 页（0 框，v3）；再对这个"空页"执行清空 ——
#     内容没有变化，版本与确认都应保留
rb3 = client.get(f"/api/{rid}/page/0.png?zoom=2&redacted=true")
check("空页重新烘焙版本=3", rb3.headers["X-Box-Version"] == "3")
client.post(f"/api/{rid}/review/confirm",
            json={"page": 0, "version": int(rb3.headers["X-Box-Version"])})
client.post(f"/api/{rid}/boxes/clear", json={"page": 0})
st = client.get(f"/api/{rid}/state").json()["review"]
check("清空本就为空的页不影响其确认与版本",
      st["pages"]["0"]["confirmed"] is True
      and st["pages"]["0"]["confirmedVersion"] == 3)

# (d) 清空"有框"的第 2 页：第 2 页失效，第 1 页不动
client.post(f"/api/{rid}/boxes/clear", json={"page": 1})
st = client.get(f"/api/{rid}/state").json()["review"]
check("清空有框页使其失效",
      st["pages"]["1"]["confirmed"] is False
      and st["pages"]["1"]["boxVersion"] == 3
      and st["pages"]["1"]["boxCount"] == 0)
check("此时仅第1页确认", st["pages"]["0"]["confirmed"] is True
      and st["allConfirmed"] is False)

# 重新复核第 2 页（现在 0 框），再取交付包：必须对应新确认而非旧包
rr = client.get(f"/api/{rid}/page/1.png?zoom=2&redacted=true")
client.post(f"/api/{rid}/review/confirm",
            json={"page": 1, "version": int(rr.headers["X-Box-Version"])})
r = client.get(f"/api/{rid}/review/delivery?filename=rev.pdf")
check("补齐复核后恢复交付 200", r.status_code == 200)
znew = zipfile.ZipFile(io.BytesIO(r.content))
mani_new = json.loads(znew.read("manifest.json").decode("utf-8"))
check("新包对应新版确认（两页确认框数均为 0）",
      [x["confirmed_box_count"] for x in mani_new["pages"]] == [0, 0],
      str(mani_new["pages"]))
check("新 PDF 摘要与旧包不同（旧包第2页有框）",
      mani_new["redacted_pdf_sha256"] != calc_sha)
# 新版停止修改后重复下载仍然逐字节相同
zip_a = r.content
zip_b = client.get(f"/api/{rid}/review/delivery?filename=rev.pdf").content
check("新版无修改重复下载 ZIP 字节相同", zip_a == zip_b)

print("== 14. 普通下载/预览/Compose 启动方式保持可用 ==")
r = client.get(f"/api/{rid}/download?filename=rev.pdf")
check("普通下载始终可用（无需复核）", r.status_code == 200
      and r.headers["content-type"] == "application/pdf")
check("干净/烘焙预览均可用",
      client.get(f"/api/{rid}/page/0.png?zoom=1.5&redacted=false").status_code == 200
      and client.get(f"/api/{rid}/page/0.png?zoom=1.5&redacted=true").status_code == 200)

print(f"\n结果：{len(PASS)} 通过，{len(FAIL)} 失败")
if FAIL:
    print("失败项：", FAIL)
    sys.exit(1)
print("全部通过 ✅")

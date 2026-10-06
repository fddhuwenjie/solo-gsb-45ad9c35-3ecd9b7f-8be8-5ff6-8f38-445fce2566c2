"""逐页复核 / 定版交付的端到端测试（不进镜像，仅开发环境用）。"""
import hashlib
import io
import json
import sys
import zipfile

import fitz
from fastapi.testclient import TestClient

sys.path.insert(0, "/workspace")
from app.main import app  # noqa: E402

client = TestClient(app)
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail else ""))


def make_pdf(path, *, pages=3):
    doc = fitz.open()
    for i in range(pages):
        w, h = 595, 842
        page = doc.new_page(width=w, height=h)
        page.draw_rect(fitz.Rect(0, 0, w, h), color=(0.9, 0.9, 0.9),
                       fill=(0.9, 0.9, 0.9), width=0)
        for j in range(6):
            page.insert_text((72, 72 + j * 40),
                             f"SECRET page {i + 1} line {j} TOPSECRET",
                             fontsize=24, color=(0.1, 0.1, 0.5))
    doc.save(path)
    doc.close()


def px(pix, x_pt, y_pt, zoom=2):
    x, y = int(x_pt * zoom), int(y_pt * zoom)
    off = y * pix.stride + x * pix.n
    return tuple(pix.samples[off:off + 3])


def upload(pages=3, path="/tmp/review.pdf"):
    make_pdf(path, pages=pages)
    with open(path, "rb") as f:
        r = client.post("/api/upload",
                        files={"file": (path.rsplit("/", 1)[-1], f, "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()


def baked_version(sid, page):
    r = client.get(f"/api/{sid}/page/{page}.png?zoom=1&redacted=true")
    assert r.status_code == 200
    return int(r.headers["X-Box-Version"]), r.content


def confirm(sid, page, version):
    return client.post(f"/api/{sid}/review/confirm",
                       json={"page": page, "version": version})


print("== 1. state 初始：全部待复核，版本为 0 ==")
up = upload()
sid = up["session"]
st = client.get(f"/api/{sid}/state").json()
check("versions 全 0", all(st["versions"][str(i)] == 0 for i in range(3)))
check("reviews 全未确认",
      all(st["reviews"][str(i)]["confirmed"] is False for i in range(3)))
check("暂无交付包", st["delivery"] is None)

print("== 2. 确认必须绑定当时烘焙预览的版本；干净预览也带版本头 ==")
r = client.get(f"/api/{sid}/page/0.png?zoom=1&redacted=false")
check("干净预览也返回 X-Box-Version", r.headers.get("X-Box-Version") == "0")

v, _ = baked_version(sid, 0)
r = confirm(sid, 0, v)
check("版本匹配 -> 200", r.status_code == 200 and r.json()["boxCount"] == 0, str(r.status_code))
check("回包确认状态为真", r.json()["reviews"]["0"]["confirmed"] is True)

r = confirm(sid, 1, 999)
check("错误版本 -> 409", r.status_code == 409, str(r.status_code))

print("== 3. 预览后框被其他请求修改：旧版本确认 409，须重新预览 ==")
v1, _ = baked_version(sid, 1)
client.post(f"/api/{sid}/boxes", json={
    "page": 1, "rect": {"x0": 60, "y0": 52, "x1": 470, "y1": 110, "color": "black"}})
r = confirm(sid, 1, v1)
check("旧画面确认被拒 409", r.status_code == 409, str(r.status_code))
st = client.get(f"/api/{sid}/state").json()
check("第 2 页版本推进为 1", st["versions"]["1"] == 1)
check("第 1 页确认仍然有效", st["reviews"]["0"]["confirmed"] is True)

v2, _ = baked_version(sid, 1)
r = confirm(sid, 1, v2)
check("重新预览后确认 200", r.status_code == 200 and r.json()["boxCount"] == 1)

print("== 4. 无框页面也必须确认；未全部确认不能生成定版包 ==")
r = client.post(f"/api/{sid}/delivery", params={"filename": "review.pdf"})
check("有未确认页 -> 409", r.status_code == 409, str(r.status_code))
check("409 指明待确认页", "3" in r.json()["detail"], r.json()["detail"])

v, _ = baked_version(sid, 2)
r = confirm(sid, 2, v)
check("无框页确认 200", r.status_code == 200 and r.json()["boxCount"] == 0)
st = client.get(f"/api/{sid}/state").json()
check("三页全部确认", all(st["reviews"][str(i)]["confirmed"] for i in range(3)))

print("== 5. 生成定版 ZIP：含位图遮盖 PDF 与 manifest，SHA-256 匹配 ==")
r = client.post(f"/api/{sid}/delivery", params={"filename": "review.pdf"})
check("生成 200", r.status_code == 200, str(r.status_code))
info = r.json()
did = info["deliveryId"]
check("zip 文件名", info["zipName"] == "review-reviewed.zip", info["zipName"])
check("回包标记未撤销", info["revoked"] is False)

z = client.get(f"/api/{sid}/delivery/{did}")
check("下载 200", z.status_code == 200 and z.headers["content-type"] == "application/zip")
zf = zipfile.ZipFile(io.BytesIO(z.content))
check("ZIP 成员为 PDF + manifest",
      sorted(zf.namelist()) == ["manifest.json", "review-redacted.pdf"],
      str(zf.namelist()))
pdf_bytes = zf.read("review-redacted.pdf")
manifest = json.loads(zf.read("manifest.json"))
check("manifest.pdfSha256 == 实际 PDF 摘要",
      manifest["pdfSha256"] == hashlib.sha256(pdf_bytes).hexdigest(),
      manifest["pdfSha256"][:16])
check("manifest 记录各页确认框数 0/1/0",
      [p["confirmedBoxCount"] for p in manifest["pages"]] == [0, 1, 0],
      str(manifest["pages"]))
check("manifest 记录各页版本 0/1/0",
      [p["confirmedVersion"] for p in manifest["pages"]] == [0, 1, 0])

red = fitz.open(stream=pdf_bytes, filetype="pdf")
check("包内 PDF 第 2 页框内=黑",
      px(red[1].get_pixmap(matrix=fitz.Matrix(2, 2)), 200, 80) == (0, 0, 0))
check("包内 PDF 第 2 页框外=浅灰",
      px(red[1].get_pixmap(matrix=fitz.Matrix(2, 2)), 200, 400)[0] > 200)
red.close()

print("== 6. 不再修改时重复生成/下载：相同 PDF、相同 ZIP、相同摘要 ==")
z2 = client.get(f"/api/{sid}/delivery/{did}").content
check("重复下载 ZIP 字节一致", z2 == z.content)
info2 = client.post(f"/api/{sid}/delivery",
                    params={"filename": "review.pdf"}).json()
check("重复生成复用同一包 id", info2["deliveryId"] == did)
check("SHA-256 保持不变", info2["pdfSha256"] == info["pdfSha256"])

print("== 7. 加框只使受影响页失效并立即撤销旧包；其他页确认保持 ==")
client.post(f"/api/{sid}/boxes", json={
    "page": 0, "rect": {"x0": 10, "y0": 10, "x1": 90, "y1": 90, "color": "red"}})
st = client.get(f"/api/{sid}/state").json()
check("第 1 页确认失效", st["reviews"]["0"]["confirmed"] is False)
check("第 1 页版本=1", st["reviews"]["0"]["version"] == 1)
check("第 2 页确认仍有效", st["reviews"]["1"]["confirmed"] is True)
check("第 3 页确认仍有效", st["reviews"]["2"]["confirmed"] is True)
check("旧交付包标记 revoked", st["delivery"]["revoked"] is True)

old = client.get(f"/api/{sid}/delivery/{did}")
check("旧包下载被拒 410", old.status_code == 410, str(old.status_code))
r = client.post(f"/api/{sid}/delivery", params={"filename": "review.pdf"})
check("未重新复核不能生成新包 409", r.status_code == 409)

v, _ = baked_version(sid, 0)
confirm(sid, 0, v)
r = client.post(f"/api/{sid}/delivery", params={"filename": "review.pdf"})
check("重新确认后生成新包 200", r.status_code == 200, str(r.status_code))
new_info = r.json()
check("新包 id 不同", new_info["deliveryId"] != did)
check("新 PDF 摘要与旧 PDF 不同（红框页变了）",
      new_info["pdfSha256"] != info["pdfSha256"])
new_zip = client.get(f"/api/{sid}/delivery/{new_info['deliveryId']}").content
new_zf = zipfile.ZipFile(io.BytesIO(new_zip))
new_manifest = json.loads(new_zf.read("manifest.json"))
check("新清单框数 1/1/0",
      [p["confirmedBoxCount"] for p in new_manifest["pages"]] == [1, 1, 0])

print("== 8. 删框 / 清空：仅受影响页失效；空清空不推进版本 ==")
# 删第 2 页那个框 -> 第 2 页失效，第 1、3 页不受影响
box_id = client.get(f"/api/{sid}/state").json()["boxes"]["1"][0]["id"]
client.post(f"/api/{sid}/boxes/delete", json={"page": 1, "id": box_id})
st = client.get(f"/api/{sid}/state").json()
check("删框后第 2 页失效", st["reviews"]["1"]["confirmed"] is False)
check("删框后第 1、3 页仍有效",
      st["reviews"]["0"]["confirmed"] and st["reviews"]["2"]["confirmed"])
check("旧包再次被撤销", st["delivery"]["revoked"] is True)
# 清空一个本来就没有框的页：版本不变，确认不丢
v_before = st["reviews"]["2"]["version"]
client.post(f"/api/{sid}/boxes/clear", json={"page": 2})
st = client.get(f"/api/{sid}/state").json()
check("清空空页：版本不变", st["versions"]["2"] == v_before)
check("清空空页：确认保留", st["reviews"]["2"]["confirmed"] is True)

print("== 9. 不存在的交付包 -> 404 ==")
check("未知包 404", client.get(f"/api/{sid}/delivery/deadbeef").status_code == 404)

print("== 10. 复核 / 生成 / 编辑交错：下载只对应同一版确认与 PDF ==")
# 重新确认第 2 页（删框后 0 框），全确认，生成包；再清空第 1 页
v, _ = baked_version(sid, 1)
confirm(sid, 1, v)
info_a = client.post(f"/api/{sid}/delivery",
                     params={"filename": "review.pdf"}).json()
# 生成之后、下载之前，另一请求清空第 1 页
client.post(f"/api/{sid}/boxes/clear", json={"page": 0})
r = client.get(f"/api/{sid}/delivery/{info_a['deliveryId']}")
check("生成后被编辑：下载 410，拿不到过期包", r.status_code == 410, str(r.status_code))
# 全部重新确认后再生成；之后不动，两次生成必须给同一包
v0, _ = baked_version(sid, 0)
confirm(sid, 0, v0)
b1 = client.post(f"/api/{sid}/delivery", params={"filename": "x.pdf"}).json()
b2 = client.post(f"/api/{sid}/delivery", params={"filename": "x.pdf"}).json()
check("无新修改时包 id 稳定", b1["deliveryId"] == b2["deliveryId"])
zip_b = client.get(f"/api/{sid}/delivery/{b1['deliveryId']}").content
check("无新修改时 ZIP 字节稳定",
      zip_b == client.get(f"/api/{sid}/delivery/{b2['deliveryId']}").content)

print(f"\n结果：{len(PASS)} 通过，{len(FAIL)} 失败")
if FAIL:
    print("失败项：", FAIL)
    sys.exit(1)
print("全部通过 ✅")

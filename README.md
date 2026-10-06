# PDF 遮盖工作台（本地 Redaction Workbench）

一个完全本地运行的 PDF 遮盖（涂黑/redaction）工具：上传最多 8 页的 PDF，
逐页预览、画出或删除**不透明遮盖框**，然后下载一份与当前预览一致的、
由**整页位图重新组成**的 PDF。

技术栈：**Python · FastAPI · PyMuPDF · 原生 JavaScript**（无前端构建、无外部 CDN）。

## 一条命令启动

需要本机已安装 Docker（含 Compose 插件）。

```bash
docker compose up --build
```

启动后在浏览器打开：

> **http://localhost:8000**

- 本机访问地址：`http://localhost:8000`
- 监听端口：容器内 `8000`，映射到宿主机 `8000`
- 若 8000 端口被占用，编辑 `docker-compose.yml` 中 `"8000:8000"`
  的左侧端口（例如 `"8080:8000"`），再访问 `http://localhost:8080`。

停止：`Ctrl+C`；后台运行：`docker compose up --build -d`；
查看日志：`docker compose logs -f`；停止并删除容器：`docker compose down`。

## 使用方法

1. 点击左侧 **选择 PDF**（1–8 页，≤100MB，未加密）。
2. **✏️ 画框**：在页面上按住鼠标拖出矩形，松开即添加；可切换黑/白/红/蓝/绿/黄。
3. **🩹 删除**：点击已有框即可删除；画框模式下悬停某框按 `Delete` 也可删除。
   侧栏列出本页全部遮盖框，可逐条删除或"清空本页所有框"。
4. 顶部按钮或 `←` `→` 翻页；`+` `−`、Ctrl+滚轮或"适应"按钮缩放
   —— **框以 PDF 点坐标存储，任意缩放下始终锚定相同内容**。
5. 勾选 **最终效果预览（服务器烘焙）** 可查看服务器端按下载路径生成的实际像素效果。
6. 点击右上角 **⬇ 下载遮盖副本**，得到 `原名-redacted.pdf`。

快捷键：`D` 画框 / `E` 删除 / `←→` 翻页 / `+−` 缩放 / `Delete` 删除悬停框。

## 输出 PDF 的安全保证

下载的副本**不是**在原页面上叠一个色块，而是按以下方式重建：

1. 原 PDF 的**每一页**（包括没有任何遮盖框的页面）都以 **200 DPI** 重新光栅化为位图；
2. 遮盖框直接**就地覆写位图字节**（含像素行填充，alpha 一并写成不透明），
   框内原像素被覆盖，无法从副本中恢复；
3. 用这些位图铺满全新页面，组成一份新的 PDF。

因此副本中：

- ❌ 不含原 PDF 的文字层（无法选中/搜索/复制遮盖区域的文字）；
- ❌ 不含批注、超链接、表单域、书签、附件（嵌入文件）；
- ❌ 不携带 Info 元数据与 XMP 元数据（标题、作者、制作工具等全部清空）；
- ✅ 保留原页数，每页可见尺寸（CropBox 显示尺寸）与方向（含旋转页）一致；
- ❌ 不是"在原页上叠色块"，遮盖区域没有任何可被移除的覆盖层。

> 注意：200 DPI 位图是栅格画面，放大到很大时会有正常的位图锯齿。
> 这是"像素不可恢复"的必然代价。

### 旋转页 / 裁切页为什么也准确

所有框都保存在**显示坐标系**（用户看到的、方向转正后的页面，单位 1/72 英寸点）。
光栅化时通过 PyMuPDF 的 `page.derotation_matrix` 把显示坐标换算回页面原始
(MediaBox/CropBox) 坐标再映射到位图像素；新页面尺寸取 `page.rect`
（即应用 rotation 后的显示尺寸）。因此 90/180/270 度旋转页、被 CropBox
裁切过的页面，框都能精确落在相同内容上。

## 输入校验（不覆盖正在编辑的文档）

- 非 PDF、文件损坏、内容流无法渲染 → 明确报错；
- 加密或带密码的 PDF（即使是空密码）→ 明确拒绝；
- 0 页或超过 8 页 → 明确拒绝；
- **只有当新文件通过全部校验后，前端才会切换会话**；上传失败时当前正在
  编辑的文档与所有遮盖框原样保留。

## 本地直接运行（不用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 隐私

文档仅保存在服务进程内存中，不写磁盘、不挂载任何卷；会话闲置 1 小时或
容器重启即消失。该工具设计为**本地单机**使用，默认也只建议绑定本机访问
（Compose 默认发布到所有接口；如需仅本机可访问，将端口改为
`"127.0.0.1:8000:8000"`）。

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py            # FastAPI 路由、校验、光栅化与副本生成
│   └── static/
│       └── index.html     # 原生 JS 单页前端（画布 + 坐标映射）
├── tests/
│   └── smoke_test.py      # 后端端到端测试（安装依赖后运行）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

运行后端测试：

```bash
pip install -r requirements.txt
python tests/smoke_test.py   # 覆盖：旋转/裁切页、像素覆写、文字层/元数据/附件清除、
                             #       加密/无效/超页数报错、报错不覆盖当前会话
```

## API 概览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/upload` | 上传并校验 PDF，返回 session id 与页面信息 |
| GET  | `/api/{sid}/state` | 当前各页遮盖框 |
| GET  | `/api/{sid}/page/{n}.png?zoom=&redacted=` | 页面预览（干净 / 烘焙后） |
| POST | `/api/{sid}/boxes` | 添加遮盖框（显示坐标点） |
| POST | `/api/{sid}/boxes/delete` | 删除指定框 |
| POST | `/api/{sid}/boxes/clear` | 清空某页 |
| GET  | `/api/{sid}/download` | 生成并下载位图重组的遮盖副本 |
| GET  | `/api/health` | 健康检查 |

# 修改指引（Demo 图片 / 文案 / 部署脚本）

本项目所有 Demo 都由 `demos.json` 驱动。页面图片与文案可以不改代码，仅改配置和静态资源即可。

## 1) 修改 Demo 图片

目录约定：
- 列表缩略图：`pc_server/app/static/demos/<demo_id>/thumb.png`
- 详情预览图：`pc_server/app/static/demos/<demo_id>/preview.png`

配置位置：`demos.json` → `media` 字段（可扩展多张）：
```json
"media": [
  {"type": "image", "src": "/static/demos/yolo_demo/preview.png", "alt": "..."},
  {"type": "image", "src": "/static/demos/yolo_demo/thumb.png", "alt": "..."}
]
```

注意：
- `src` 必须以 `/static/` 开头，对应 `pc_server/app/static/` 目录。
- 列表页默认取 `media[1]` 作为缩略图，如果只配置一张会自动用 `media[0]`。

## 2) 修改 Demo 列表与详情文案

Demo 核心文案来自 `demos.json`：
- `name`：列表卡片标题 + 详情页标题
- `description`：列表卡片描述 + 详情页副标题
- `tags`：列表卡片标签 + 详情页标签行

示例：
```json
{"id":"yolo_demo","name":"YOLO Realtime Detection","description":"...","tags":["Jetson","Camera","YOLO"]}
```

## 3) 修改页面固定提示文字

这些是模板内固定文案（非 demo 配置）：

### 首页文案
- `pc_server/app/templates/index.html`
  - Hero 标题、副标题
  - “Explore Demos”等按钮文案

### 详情页提示文案
- `pc_server/app/templates/demo_detail.html`
  - 连接要求 / 部署内容 / 运行提示
  - A/B/C 面板标题与说明

### 交互状态提示（连接/部署/运行）
- `pc_server/app/static/app.js`
  - 连接成功/失败、部署进度/失败、运行错误等提示

## 4) 修改主题与样式

- 全局主题 token：`pc_server/app/static/styles.css` 顶部 `:root` 变量
- 面板/按钮/输入框样式均基于 token，建议优先调 token 再微调局部

## 5) 部署脚本来源（GitHub 仓库）

当前部署脚本来自以下仓库：
- `https://github.com/zibochen6/easy_develope_script`

配置位置：`demos.json` → `deploy` 字段：
```json
"deploy": {
  "script_repo": "https://github.com/zibochen6/easy_develope_script",
  "script_ref": "main",
  "script_path": "setup_yolo11.sh",
  "remote_dir": "/tmp/oneclick_demos/yolo_demo",
  "remote_script_name": "setup_yolo11.sh"
}
```

说明：
- `script_repo` + `script_ref` + `script_path` 会自动拼接为 GitHub Raw 下载地址。
- 只要维护该仓库脚本，平台部署就会拉取最新版本。

## 6) 新增一个 Demo 的最小步骤

1. 在 `demos.json` 追加 demo 配置（含 `id` / `name` / `description` / `deploy` / `run`）。
2. 在 `pc_server/app/static/demos/<demo_id>/` 放入 `preview.png` 与 `thumb.png`。
3. 如需运行流式服务，配置 `run` 字段并准备 Jetson payload。

---

如果你希望我把文案改成可在配置中完全自定义（包括右侧提示卡），告诉我我会把这些文本迁移到 `demos.json` 并统一渲染。

# Jetson Demo Web 平台（PC 端 + 双模式）

在 **PC/服务器** 上运行本 Web 服务，通过浏览器连接 Jetson 后一键部署 Demo 并查看实时推流；也可在 **Jetson 本机** 运行，使用本地模式。

## 安装与运行

```bash
cd web
pip install -r requirements.txt
python main.py
```

或：`uvicorn main:app --host 0.0.0.0 --port 8000`

浏览器访问：`http://<PC_IP>:8000`

## 双模式

- **未连接 Jetson**：部署与推流在**本机**执行（需本机有 `setup_yolo11.sh`、`yolo11/.venv` 等，即本机为 Jetson 时使用）。
- **已连接 Jetson**：在首页输入 Jetson IP、SSH 用户名与密码，点击「连接」；之后的「一键部署」与「运行 Demo」均在 **Jetson 上** 通过 SSH 执行，日志与视频流经 PC 后端推给浏览器。

## Jetson 侧准备

- 确保 SSH 可达（`ssh user@<Jetson_IP>` 可登录）。
- 在 Jetson 上已有项目目录（默认 `/home/seeed/setup`），内含：
  - `setup_yolo11.sh`（一键部署脚本）
  - `web/stream_yolo.py`
  - 部署完成后会有 `yolo11/.venv`、`yolo11/yolo11n.pt`。

## API 概要

- `POST /api/connect`：连接 Jetson（host, port, username, password, jetson_project_path）。
- `GET /api/connect/status`：当前是否已连接。
- `GET /api/demos`：Demo 列表。
- `GET /api/demos/{id}/status`：是否已部署（本地或远程）。
- `POST /api/demos/{id}/deploy`：一键部署（本地或 SSH），返回 `stream_url`。
- `GET /api/demos/{id}/deploy/stream`：SSE 部署日志。
- `GET /api/demos/{id}/stream`：MJPEG 视频流（本地或 SSH）。
- `GET /api/demos/{id}/stream-last-error`：最近一次推流错误信息。

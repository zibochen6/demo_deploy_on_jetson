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

## 停止服务与端口释放

使用 `python main.py` 启动时，按 **Ctrl+C** 会触发清理（关闭 SSH、结束部署/推流子进程）并退出，端口会随之释放。若重启时仍提示端口被占用，可等待约 60 秒（TCP TIME_WAIT）或先查占用的进程再结束：

```bash
# 查看占用 8000 端口的进程
lsof -i :8000
# 或
ss -tlnp | grep 8000

# 结束该进程（将 PID 换成实际进程号）
kill -9 <PID>
```

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
- `GET /api/demos/{id}/stream-debug`：调试用，返回最近错误、路径与可在 Jetson 上手动执行的命令预览（便于在 SSH 终端里复现问题）。

推流相关接口在服务端会打 INFO/WARNING 日志（如 `stream start`、`SFTP upload ok`、`first frame ok`、`503 exit_status/stderr`），便于排查。

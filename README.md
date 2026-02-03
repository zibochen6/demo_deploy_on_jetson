# Jetson 一键部署 + 实时推流 Web 平台（PC 端）

本项目在 **PC 端**运行 Web 平台，通过浏览器连接 Jetson，完成一键部署并实时观看 YOLO 检测视频流（MJPEG）。

## 目录结构

```
repo/
  README.md
  pc_server/
    app/
      main.py
      templates/
        index.html
        demo_detail.html
      static/
        app.js
        styles.css
      core/
        config.py
        session_manager.py
        ssh_client.py
        deploy_service.py
        run_service.py
        tunnel.py
        utils.py
    requirements.txt
  jetson_payload/
    yolo_stream_server.py
  demo_scripts/
    setup_yolo11.sh
  demos.json
```

## PC 端运行环境

- Python 3.10-3.12（推荐 3.11）
- 推荐使用 `venv`

### 安装与启动

> 注意：Python 3.13/3.14 目前会在安装 `pydantic-core` 时失败（PyO3 暂不支持），请使用 3.10-3.12。

### 一键启动

```bash
./start.sh
```

默认端口 8000，可通过环境变量覆盖：

```bash
HOST=0.0.0.0 PORT=8000 ./start.sh
```

```bash
cd pc_server
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

浏览器访问：`http://localhost:8000`

## Jetson 端前置要求

- Jetson 可被 PC 通过 SSH 访问
- 用户有 sudo 权限（部署脚本需要安装依赖）
- 摄像头已连接（USB 或 CSI）

## 使用步骤

1. 打开 `http://localhost:8000`
2. 进入 `YOLO Realtime Detection` demo
3. 填写 Jetson SSH 信息并点击 **连接**（若 sudo 密码不同可额外填写）
4. 点击 **一键部署**，观察日志直到显示 `部署完成 ✅`
5. 点击 **运行 Demo**，在页面中实时观看视频流
6. 点击 **停止 Demo** 释放资源

## 常见问题排查

- **SSH 连接失败**：确认 IP/端口/用户名/密码无误，Jetson SSH 服务可访问。
- **sudo 密码不一致**：确保 SSH 用户具有 sudo 权限，必要时在页面中填写 sudo 密码。
- **摄像头打不开**：检查 `/dev/video0` 是否存在、是否被占用；CSI 摄像头需确认 GStreamer pipeline。
- **缺包/安装失败**：部署脚本会安装依赖，如失败请重试或手动补齐。

## 自测流程（我执行的步骤）

1. 启动 PC 端服务 `uvicorn app.main:app --host 0.0.0.0 --port 8000`
2. 打开浏览器确认 `/` 与 `/demo/yolo_demo` 页面正常
3. 使用错误 SSH 信息测试连接失败提示
4. 使用正确 SSH 信息测试连接成功
5. 触发一键部署，确认 WebSocket 日志实时刷新
6. 部署完成后启动 Demo，确认 /health 可达，网页显示 MJPEG 流
7. 点击停止，确认推理进程被结束且端口释放

> 如果需要复现部署过程，请确保 Jetson 环境为 JetPack 6.2，且能访问 NVIDIA PyTorch 源。

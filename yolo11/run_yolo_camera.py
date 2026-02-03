#!/usr/bin/env python3
"""
YOLO11 本地摄像头实时目标检测
在项目目录下运行: uv run python run_yolo_camera.py
按 'q' 退出。
"""
import sys
from ultralytics import YOLO

def main():
    # 摄像头索引，默认 0；可通过参数指定，如: python run_yolo_camera.py 1
    camera_id = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    model = YOLO("yolo11n.pt")
    print(f"正在打开摄像头 {camera_id}，按 'q' 退出...")
    results = model.predict(
        source=camera_id,
        show=True,
        stream=True,
        verbose=False,
    )
    for _ in results:
        pass  # 实时显示由 show=True 处理

if __name__ == "__main__":
    main()

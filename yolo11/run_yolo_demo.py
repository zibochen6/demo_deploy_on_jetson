#!/usr/bin/env python3
"""YOLO11 推理示例（在项目目录下用 uv run python run_yolo_demo.py 运行）"""
from ultralytics import YOLO

model = YOLO("yolo11n.pt")
results = model("https://ultralytics.com/images/bus.jpg")
print("推理完成，结果数量:", len(results))

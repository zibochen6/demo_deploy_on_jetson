#!/usr/bin/env python3
"""
摄像头 + YOLO 推理循环，将每帧 JPEG 以「4 字节长度 + 原始 JPEG」写入 stdout，
供主进程封装为 MJPEG HTTP 流。在 Jetson 上由 yolo11/.venv 的 Python 运行。
"""
import os
import platform
import struct
import sys


def _err(msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


# 若在终端直接运行，stdout 会输出二进制 MJPEG，导致终端乱码；此处直接退出并提示
if sys.stdout.isatty():
    _err("stream_yolo 向 stdout 输出二进制 MJPEG，不适合在终端直接运行。请通过网页「运行 Demo」查看推流，或重定向: stream_yolo.py > /dev/null")
    sys.exit(0)

_err("stream_yolo starting...")
PROJECT_DIR = os.environ.get("YOLO11_PROJECT_DIR", "")
MODEL_PATH = os.environ.get("YOLO11_MODEL_PATH", "")
CAMERA_ID = int(os.environ.get("YOLO11_CAMERA_ID", "0"))

if not PROJECT_DIR or not MODEL_PATH:
    _err("YOLO11_PROJECT_DIR and YOLO11_MODEL_PATH must be set")
    sys.exit(1)
if not os.path.isfile(MODEL_PATH):
    _err(f"Model not found: {MODEL_PATH}")
    sys.exit(1)

os.chdir(PROJECT_DIR)
_err("loading cv2 and ultralytics...")
try:
    import cv2  # noqa: E402
    from ultralytics import YOLO  # noqa: E402
except Exception as e:
    _err(f"import error: {e}")
    raise


def _open_camera():
    cap = cv2.VideoCapture(CAMERA_ID)
    if cap.isOpened():
        return cap
    cap.release()
    if platform.machine() == "aarch64":
        gst = (
            "v4l2src device=/dev/video0 ! "
            "video/x-raw,width=640,height=480,framerate=30/1 ! "
            "videoconvert ! video/x-raw,format=BGR ! appsink"
        )
        cap = cv2.VideoCapture(gst, cv2.CAP_GSTREAMER)
        if cap.isOpened():
            return cap
        cap.release()
    raise RuntimeError("Cannot open camera")


def main() -> None:
    try:
        _err("Loading YOLO model...")
        model = YOLO(MODEL_PATH)
        cap = _open_camera()
        _err("Streaming frames...")
        try:
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                results = model(frame, verbose=False)
                frame_annotated = results[0].plot()
                _, jpeg = cv2.imencode(".jpg", frame_annotated)
                jpeg_bytes = jpeg.tobytes()
                sys.stdout.buffer.write(struct.pack(">I", len(jpeg_bytes)))
                sys.stdout.buffer.write(jpeg_bytes)
                sys.stdout.buffer.flush()
        finally:
            cap.release()
    except Exception as e:
        _err(f"stream_yolo error: {e}")
        raise


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
YOLO camera inference + MJPEG streaming server for Jetson.
"""
from __future__ import annotations

import argparse
import queue
import threading
import time
from typing import Optional

import cv2
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from ultralytics import YOLO

app = FastAPI()


class VideoStreamer:
    def __init__(self, camera: str, usb_index: int, width: int, height: int, flip: int, model_path: str):
        self.camera = camera
        self.usb_index = usb_index
        self.width = width
        self.height = height
        self.flip = flip
        self.model_path = model_path
        self._thread: Optional[threading.Thread] = None
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._model: Optional[YOLO] = None
        self._cap: Optional[cv2.VideoCapture] = None
        self.error: Optional[str] = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._model = YOLO(self.model_path)
            self._cap = self._open_camera()
            self._ready.set()
            while not self._stop.is_set():
                ret, frame = self._cap.read()
                if not ret:
                    time.sleep(0.05)
                    continue
                results = self._model(frame, verbose=False)
                frame = results[0].plot()
                ok, jpeg = cv2.imencode(".jpg", frame)
                if not ok:
                    continue
                data = jpeg.tobytes()
                if self._queue.full():
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        pass
                try:
                    self._queue.put_nowait(data)
                except queue.Full:
                    pass
        except Exception as exc:
            self.error = str(exc)
        finally:
            if self._cap:
                self._cap.release()
            self._ready.set()

    def _open_camera(self) -> cv2.VideoCapture:
        if self.camera == "usb":
            cap = cv2.VideoCapture(self.usb_index)
            if cap.isOpened():
                return cap
            cap.release()
        if self.camera == "csi":
            pipeline = self._get_gstreamer_pipeline()
            cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
            if cap.isOpened():
                return cap
            cap.release()
        raise RuntimeError("Cannot open camera")

    def _get_gstreamer_pipeline(self) -> str:
        return (
            f"nvarguscamerasrc ! "
            f"video/x-raw(memory:NVMM), width={self.width}, height={self.height}, "
            f"format=NV12, framerate=30/1 ! "
            f"nvvidconv flip-method={self.flip} ! "
            f"video/x-raw, format=BGRx ! "
            f"videoconvert ! video/x-raw, format=BGR ! appsink"
        )

    def get_frame(self, timeout: float = 1.0) -> Optional[bytes]:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        self._stop.set()

    def ready(self) -> bool:
        return self._ready.is_set() and self.error is None


streamer: Optional[VideoStreamer] = None


@app.get("/health")
def health():
    if streamer is None:
        raise HTTPException(status_code=503, detail="streamer not initialized")
    if streamer.error:
        raise HTTPException(status_code=500, detail=streamer.error)
    if not streamer.ready():
        raise HTTPException(status_code=503, detail="starting")
    return {"status": "ok"}


@app.get("/video")
def video():
    if streamer is None:
        raise HTTPException(status_code=503, detail="streamer not initialized")

    boundary = "frame"

    def generate():
        while True:
            if streamer.error:
                break
            frame = streamer.get_frame(timeout=5.0)
            if frame is None:
                continue
            yield (
                b"--" + boundary.encode() + b"\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n"
            )

    return StreamingResponse(generate(), media_type=f"multipart/x-mixed-replace; boundary={boundary}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--camera", choices=["usb", "csi"], default="usb")
    parser.add_argument("--usb-index", type=int, default=0)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--flip", type=int, default=0)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()

    global streamer
    streamer = VideoStreamer(
        camera=args.camera,
        usb_index=args.usb_index,
        width=args.width,
        height=args.height,
        flip=args.flip,
        model_path=args.model,
    )
    streamer.start()

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

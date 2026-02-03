"""
Demo 元数据与路径配置。
本地模式：路径相对于本项目根目录。
远程模式（Jetson）：路径为 Jetson 上的绝对路径，由连接时 jetson_project_path 指定。
"""
import os

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# 默认 Jetson 项目路径（远程模式）
DEFAULT_JETSON_PROJECT = "/home/seeed/setup"

DEMOS = {
    "yolo11": {
        "id": "yolo11",
        "name": "YOLO11 目标检测",
        "description": "本地摄像头实时目标检测，一键部署 PyTorch + Ultralytics 环境到 Jetson。",
        "script_name": "setup_yolo11.sh",
        "stream_script_name": "stream_yolo.py",
        "work_dir_name": "yolo11",
    },
}


def get_demo(demo_id: str) -> dict | None:
    return DEMOS.get(demo_id)


def list_demos() -> list[dict]:
    return [
        {"id": d["id"], "name": d["name"], "description": d["description"]}
        for d in DEMOS.values()
    ]


def local_script_path(demo_id: str) -> str:
    """本地模式：部署脚本绝对路径。"""
    d = get_demo(demo_id)
    if not d:
        return ""
    return os.path.join(PROJECT_ROOT, d["script_name"])


def local_work_dir(demo_id: str) -> str:
    """本地模式：工作目录（项目根）。"""
    return PROJECT_ROOT


def local_venv_path(demo_id: str) -> str:
    """本地模式：venv Python 路径。"""
    d = get_demo(demo_id)
    if not d:
        return ""
    return os.path.join(PROJECT_ROOT, d["work_dir_name"], ".venv", "bin", "python")


def local_model_path(demo_id: str) -> str:
    """本地模式：模型路径。"""
    d = get_demo(demo_id)
    if not d:
        return ""
    return os.path.join(PROJECT_ROOT, d["work_dir_name"], "yolo11n.pt")


def jetson_paths(jetson_project: str, demo_id: str) -> dict:
    """远程模式：Jetson 上脚本、工作目录、venv、模型路径。"""
    d = get_demo(demo_id)
    if not d:
        return {}
    return {
        "script_path": os.path.join(jetson_project, d["script_name"]),
        "work_dir": jetson_project,
        "yolo_dir": os.path.join(jetson_project, d["work_dir_name"]),
        "venv_python": os.path.join(jetson_project, d["work_dir_name"], ".venv", "bin", "python"),
        "model_path": os.path.join(jetson_project, d["work_dir_name"], "yolo11n.pt"),
        "stream_script": os.path.join(jetson_project, "web", d["stream_script_name"]),
    }


def is_deployed_local(demo_id: str) -> bool:
    """本地模式：是否已部署（venv + 模型存在）。"""
    venv = local_venv_path(demo_id)
    model = local_model_path(demo_id)
    return venv and os.path.isfile(venv) and model and os.path.isfile(model)

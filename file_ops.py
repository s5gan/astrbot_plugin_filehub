import os
from pathlib import Path


"""
file_ops 模块

职责：
- 文件类型与合法性判断（图片魔数校验等）；
- 路径工具（绝对路径、file:// URI、文件大小）。
"""

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def is_image(path: str) -> bool:
    """是否为常见图片类型（基于扩展名）"""
    return os.path.splitext(path)[1].lower() in IMAGE_EXTS


def is_valid_image_file(path: str) -> bool:
    """快速图片魔数校验，防止占位/损坏图片导致发送失败"""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return True
        if head.startswith(b"\xFF\xD8"):
            return True
        if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
            return True
        if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return True
        if head.startswith(b"BM"):
            return True
        return False
    except Exception:
        return False


def detect_extension_by_magic(path: str) -> str:
    """通过文件魔数猜测常见后缀。

    覆盖：png/jpg/gif/webp/bmp/pdf（可按需扩展）。
    返回带点的扩展名，未知返回空字符串。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(16)
        # 图片类
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if head.startswith(b"\xFF\xD8"):
            return ".jpg"
        if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
            return ".gif"
        if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return ".webp"
        if head.startswith(b"BM"):
            return ".bmp"
        # 文档类
        if head.startswith(b"%PDF"):
            return ".pdf"
    except Exception:
        pass
    return ""


def normalize_abs_path(root_dir: str, p: str) -> str:
    """将相对路径基于 root_dir 解析为绝对路径"""
    if os.path.isabs(p):
        return os.path.abspath(p)
    return os.path.abspath(os.path.join(root_dir, p))


def to_file_uri(local_path: str) -> str:
    """将本地路径转换为标准 file:// URI（避免多余斜杠）"""
    try:
        return Path(local_path).absolute().as_uri()
    except Exception:
        lp = os.path.abspath(local_path)
        if lp.startswith("/"):
            return f"file://{lp}"
        return f"file:///{lp}"


def get_file_size_mb(path: str) -> float:
    """返回文件大小（MB）。异常时返回 0.0。"""
    try:
        return os.path.getsize(path) / (1024 * 1024)
    except Exception:
        return 0.0

"""
registry 模块

职责：
- 解析并加载文件索引（registry.json）。
- 解耦主模块与具体文件操作，便于单元测试与替换实现。
"""

import os
import json
from typing import Dict, Tuple, Any


def resolve_registry_path(root_dir: str, registry_file: str) -> str:
    """返回实际使用的索引文件路径（仅支持 JSON）

    - 优先使用配置的 registry_file；
    - 若不存在，则回退到 root_dir 下的 registry.json。
    """
    candidate = (
        registry_file
        if os.path.isabs(registry_file)
        else os.path.join(root_dir, registry_file)
    )
    if os.path.exists(candidate):
        return candidate
    # 回退到默认 JSON
    json_path = os.path.join(root_dir, "registry.json")
    return json_path


def load_registry(root_dir: str, registry_file: str) -> Tuple[Dict[str, Any], str]:
    """加载索引并返回 (dict, path)

    - 若索引不存在或损坏，返回空结构 {"files": []} 与路径；
    - 调用者应始终使用返回的 path（便于 info/诊断显示实际文件）。
    """
    path = resolve_registry_path(root_dir, registry_file)
    if not os.path.exists(path):
        return {"files": []}, path
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
            if not isinstance(data, dict):
                return {"files": []}, path
            if "files" not in data or not isinstance(data["files"], list):
                data["files"] = []
            return data, path
    except Exception:
        return {"files": []}, path

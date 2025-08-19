"""
search 模块

职责：
- 提供基于关键词的简单排序搜索；
- 提供条目简要格式化文本（便于列表展示）。
"""

import os
from typing import Dict, List, Tuple


def search_entries(entries: List[Dict], query: str) -> List[Tuple[float, Dict]]:
    """对条目进行简单打分搜索

    规则（越靠前分越高）：
    - 完整匹配 id/文件名/name 加高分；
    - 包含关系次之；
    - 命中标签/描述再加分；
    - 返回 (score, entry) 列表，已按高到低排序。
    """
    q = (query or "").strip().lower()
    results: List[Tuple[float, Dict]] = []
    if not q:
        return [(0.0, e) for e in entries]
    for e in entries:
        score = 0.0
        id_ = str(e.get("id", "")).lower()
        name = str(e.get("name", "")).lower()
        desc = str(e.get("description", "")).lower()
        path = str(e.get("path", ""))
        filename = os.path.basename(path).lower()
        tags = [str(t).lower() for t in (e.get("tags") or [])]

        if q == id_ or q == filename or q == name:
            score += 10
        if q in id_:
            score += 6
        if q in filename:
            score += 5
        if q in name:
            score += 4
        if any(q in t for t in tags):
            score += 3
        if q in desc:
            score += 2
        if score > 0:
            results.append((score, e))
    results.sort(key=lambda x: x[0], reverse=True)
    return results


def format_entry_brief(entry: Dict) -> str:
    """将条目格式化为简要的一行文本，便于在列表中展示"""
    name = entry.get("name") or os.path.basename(str(entry.get("path", "")))
    desc = entry.get("description") or ""
    tags = entry.get("tags") or []
    return f"{entry.get('id','')} | {name} | {desc} | tags: {', '.join(map(str,tags))}"

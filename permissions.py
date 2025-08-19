"""
permissions 模块

职责：
- 统一实现条目权限判定逻辑：合并全局黑白名单、条目 allow/deny 与最终决策。
"""

from typing import Any, Dict, List


def norm_list_str(v: Any) -> List[str]:
    """将任意输入规整为字符串列表（用于处理配置/索引里的 ID 列表）"""
    if not v:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)]


def has_access(
    entry: Dict[str, Any],
    group_id: str,
    sender_id: str,
    default_allow_users: List[str],
    default_allow_groups: List[str],
    default_deny_users: List[str],
    default_deny_groups: List[str],
) -> bool:
    """是否允许访问该条目

    规则：
    - 显式 deny 优先（全局 + 条目）；
    - 条目 allow 非空时作为白名单（需命中其一）；
    - 条目未限制且全局 allow 非空 → 使用全局白名单；
    - 否则默认允许。
    """
    perms = entry.get("permissions") or {}
    allow = perms.get("allow") or {}
    deny = perms.get("deny") or {}

    deny_users = set(norm_list_str(deny.get("users"))) | set(default_deny_users)
    deny_groups = set(norm_list_str(deny.get("groups"))) | set(default_deny_groups)
    if str(sender_id) in deny_users or str(group_id) in deny_groups:
        return False

    allow_users = set(norm_list_str(allow.get("users")))
    allow_groups = set(norm_list_str(allow.get("groups")))
    has_allow_constraint = bool(allow_users or allow_groups)

    if not has_allow_constraint:
        if default_allow_users or default_allow_groups:
            return (str(sender_id) in set(default_allow_users)) or (
                str(group_id) in set(default_allow_groups)
            )
        return True

    if (str(sender_id) in allow_users) or (str(group_id) in allow_groups):
        return True
    return False

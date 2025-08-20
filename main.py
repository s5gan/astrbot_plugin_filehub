import os
import re
import json
import time
import shutil
from typing import List, Dict, Any, Optional, Tuple
import base64
from .registry import load_registry, resolve_registry_path
from .permissions import norm_list_str, has_access
from .file_ops import is_image, is_valid_image_file, normalize_abs_path
from .search import search_entries, format_entry_brief

import astrbot.api.message_components as Comp
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr
from astrbot.api import logger
from astrbot.core.utils.io import download_file
 


DEFAULT_ROOT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "filehub")
)
DEFAULT_REGISTRY = "registry.json"


def _format_entry_brief(entry: Dict[str, Any]) -> str:
    name = entry.get("name") or os.path.basename(str(entry.get("path", "")))
    desc = entry.get("description") or ""
    return f"{entry.get('id','')} | {name} | {desc}"


@register("astrbot_plugin_filehub", "awa", "本地文件检索与发送插件", "0.1.0", "")
class FileHubPlugin(Star):
    def __init__(self, context: Context, config: Optional[Dict[str, Any]] = None):
        super().__init__(context)
        self.config = config or {}
        self.root_dir: str = os.path.abspath(self.config.get("root_dir") or DEFAULT_ROOT_DIR)
        self.registry_file: str = str(self.config.get("registry_file") or DEFAULT_REGISTRY)
        self.cb_base: str = str(self.config.get("callback_api_base") or "").strip()
        self.max_file_size_mb: int = int(self.config.get("max_file_size_mb") or -1)

        # 默认权限配置（当注册项未写权限时使用）；按需求：未写权限 -> 全体可访问
        self.default_allow_users: List[str] = norm_list_str(self.config.get("default_allow_users"))
        self.default_allow_groups: List[str] = norm_list_str(self.config.get("default_allow_groups"))
        self.default_deny_users: List[str] = norm_list_str(self.config.get("default_deny_users"))
        self.default_deny_groups: List[str] = norm_list_str(self.config.get("default_deny_groups"))
        os.makedirs(self.root_dir, exist_ok=True)

        # 最近媒体缓存（按会话维度）：[{path,type,name,timestamp}]
        self.recent_media: Dict[str, List[Dict[str, Any]]] = {}

        logger.info(f"[FileHub] root_dir={self.root_dir} registry={self.registry_file}")

        # 如插件配置提供了 callback_api_base，则写入 AstrBot 全局配置，省去手动改 cmd_config.json
        try:
            if self.cb_base:
                conf = self.context.get_config()
                old = str(conf.get("callback_api_base") or "").strip()
                if old != self.cb_base:
                    conf["callback_api_base"] = self.cb_base
                    conf.save_config()
                    logger.info(f"[FileHub] 已设置 callback_api_base = {self.cb_base}")
        except Exception as e:
            logger.warning(f"[FileHub] 写入 callback_api_base 失败: {e}")

    # =============== 内部工具 ===============
    @staticmethod
    def _slugify(name: str) -> str:
        s = os.path.splitext(os.path.basename(name))[0].lower()
        s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
        return s or "file"

    @staticmethod
    def _get_file_size_mb(path: str) -> float:
        try:
            return os.path.getsize(path) / (1024 * 1024)
        except Exception:
            return 0.0

    def _save_registry(self, reg: Dict[str, Any]):
        """保存索引 JSON 到磁盘。"""
        path = resolve_registry_path(self.root_dir, self.registry_file)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(reg, f, ensure_ascii=False, indent=2)

    def _unique_id(self, files: List[Dict[str, Any]], base: str) -> str:
        """基于 base 生成唯一 id。"""
        uid = base
        i = 1
        ids = {str(x.get("id")) for x in files}
        while uid in ids:
            i += 1
            uid = f"{base}_{i}"
        return uid

    def _copy_into_root(self, abs_src: str, preferred_slug: str) -> Tuple[str, str]:
        """复制文件到 root_dir/saved/，返回 (relpath, final_name)。"""
        os.makedirs(self.root_dir, exist_ok=True)
        subdir = os.path.join(self.root_dir, "saved")
        os.makedirs(subdir, exist_ok=True)
        ext = os.path.splitext(abs_src)[1]
        slug = self._slugify(preferred_slug) or "file"
        name = f"{slug}{ext}"
        dst = os.path.join(subdir, name)
        seq = 1
        while os.path.exists(dst):
            name = f"{slug}_{seq}{ext}"
            dst = os.path.join(subdir, name)
            seq += 1
        shutil.copy2(abs_src, dst)
        relp = os.path.relpath(dst, self.root_dir)
        return relp, name

    def _remember_media(self, origin: str, item: Dict[str, Any]):
        """记录最近媒体（最多 5 个，1 小时内有效）。"""
        bucket = self.recent_media.setdefault(origin, [])
        bucket.append(item)
        now = time.time()
        bucket[:] = [x for x in bucket if now - x.get("timestamp", 0) <= 3600]
        if len(bucket) > 5:
            del bucket[:-5]

    async def _send_image_safely(self, event: AstrMessageEvent, abs_path: str, name: str) -> None:
        """在不同平台下尽量可靠地发送图片。

        优先使用回调URL（需要 callback_api_base），否则回退 base64，再回退本地文件路径。
        """
        plat = (event.get_platform_name() or "").lower()
        try:
            conf = self.context.get_config()
            cb = str(conf.get("callback_api_base") or "").strip()
        except Exception:
            cb = ""

        # 优先：回调URL（Napcat/go-cqhttp 可靠）
        if cb:
            try:
                url = await Comp.File(name=name, file=abs_path).register_to_file_service()
                await event.send(MessageChain([Comp.Image.fromURL(url)]))
                return
            except Exception:
                pass

        # 其次：base64
        try:
            with open(abs_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            await event.send(MessageChain([Comp.Image.fromBase64(b64)]))
            return
        except Exception:
            pass

        # 最后：本地文件路径
        await event.send(MessageChain([Comp.Image.fromFileSystem(abs_path)]))

    def _has_access(self, entry: Dict[str, Any], group_id: str, sender_id: str) -> bool:
        return has_access(
            entry,
            group_id,
            sender_id,
            self.default_allow_users,
            self.default_allow_groups,
            self.default_deny_users,
            self.default_deny_groups,
        )

    # =============== 基础指令 ===============

    @filter.command_group("filehub")
    def filehub(self):
        """文件中心：管理/搜索/发送本地文件。"""
        pass

    

    @filehub.command("list")
    async def list_files(self, event: AstrMessageEvent, query: str = GreedyStr):
        query = (query or "").strip()
        reg, _ = load_registry(self.root_dir, self.registry_file)
        group_id = event.get_group_id() or ""
        sender_id = event.get_sender_id() or ""
        entries = [e for e in reg.get("files", []) if self._has_access(e, group_id, sender_id)]
        scored = search_entries(entries, query)
        if not scored:
            yield event.plain_result("未找到匹配的文件。")
            return
        lines = ["候选："] + ["- " + _format_entry_brief(e) for _, e in scored[:20]]
        yield event.plain_result("\n".join(lines))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _capture_recent_media(self, event: AstrMessageEvent):
        """捕获最近收到的图片/文件，供“自然语言保存”使用（不回消息）。"""
        try:
            comps = event.message_obj.message or []
            if not comps:
                return
            origin = event.unified_msg_origin
            found = False
            for comp in comps:
                if isinstance(comp, Comp.Image):
                    try:
                        path = await comp.convert_to_file_path()
                        if path and os.path.exists(path):
                            self._remember_media(origin, {
                                "path": os.path.abspath(path),
                                "type": "image",
                                "name": os.path.basename(path),
                                "timestamp": time.time(),
                            })
                            found = True
                    except Exception:
                        pass
                if isinstance(comp, Comp.File):
                    try:
                        path = await comp.get_file()
                        if path and os.path.exists(path):
                            self._remember_media(origin, {
                                "path": os.path.abspath(path),
                                "type": "file",
                                "name": comp.name or os.path.basename(path),
                                "timestamp": time.time(),
                            })
                            found = True
                    except Exception:
                        pass
            if found:
                logger.debug("[FileHub] 捕获到最近媒体，已缓存供保存使用。")
        except Exception as e:
            logger.debug(f"[FileHub] 捕获媒体失败: {e}")

    @filehub.command("info")
    async def info(self, event: AstrMessageEvent):
        """查看根目录、索引状态与关键配置。"""
        reg, used_path = load_registry(self.root_dir, self.registry_file)
        total = len(reg.get("files", []))
        cb = ""
        try:
            cb = str(self.context.get_config().get("callback_api_base") or "")
        except Exception:
            pass
        lines = [
            f"root_dir: {self.root_dir}",
            f"registry: {used_path}",
            f"entries: {total}",
            f"callback_api_base: {cb or '(未设置)'}",
        ]
        yield event.plain_result("\n".join(lines))

    @filehub.command("set_callback")
    async def set_callback(self, event: AstrMessageEvent, url: str):
        """设置回调地址供 Napcat 拉取文件，例如: /filehub set_callback http://127.0.0.1:6185"""
        url = (url or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            yield event.plain_result("请提供以 http:// 或 https:// 开头的地址")
            return
        try:
            conf = self.context.get_config()
            conf["callback_api_base"] = url
            conf.save_config()
            self.cb_base = url
            yield event.plain_result(f"已设置 callback_api_base = {url}，请稍后重试发送文件。")
        except Exception as e:
            yield event.plain_result(f"设置失败: {e}")



    @filehub.command("send")
    async def send_file(self, event: AstrMessageEvent, file_id: str):
        reg, _ = load_registry(self.root_dir, self.registry_file)
        group_id = event.get_group_id() or ""
        sender_id = event.get_sender_id() or ""
        entries = reg.get("files", [])
        match = next((e for e in entries if str(e.get("id")) == str(file_id)), None)
        if not match:
            yield event.plain_result(f"未找到 id={file_id} 的文件。可先使用 /filehub list 搜索。")
            return
        if not self._has_access(match, group_id, sender_id):
            yield event.plain_result("你没有权限获取该文件。")
            return
        abs_path = normalize_abs_path(self.root_dir, str(match.get("path", "")))
        if not os.path.exists(abs_path):
            yield event.plain_result(f"文件不存在：{abs_path}")
            return
        name = match.get("name") or os.path.basename(abs_path)
        send_as = (match.get("send_as") or "auto").lower()

        plat = (event.get_platform_name() or "").lower()
        is_img = send_as == "image" or (send_as == "auto" and is_image(abs_path))
        if is_img:
            if not is_valid_image_file(abs_path):
                yield event.plain_result(f"图片文件无效或已损坏：{name}")
                return
            await self._send_image_safely(event, abs_path, name)
            return
        # 非图片文件：针对不同平台选择合适策略
        if plat == "aiocqhttp":
            # 方案A：使用 AstrBot 回调服务生成可下载URL，Napcat后台拉取并原生发送（聊天不显示链接）
            # 仅需传 file=宿主绝对路径，to_dict() 会基于 callback_api_base 生成回调URL
            if self.max_file_size_mb and self.max_file_size_mb > 0:
                size_mb = self._get_file_size_mb(abs_path)
                if size_mb > self.max_file_size_mb:
                    yield event.plain_result(
                        f"文件大小 {size_mb:.1f}MB 超过阈值 {self.max_file_size_mb}MB，仍尝试发送（将走回调拉取）。"
                    )
            yield event.chain_result([Comp.File(name=name, file=abs_path)])
            return
        # 其他可能不支持 File 段的平台，尝试回退为下载链接文本
        if plat in {"qq_official", "weixin_official_account", "dingtalk"}:
            try:
                url = await Comp.File(name=name, file=abs_path).register_to_file_service()
                yield event.plain_result(f"{name}: {url}")
                return
            except Exception:
                # 回调服务不可用时，仍然尝试文件消息段（部分平台可能直接忽略）
                pass
        yield event.chain_result([Comp.File(name=name, file=abs_path)])
        return

    @filehub.command("show")
    async def show(self, event: AstrMessageEvent, file_id: str):
        """查看单项详情（路径/大小/权限等）。"""
        reg, _ = load_registry(self.root_dir, self.registry_file)
        group_id = event.get_group_id() or ""
        sender_id = event.get_sender_id() or ""
        e = next((x for x in reg.get("files", []) if str(x.get("id")) == str(file_id)), None)
        if not e:
            yield event.plain_result(f"未找到 id={file_id} 的条目。")
            return
        can = self._has_access(e, group_id, sender_id)
        abs_path = normalize_abs_path(self.root_dir, str(e.get("path", "")))
        exists = os.path.exists(abs_path)
        size_mb = self._get_file_size_mb(abs_path) if exists else 0.0
        lines = [
            f"id: {e.get('id')}",
            f"name: {e.get('name')}",
            f"path: {e.get('path')}\nabs: {abs_path}",
            f"exists: {exists} size: {size_mb:.2f}MB",
            f"send_as: {e.get('send_as','auto')} is_image: {is_image(abs_path)}",
            f"description: {e.get('description','')}",
            f"you_can_access: {can}",
        ]
        perms = e.get("permissions") or {}
        allow = perms.get("allow") or {}
        deny = perms.get("deny") or {}
        lines += [
            f"allow.users: {', '.join(map(str, allow.get('users') or []))}",
            f"allow.groups: {', '.join(map(str, allow.get('groups') or []))}",
            f"deny.users: {', '.join(map(str, deny.get('users') or []))}",
            f"deny.groups: {', '.join(map(str, deny.get('groups') or []))}",
        ]
        yield event.plain_result("\n".join(lines))

    @filehub.command("probe")
    async def probe(self, event: AstrMessageEvent, file_id: str):
        """诊断路径映射/回调服务可用性，辅助排障。"""
        from astrbot.core.utils.path_util import path_Mapping

        reg, _ = load_registry(self.root_dir, self.registry_file)
        e = next((x for x in reg.get("files", []) if str(x.get("id")) == str(file_id)), None)
        if not e:
            yield event.plain_result(f"未找到 id={file_id} 的条目。")
            return
        abs_path = normalize_abs_path(self.root_dir, str(e.get("path", "")))
        exists = os.path.exists(abs_path)

        conf = self.context.get_config()
        cb = str(conf.get("callback_api_base") or "")
        plat = (event.get_platform_name() or "").lower()
        pm = (conf.get("platform_settings", {}) or {}).get("path_mapping", [])
        mapped = path_Mapping(pm, abs_path) if pm else abs_path
        lines = [
            f"platform: {plat}",
            f"abs_path: {abs_path}",
            f"exists_on_host: {exists}",
            f"path_mapping_rules: {pm if pm else '[]'}",
            f"mapped_path(for adapter): {mapped}",
            f"callback_api_base: {cb or '(未设置)'}",
        ]
        if cb:
            try:
                url = await Comp.File(name=e.get("name") or os.path.basename(abs_path), file=abs_path).register_to_file_service()
                lines.append(f"registered_url: {url}")
            except Exception as er:
                lines.append(f"register_failed: {er}")
        yield event.plain_result("\n".join(lines))

    # =============== LLM 工具 ===============

    @filter.llm_tool(name="save_recent_file")
    async def tool_save_recent_file(
        self,
        event: AstrMessageEvent,
        name: str = "",
        description: str = "",
        send_as: str = "auto",
        which: int = -1,
        prefer_type: str = "any",
    ) -> MessageEventResult:
        """保存最近发送的图片/文件到文件库并写入索引。

        参数:
        - name(string): 条目名称（缺省从消息文本或原文件名推断）
        - description(string): 描述
        - send_as(string): auto|image|file（默认 auto）
        - which(number): 选取第几个最近媒体，-1 表示最后一个
        - prefer_type(string): any|image|file（仅在存在多种媒体时用于筛选）
        """
        origin = event.unified_msg_origin
        bucket = list(self.recent_media.get(origin, []))
        if not bucket:
            yield event.plain_result("未检测到最近发送的媒体，请先发送图片或文件，再说明要保存。")
            return
        prefer_type = (prefer_type or "any").lower()
        if prefer_type in {"image", "file"}:
            bucket = [x for x in bucket if x.get("type") == prefer_type]
            if not bucket:
                yield event.plain_result("未找到符合类型的媒体，请调整 prefer_type。")
                return
        # 选择项
        idx = which if which is not None else -1
        if idx < 0:
            last = bucket[-1]
        else:
            if idx >= len(bucket):
                yield event.plain_result("which 超出范围。")
                return
            last = bucket[idx]
        abs_src = last.get("path")
        if not abs_src or not os.path.exists(abs_src):
            yield event.plain_result("最近媒体不可用或已过期，请重试发送。")
            return
        reg, _ = load_registry(self.root_dir, self.registry_file)
        files = reg.get("files", [])
        # 解析名称：若未提供，从用户文本中提取关键短语；再退回源文件名
        raw_text = (event.message_str or "").strip()
        nm = (name or "").strip()
        if not nm and raw_text:
            # 常见表达：就叫X / 名称: X / 叫X / 取名X
            m = re.search(r"(?:就叫|名称\s*[:：]|叫|取名)\s*([^，。,\n\r\s]+)\s*$", raw_text)
            if m:
                nm = m.group(1)
        if not nm:
            nm = os.path.splitext(os.path.basename(abs_src))[0]
        base_slug = self._slugify(nm)
        # 解析发送方式：根据最近媒体类型兜底
        s_as = (send_as or "auto").lower()
        if s_as == "auto":
            s_as = "image" if last.get("type") == "image" or is_image(abs_src) else "file"
        relp, final_name = self._copy_into_root(abs_src, base_slug)
        uid = self._unique_id(files, base_slug)
        entry = {
            "id": uid,
            "path": relp,
            "name": final_name,
            "description": (description or "").strip(),
            "send_as": s_as,
            "permissions": {"allow": {"users": [], "groups": []}, "deny": {"users": [], "groups": []}},
        }
        files.append(entry)
        try:
            self._save_registry({"files": files})
            yield event.plain_result(f"已保存：{final_name} (id={uid})")
        except Exception as e:
            yield event.plain_result(f"写入索引失败：{e}")

    @filter.llm_tool(name="save_file_from_url")
    async def tool_save_file_from_url(
        self,
        event: AstrMessageEvent,
        url: str,
        name: str = "",
        description: str = "",
        send_as: str = "auto",
    ) -> MessageEventResult:
        """从 HTTP(S) 链接保存文件到文件库并写入索引。

        参数:
        - url(string): 以 http/https 开头的下载链接
        - name(string): 可选名称（未提供时从 URL 文件名推断）
        - description(string): 描述
        - send_as(string): auto|image|file（默认 auto，按扩展名兜底）
        """
        u = (url or "").strip()
        if not (u.startswith("http://") or u.startswith("https://")):
            yield event.plain_result("请提供以 http:// 或 https:// 开头的 URL")
            return
        # 下载到临时路径
        tmp_dir = os.path.join(self.root_dir, ".tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        tmp_path = os.path.join(tmp_dir, os.path.basename(u) or "download.bin")
        try:
            await download_file(u, tmp_path)
        except Exception as e:
            yield event.plain_result(f"下载失败：{e}")
            return
        # 生成条目
        reg, _ = load_registry(self.root_dir, self.registry_file)
        files = reg.get("files", [])
        nm = (name or os.path.splitext(os.path.basename(tmp_path))[0]).strip() or "file"
        base_slug = self._slugify(nm)
        s_as = (send_as or "auto").lower()
        if s_as == "auto":
            s_as = "image" if is_image(tmp_path) else "file"
        relp, final_name = self._copy_into_root(tmp_path, base_slug)
        try:
            # 清理临时文件
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            uid = self._unique_id(files, base_slug)
            entry = {
                "id": uid,
                "path": relp,
                "name": final_name,
                "description": (description or "").strip(),
                "send_as": s_as,
                "permissions": {"allow": {"users": [], "groups": []}, "deny": {"users": [], "groups": []}},
            }
            files.append(entry)
            self._save_registry({"files": files})
            yield event.plain_result(f"已保存：{final_name} (id={uid})")
        except Exception as e:
            yield event.plain_result(f"写入索引失败：{e}")

    @filter.llm_tool(name="update_file_metadata")
    async def tool_update_file_metadata(
        self,
        event: AstrMessageEvent,
        file_id: str,
        name: str = "",
        description: str = "",
        send_as: str = "",
    ) -> MessageEventResult:
        """更新文件元数据。

        参数:
        - file_id(string): 索引中的 ID
        - name(string): 新名称（可选）
        - description(string): 新描述（可选）
        - send_as(string): auto|image|file（可选）
        - add_tags(array): 追加的标签
        - remove_tags(array): 要移除的标签
        """
        reg, _ = load_registry(self.root_dir, self.registry_file)
        files = reg.get("files", [])
        e = next((x for x in files if str(x.get("id")) == str(file_id)), None)
        if not e:
            yield event.plain_result("未找到该文件条目。")
            return
        if name:
            e["name"] = name.strip()
        if description:
            e["description"] = description.strip()
        if send_as:
            v = send_as.strip().lower()
            if v not in {"auto", "image", "file"}:
                yield event.plain_result("send_as 仅支持 auto|image|file")
                return
            e["send_as"] = v
        try:
            self._save_registry({"files": files})
            yield event.plain_result("已更新元数据。")
        except Exception as err:
            yield event.plain_result(f"保存失败：{err}")

    @filter.llm_tool(name="get_registry")
    async def tool_get_registry(self, event: AstrMessageEvent):
        """返回可访问条目的 registry.json 内容（JSON）。

        说明：为避免越权泄露，仅返回当前会话可访问的条目子集；结构与原 registry 类似。
        """
        reg, _ = load_registry(self.root_dir, self.registry_file)
        group_id = event.get_group_id() or ""
        sender_id = event.get_sender_id() or ""
        entries = [e for e in reg.get("files", []) if self._has_access(e, group_id, sender_id)]
        payload = {"files": entries}
        return json.dumps(payload, ensure_ascii=False)

    @filter.llm_tool(name="delete_file_by_id")
    async def tool_delete_file_by_id(
        self,
        event: AstrMessageEvent,
        file_id: str,
        remove_physical: str = "no",
    ) -> MessageEventResult:
        """删除文件条目；可选删除物理文件。

        参数:
        - file_id(string): 索引中的 ID
        - remove_physical(string): yes|no，是否删除物理文件（默认 no）
        """
        reg, _ = load_registry(self.root_dir, self.registry_file)
        files = reg.get("files", [])
        e = next((x for x in files if str(x.get("id")) == str(file_id)), None)
        if not e:
            yield event.plain_result("未找到该文件条目。")
            return
        # 物理删除
        if str(remove_physical).lower() in {"y", "yes", "true", "1"}:
            abs_path = normalize_abs_path(self.root_dir, str(e.get("path", "")))
            try:
                if abs_path and os.path.exists(abs_path):
                    os.remove(abs_path)
            except Exception:
                pass
        new_files = [x for x in files if str(x.get("id")) != str(file_id)]
        try:
            self._save_registry({"files": new_files})
            yield event.plain_result("已删除条目。")
        except Exception as err:
            yield event.plain_result(f"保存失败：{err}")

    @filter.llm_tool(name="set_file_permissions")
    async def tool_set_file_permissions(
        self,
        event: AstrMessageEvent,
        file_id: str,
        allow_users: List[str] | None = None,
        allow_groups: List[str] | None = None,
        deny_users: List[str] | None = None,
        deny_groups: List[str] | None = None,
        mode: str = "merge",
    ) -> MessageEventResult:
        """设置文件权限（合并或替换）。

        参数:
        - file_id(string): 索引中的 ID
        - allow_users(array): 允许的用户 ID 列表
        - allow_groups(array): 允许的群 ID 列表
        - deny_users(array): 拒绝的用户 ID 列表
        - deny_groups(array): 拒绝的群 ID 列表
        - mode(string): merge|replace，merge 为在原有基础上增改，replace 为覆盖
        """
        reg, _ = load_registry(self.root_dir, self.registry_file)
        files = reg.get("files", [])
        e = next((x for x in files if str(x.get("id")) == str(file_id)), None)
        if not e:
            yield event.plain_result("未找到该文件条目。")
            return
        perms = e.get("permissions") or {"allow": {"users": [], "groups": []}, "deny": {"users": [], "groups": []}}
        if (mode or "merge").lower() == "replace":
            perms = {"allow": {"users": [], "groups": []}, "deny": {"users": [], "groups": []}}
        # 合并
        if allow_users is not None:
            perms.setdefault("allow", {}).setdefault("users", [])
            perms["allow"]["users"] = list({*map(str, perms["allow"]["users"]), *map(str, allow_users)})
        if allow_groups is not None:
            perms.setdefault("allow", {}).setdefault("groups", [])
            perms["allow"]["groups"] = list({*map(str, perms["allow"]["groups"]), *map(str, allow_groups)})
        if deny_users is not None:
            perms.setdefault("deny", {}).setdefault("users", [])
            perms["deny"]["users"] = list({*map(str, perms["deny"]["users"]), *map(str, deny_users)})
        if deny_groups is not None:
            perms.setdefault("deny", {}).setdefault("groups", [])
            perms["deny"]["groups"] = list({*map(str, perms["deny"]["groups"]), *map(str, deny_groups)})
        e["permissions"] = perms
        try:
            self._save_registry({"files": files})
            yield event.plain_result("已更新权限。")
        except Exception as err:
            yield event.plain_result(f"保存失败：{err}")

    @filter.llm_tool(name="search_local_files")
    async def tool_search_files(self, event: AstrMessageEvent, query: str):
        """搜索本地文件。

        Args:
            query(string): 搜索关键词，可为文件名或描述。
        """
        logger.info(f"[FileHub/tool] search_local_files query={query!r}")
        reg, _ = load_registry(self.root_dir, self.registry_file)
        group_id = event.get_group_id() or ""
        sender_id = event.get_sender_id() or ""
        entries = [e for e in reg.get("files", []) if self._has_access(e, group_id, sender_id)]
        scored = search_entries(entries, query)
        # 返回结构化结果（字符串），供 LLM 消化（不直接向用户输出）
        items = []
        for _, e in scored[:10]:
            items.append({
                "id": e.get("id"),
                "name": e.get("name") or os.path.basename(str(e.get("path", ""))),
                "description": e.get("description") or "",
                "path": e.get("path", ""),
                "send_as": (e.get("send_as") or "auto"),
                "is_image": (
                    True if (e.get("send_as") or "auto") == "image"
                    else (is_image(str(e.get("path", ""))) if (e.get("send_as") or "auto") == "auto" else False)
                ),
            })
        return json.dumps({"results": items}, ensure_ascii=False)

    @filter.llm_tool(name="send_local_file_by_id")
    async def tool_send_file_by_id(self, event: AstrMessageEvent, file_id: str):
        """按 id 发送本地文件。

        Args:
            file_id(string): 在索引文档中注册的文件 id。
        """
        logger.info(f"[FileHub/tool] send_local_file_by_id id={file_id!r}")
        reg, _ = load_registry(self.root_dir, self.registry_file)
        group_id = event.get_group_id() or ""
        sender_id = event.get_sender_id() or ""
        entries = reg.get("files", [])
        match = next((e for e in entries if str(e.get("id")) == str(file_id)), None)
        if not match:
            return f"ERROR not_found id={file_id}"
        if not self._has_access(match, group_id, sender_id):
            return f"ERROR no_permission id={file_id}"
        abs_path = normalize_abs_path(self.root_dir, str(match.get("path", "")))
        if not os.path.exists(abs_path):
            return f"ERROR missing_file id={file_id}"
        name = match.get("name") or os.path.basename(abs_path)
        send_as = (match.get("send_as") or "auto").lower()

        plat = (event.get_platform_name() or "").lower()
        is_img = send_as == "image" or (send_as == "auto" and is_image(abs_path))
        if is_img:
            await self._send_image_safely(event, abs_path, name)
            return f"SENT id={file_id} name={name}"
        if plat == "aiocqhttp":
            await event.send(MessageChain([Comp.File(name=name, file=abs_path)]))
            return f"SENT id={file_id} name={name}"
        if plat in {"qq_official", "weixin_official_account", "dingtalk"}:
            try:
                url = await Comp.File(name=name, file=abs_path).register_to_file_service()
                await event.send(MessageChain().message(f"{name}: {url}"))
                return f"SENT id={file_id} name={name} url={url}"
            except Exception:
                pass
        await event.send(MessageChain([Comp.File(name=name, file=abs_path)]))
        return f"SENT id={file_id} name={name}"

    @filter.llm_tool(name="find_and_send")
    async def tool_find_and_send(self, event: AstrMessageEvent, query: str):
        """按关键词检索并直接发送最匹配的文件。

        Args:
            query(string): 搜索关键词（文件名或描述皆可）
        """
        logger.info(f"[FileHub/tool] find_and_send query={query!r}")
        reg, _ = load_registry(self.root_dir, self.registry_file)
        group_id = event.get_group_id() or ""
        sender_id = event.get_sender_id() or ""
        entries = [e for e in reg.get("files", []) if self._has_access(e, group_id, sender_id)]
        scored = search_entries(entries, query)
        if not scored:
            return "CANDIDATES 0"
        # 多候选：返回候选让 LLM 决策
        if len(scored) > 1:
            lines = ["CANDIDATES"]
            for _, e in scored[:10]:
                lines.append(_format_entry_brief(e))
            return "\n".join(lines)

        top = scored[0][1]
        abs_path = normalize_abs_path(self.root_dir, str(top.get("path", "")))
        if not os.path.exists(abs_path):
            return "ERROR missing_file"
        name = top.get("name") or os.path.basename(abs_path)
        file_id = top.get("id")
        send_as = (top.get("send_as") or "auto").lower()
        plat = (event.get_platform_name() or "").lower()
        is_img = send_as == "image" or (send_as == "auto" and is_image(abs_path))
        if is_img:
            if not is_valid_image_file(abs_path):
                return "ERROR invalid_image"
            await self._send_image_safely(event, abs_path, name)
            return f"SENT id={file_id} name={name}"
        if plat == "aiocqhttp":
            await event.send(MessageChain([Comp.File(name=name, file=abs_path)]))
            return f"SENT id={file_id} name={name}"
        if plat in {"qq_official", "weixin_official_account", "dingtalk"}:
            try:
                url = await Comp.File(name=name, file=abs_path).register_to_file_service()
                await event.send(MessageChain().message(f"{name}: {url}"))
                return f"SENT id={file_id} name={name} url={url}"
            except Exception:
                pass
        await event.send(MessageChain([Comp.File(name=name, file=abs_path)]))
        return f"SENT id={file_id} name={name}"

    # =============== 触发 LLM 的入口指令 ===============

    @filter.command("找文件")
    async def llm_find_and_send(self, event: AstrMessageEvent, query: str = GreedyStr):
        """让 LLM 使用工具搜索并发送文件。

        用法：/找文件 关键字
        """
        prompt = (
            "你是文件助理。先调用 get_registry() 获取可访问条目（不要把 registry 内容直接发给用户），"
            "阅读其中的 id、name、description、path、send_as，基于用户需求自行判断最合适的条目。\n"
            "- 若能唯一确定条目，调用 send_local_file_by_id(id) 发送；\n"
            "- 若存在多个候选，请整理候选并让用户选择 id；\n"
            "- 未找到合适条目时，明确告知并建议更换关键词；\n"
            "- 工具调用完成后，再发送一句简短的自然语言确认（例如：已发送 项目Logo.png）；\n"
            "- 严格遵循权限，不越权访问。"
        )

        query = (query or "").strip()
        if not query:
            yield event.plain_result("请提供关键词，例如：/找文件 设计图")
            return

        # 将工具管理器交给默认 LLM 流程；按文档仅在命令入口显式指定 provider
        func_tools_mgr = self.context.get_llm_tool_manager()
        # 选择一个 provider：优先使用默认提供商 ID；否则选第一个已加载的
        try:
            conf = self.context.get_config()
            default_pid = (conf.get("provider_settings", {}) or {}).get("default_provider_id") or ""
            pid = default_pid.strip()
            if not pid:
                provs = self.context.get_all_providers() or []
                if provs:
                    pid = provs[0].meta().id
            if pid:
                event.set_extra("selected_provider", pid)
        except Exception:
            pass
        yield event.request_llm(
            prompt=f"关键词：{query}",
            func_tool_manager=func_tools_mgr,
            system_prompt=prompt,
            contexts=[],
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def natural_save_request(self, event: AstrMessageEvent):
        """自然语言触发“保存最近媒体”。

        触发条件：消息包含保存意图且缓存中存在最近媒体。
        行为：发起一次 LLM 请求，强制调用 save_recent_file 工具解析用户给出的名称/标签/描述。
        """
        text = (event.message_str or "").strip()
        if not text:
            return
        # 关键词判断：尽量收敛触发，避免误伤其他对话
        intents = ["保存", "存一下", "存图", "存这个", "加入文件库", "收藏", "存下"]
        if not any(k in text for k in intents):
            return
        bucket = self.recent_media.get(event.unified_msg_origin, [])
        if not bucket:
            # 没有可保存的媒体，礼貌提示
            yield event.plain_result("我可以帮你保存最近发送的图片/文件，请先发送媒体，再告诉我要保存和名称。")
            event.stop_event()
            return
        # 构造 System Prompt，强约束使用工具
        sys_prompt = (
            "你是文件入库助手。用户说要保存最近的图片或文件时，必须调用 save_recent_file(name, description, send_as, which, prefer_type)。\n"
            "规范：\n"
            "- name：尽量从用户话里提取最简短易懂的名称；\n"
            "- description：保留用户额外描述；\n"
            "- send_as：若最近媒体是图片则用 'image'，若是普通文件则用 'file'，否则 'auto'；\n"
            "- which 与 prefer_type 用于在多媒体时选择合适文件；\n"
            "- 工具调用完成后，再发送一句简短的自然语言确认（例如：已保存 项目Logo.png）；\n"
            "- 仅调用一次工具。\n"
        )
        # 选择 provider（与“找文件”一致的策略）
        func_tools_mgr = self.context.get_llm_tool_manager()
        try:
            conf = self.context.get_config()
            default_pid = (conf.get("provider_settings", {}) or {}).get("default_provider_id") or ""
            pid = default_pid.strip()
            if not pid:
                provs = self.context.get_all_providers() or []
                if provs:
                    pid = provs[0].meta().id
            if pid:
                event.set_extra("selected_provider", pid)
        except Exception:
            pass
        # 将原始用户文本作为 prompt，让模型解析字段并调用工具
        yield event.request_llm(
            prompt=text,
            func_tool_manager=func_tools_mgr,
            system_prompt=sys_prompt,
            contexts=[],
        )

    # =============== 全局 LLM 请求引导 ===============
    @filter.on_llm_request()
    async def steer_llm(self, event: AstrMessageEvent, req: ProviderRequest):
        guide = (
            "当用户请求发送/查找文件或图片（如‘发我’、‘发送’、‘给我’、‘找一下’、‘文件’、‘图片’、‘logo’等关键词出现），"
            "请优先使用以下步骤：\n"
            "1) 调用 get_registry() 获取可访问条目（不要把 registry 内容直接发给用户）；\n"
            "2) 基于用户描述选择最合适的 id，调用 send_local_file_by_id(id) 发送文件；\n"
            "3) 工具调用完成后，再发送一句简短的自然语言确认（例如：已发送 项目Logo.png）。\n"
            "如存在多个候选，请列出候选并让用户选择 id；若无合适条目，请明确告知并给出建议。"
        )
        req.system_prompt = (req.system_prompt or "") + "\n" + guide

    # =============== 维护命令：删除与更新 ===============

    @filehub.command("remove")
    async def remove_entry(self, event: AstrMessageEvent, file_id: str):
        """从索引中移除条目（不删除物理文件）。"""
        reg, _ = load_registry(self.root_dir, self.registry_file)
        files = reg.get("files", [])
        new_files = [e for e in files if str(e.get("id")) != str(file_id)]
        if len(new_files) == len(files):
            yield event.plain_result(f"未找到 id={file_id} 的条目。")
            return
        try:
            self._save_registry({"files": new_files})
            yield event.plain_result(f"已移除 id={file_id}。")
        except Exception as e:
            yield event.plain_result(f"保存失败：{e}")

    @filehub.command("update")
    async def update_entry(self, event: AstrMessageEvent, file_id: str, field: str, value: str = GreedyStr):
        """更新索引条目字段。

        用法：/filehub update <id> <field> <value>
        - field 可为 name|desc|send_as
        """
        reg, _ = load_registry(self.root_dir, self.registry_file)
        files = reg.get("files", [])
        e = next((x for x in files if str(x.get("id")) == str(file_id)), None)
        if not e:
            yield event.plain_result(f"未找到 id={file_id} 的条目。")
            return
        field = (field or "").lower()
        if field == "name":
            e["name"] = value.strip()
        elif field in {"desc", "description"}:
            e["description"] = value.strip()
        elif field == "send_as":
            v = (value or "auto").lower()
            if v not in {"auto", "image", "file"}:
                yield event.plain_result("send_as 仅支持 auto|image|file")
                return
            e["send_as"] = v
        else:
            yield event.plain_result("不支持的字段，请使用 name|desc|send_as")
            return
        try:
            self._save_registry({"files": files})
            yield event.plain_result("已更新。")
        except Exception as e:
            yield event.plain_result(f"保存失败：{e}")

    # =============== 辅助命令：扫描并写入索引 ===============

    # =============== 辅助命令：扫描并写入索引 ===============
    @filehub.command("index")
    async def build_index(self, event: AstrMessageEvent, mode: str = "all", recursive: str = "yes"):
        """扫描 root_dir 并将新文件写入索引

        用法：/filehub index [all|images] [yes|no]
        - mode = all | images（仅收集图片）
        - recursive = yes | no（是否递归子目录）
        说明：仅新增未在索引中的文件，自动生成 id/name/send_as。
        """
        reg, used_path = load_registry(self.root_dir, self.registry_file)
        files = reg.get("files", [])
        known_abs = {os.path.abspath(normalize_abs_path(self.root_dir, f.get("path", ""))) for f in files}
        add_cnt = 0
        index_abs = os.path.abspath(resolve_registry_path(self.root_dir, self.registry_file))
        for root, dirs, fnames in os.walk(self.root_dir):
            # 跳过隐藏目录
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for fn in fnames:
                if fn.startswith('.'):
                    continue
                fp = os.path.join(root, fn)
                # 跳过索引文件本身
                if os.path.abspath(fp) == index_abs:
                    continue
                if mode == "images" and not is_image(fp):
                    continue
                abp = os.path.abspath(fp)
                if abp in known_abs:
                    continue
                relp = os.path.relpath(abp, self.root_dir)
                # 生成唯一 id
                base = self._slugify(fn)
                uid = base
                i = 1
                ids = {str(x.get("id")) for x in files}
                while uid in ids:
                    i += 1
                    uid = f"{base}_{i}"
                entry = {
                    "id": uid,
                    "path": relp,
                    "name": fn,
                    "description": "",
                    "send_as": "image" if is_image(fp) else "file",
                    "permissions": {"allow": {"users": [], "groups": []}, "deny": {"users": [], "groups": []}},
                }
                files.append(entry)
                known_abs.add(abp)
                add_cnt += 1
            if recursive.lower() not in {"y", "yes", "true", "1"}:
                break
        # 保存
        try:
            path = resolve_registry_path(self.root_dir, self.registry_file)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"files": files}, f, ensure_ascii=False, indent=2)
            yield event.plain_result(f"索引完成，新增 {add_cnt} 条，写入：{path}")
        except Exception as e:
            yield event.plain_result(f"写入索引失败：{e}")

    # 注：不再通过 on_llm_request 注入工具或绑定 provider，转为在命令入口显式指定 provider，并交由默认 LLM 流程处理。

import os
import re
import json
from typing import List, Dict, Any, Optional, Tuple
from .registry import load_registry, resolve_registry_path
from .permissions import norm_list_str, has_access
from .file_ops import is_image, is_valid_image_file, normalize_abs_path
from .search import search_entries, format_entry_brief

import astrbot.api.message_components as Comp
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr
from astrbot.api import logger
 


DEFAULT_ROOT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "filehub")
)
DEFAULT_REGISTRY = "registry.json"


def _format_entry_brief(entry: Dict[str, Any]) -> str:
    name = entry.get("name") or os.path.basename(str(entry.get("path", "")))
    desc = entry.get("description") or ""
    tags = entry.get("tags") or []
    return f"{entry.get('id','')} | {name} | {desc} | tags: {', '.join(map(str,tags))}"


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
            yield event.chain_result([Comp.Image.fromFileSystem(abs_path)])
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

    # =============== LLM 工具 ===============

    @filter.llm_tool(name="search_local_files")
    async def tool_search_files(self, event: AstrMessageEvent, query: str) -> MessageEventResult:
        """搜索本地文件。

        Args:
            query(string): 搜索关键词，可为文件名、标签或描述。
        """
        logger.info(f"[FileHub/tool] search_local_files query={query!r}")
        reg, _ = load_registry(self.root_dir, self.registry_file)
        group_id = event.get_group_id() or ""
        sender_id = event.get_sender_id() or ""
        entries = [e for e in reg.get("files", []) if self._has_access(e, group_id, sender_id)]
        scored = search_entries(entries, query)
        if not scored:
            yield event.plain_result("未找到匹配的文件。")
            return
        # 输出结构化 JSON，便于 LLM 稳定解析 id
        items = []
        for _, e in scored[:10]:
            items.append({
                "id": e.get("id"),
                "name": e.get("name") or os.path.basename(str(e.get("path", ""))),
                "description": e.get("description") or "",
                "tags": e.get("tags") or [],
                "path": e.get("path", ""),
                "send_as": (e.get("send_as") or "auto"),
                "is_image": (
                    True if (e.get("send_as") or "auto") == "image"
                    else (is_image(str(e.get("path", ""))) if (e.get("send_as") or "auto") == "auto" else False)
                ),
            })
        yield event.plain_result(json.dumps({"results": items}, ensure_ascii=False))

    @filter.llm_tool(name="send_local_file_by_id")
    async def tool_send_file_by_id(self, event: AstrMessageEvent, file_id: str) -> MessageEventResult:
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
            yield event.plain_result(f"未找到 id={file_id} 的文件。")
            return
        if not self._has_access(match, group_id, sender_id):
            yield event.plain_result("没有权限发送该文件。")
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
            yield event.plain_result(f"正在发送：{name}（id={file_id}）")
            yield event.chain_result([Comp.Image.fromFileSystem(abs_path)])
            return
        if plat == "aiocqhttp":
            yield event.plain_result(f"正在发送：{name}（id={file_id}）")
            if self.max_file_size_mb and self.max_file_size_mb > 0:
                size_mb = self._get_file_size_mb(abs_path)
                if size_mb > self.max_file_size_mb:
                    yield event.plain_result(
                        f"文件大小 {size_mb:.1f}MB 超过阈值 {self.max_file_size_mb}MB，仍尝试发送（将走回调拉取）。"
                    )
            yield event.chain_result([Comp.File(name=name, file=abs_path)])
            return
        if plat in {"qq_official", "weixin_official_account", "dingtalk"}:
            try:
                url = await Comp.File(name=name, file=abs_path).register_to_file_service()
                yield event.plain_result(f"{name}: {url}")
                return
            except Exception:
                pass
        yield event.chain_result([Comp.File(name=name, file=abs_path)])

    @filter.llm_tool(name="find_and_send")
    async def tool_find_and_send(self, event: AstrMessageEvent, query: str) -> MessageEventResult:
        """按关键词检索并直接发送最匹配的文件。

        Args:
            query(string): 搜索关键词（文件名/标签/描述皆可）
        """
        logger.info(f"[FileHub/tool] find_and_send query={query!r}")
        reg, _ = load_registry(self.root_dir, self.registry_file)
        group_id = event.get_group_id() or ""
        sender_id = event.get_sender_id() or ""
        entries = [e for e in reg.get("files", []) if self._has_access(e, group_id, sender_id)]
        scored = search_entries(entries, query)
        if not scored:
            yield event.plain_result("未找到匹配的文件。")
            return
        # 如果存在多个高分候选，则直接列出候选，请用户选择具体 id
        if len(scored) > 1:
            choices = [
                f"- { _format_entry_brief(e) }" for _, e in scored[:10]
            ]
            tips = (
                "检测到多个候选，请回复要发送的 id，"
                "或使用指令：/filehub send <id>"
            )
            yield event.plain_result("找到多个匹配：\n" + "\n".join(choices) + "\n" + tips)
            return

        top = scored[0][1]
        abs_path = normalize_abs_path(self.root_dir, str(top.get("path", "")))
        if not os.path.exists(abs_path):
            yield event.plain_result(f"文件不存在：{abs_path}")
            return
        name = top.get("name") or os.path.basename(abs_path)
        file_id = top.get("id")
        send_as = (top.get("send_as") or "auto").lower()
        plat = (event.get_platform_name() or "").lower()
        is_img = send_as == "image" or (send_as == "auto" and is_image(abs_path))
        if is_img:
            if not is_valid_image_file(abs_path):
                yield event.plain_result(f"图片文件无效或已损坏：{name}")
                return
            yield event.plain_result(f"已选择并发送：{name}（id={file_id}）")
            yield event.chain_result([Comp.Image.fromFileSystem(abs_path)])
            return
        if plat == "aiocqhttp":
            yield event.plain_result(f"已选择并发送：{name}（id={file_id}）")
            yield event.chain_result([Comp.File(name=name, file=abs_path)])
            return
        if plat in {"qq_official", "weixin_official_account", "dingtalk"}:
            try:
                url = await Comp.File(name=name, file=abs_path).register_to_file_service()
                yield event.plain_result(f"{name}: {url}")
                return
            except Exception:
                pass
        yield event.chain_result([Comp.File(name=name, file=abs_path)])

    # =============== 触发 LLM 的入口指令 ===============

    @filter.command("找文件")
    async def llm_find_and_send(self, event: AstrMessageEvent, query: str = GreedyStr):
        """让 LLM 使用工具搜索并发送文件。

        用法：/找文件 关键字
        """
        prompt = (
            "你是文件助理。严格使用函数工具完成文件检索与发送：\n"
            "1) 若用户需求明确且只有一个匹配，调用 find_and_send(query) 直接发送；\n"
            "2) 若存在多个候选，调用 find_and_send(query) 让工具列出候选供用户选择，切勿自行臆断；\n"
            "3) 如需分步，先 search_local_files(query) 再 send_local_file_by_id(id)；\n"
            "4) 未找到则清晰说明并建议用户换关键词；\n"
            "5) 严格遵循索引权限，不越权访问。"
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

    # =============== 辅助命令：扫描并写入索引 ===============

    # =============== 辅助命令：扫描并写入索引 ===============
    @filehub.command("index")
    async def build_index(self, event: AstrMessageEvent, mode: str = "all", recursive: str = "yes"):
        """扫描 root_dir 并将新文件写入索引

        用法：/filehub index [all|images] [yes|no]
        - mode = all | images（仅收集图片）
        - recursive = yes | no（是否递归子目录）
        说明：仅新增未在索引中的文件，自动生成 id/name/send_as/tags。
        """
        reg, used_path = load_registry(self.root_dir, self.registry_file)
        files = reg.get("files", [])
        known_abs = {os.path.abspath(normalize_abs_path(self.root_dir, f.get("path", ""))) for f in files}
        add_cnt = 0
        for root, dirs, fnames in os.walk(self.root_dir):
            for fn in fnames:
                fp = os.path.join(root, fn)
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
                    "tags": [os.path.splitext(fn)[1].lstrip(".")],
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
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"files": files}, f, ensure_ascii=False, indent=2)
            yield event.plain_result(f"索引完成，新增 {add_cnt} 条，写入：{path}")
        except Exception as e:
            yield event.plain_result(f"写入索引失败：{e}")

    # 注：不再通过 on_llm_request 注入工具或绑定 provider，转为在命令入口显式指定 provider，并交由默认 LLM 流程处理。

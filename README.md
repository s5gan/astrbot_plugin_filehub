AstrBot FileHub 插件
=====================

功能：
- 在 QQ 中通过指令/LLM 查找并发送服务器本地文件。
- 使用 JSON 索引管理文件路径、描述、标签与权限。
- 支持 aiocqhttp（Napcat/go-cqhttp）原生发送文件（走回调拉取），聊天中不显示链接。

快速开始：
- 默认根目录：`AstrBot/data/filehub`（与 info 命令一致）。
- 索引文件：固定为 `registry.json`（位于根目录内）。

索引示例（JSON）：

```
{
  "files": [
    {
      "id": "logo",
      "path": "images/logo.png",
      "name": "项目Logo.png",
      "description": "团队项目Logo",
      "tags": ["logo", "image"],
      "send_as": "auto",
      "permissions": {
        "allow": {"users": ["12345678"], "groups": ["987654321"]},
        "deny": {"users": [], "groups": []}
      }
    }
  ]
}
```

常用指令：
- `/filehub info` 查看根目录与索引状态（包含实际使用的索引文件路径与条目数）
- `/filehub list [关键词]` 搜索并列出候选
- `/filehub send <id>` 发送索引中的文件
- `/找文件 <关键词>` 交给 LLM 使用工具自动检索与发送
- `/filehub show <id>` 查看单个条目的详情（路径、大小、权限等）
- `/filehub index [all|images] [yes|no]` 扫描根目录并将新文件写入索引（默认 all、递归）
- `/filehub probe <id>` 诊断路径映射（用于排查 Napcat 无法读文件问题）
- `/filehub set_callback <url>` 设置回调地址（Napcat 拉取文件用），如 `http://127.0.0.1:6185`

自然语言驱动：
- 无需指令，直接说“把XX文档发我”“找下关于XX的图片”等：
  - 模型优先调用 `find_and_send(query)` 直接检索并发送最合适的文件；
  - 如需多候选，则 `search_local_files(query)` → `send_local_file_by_id(id)`；
  - 无结果会友好提示并建议关键词；严格遵循索引权限。

LLM 工具（给有需要的开发者参考）：
- `search_local_files(query: string)`：返回 JSON 结果 
  - 结构：`{ "results": [{ "id", "name", "description", "tags", "path", "send_as", "is_image" }] }`
  - 说明：便于 LLM 直接解析 id、并基于 path/send_as/is_image 做更稳健的决策
- `send_local_file_by_id(file_id: string)`：按索引 id 发送文件
- `find_and_send(query: string)`：合并工具，检索并直接发送 top1（更易被 LLM 使用）

插件配置（可在 AstrBot WebUI 插件管理中修改）：
- `root_dir`：文件根目录，默认 `AstrBot/data/filehub`
- `registry_file`：索引文件名或绝对路径（JSON），默认 `registry.json`
- `callback_api_base`：回调地址（Napcat 拉取文件用），如 `http://127.0.0.1:6185`
- `max_file_size_mb`：文件大小阈值（MB），-1 表示不限制。超出阈值时会提示并继续尝试发送。
- `default_allow_users`：当索引条目未设置权限时，全局允许的用户ID列表（留空=全体可访问）
- `default_allow_groups`：当索引条目未设置权限时，全局允许的群ID列表（留空=全体可访问）
- `default_deny_users`：全局拒绝的用户ID列表
- `default_deny_groups`：全局拒绝的群ID列表

注意：
- 非图片文件的发送依赖平台支持（aiocqhttp/Telegram 支持 File；部分官方接口不支持）。
- 如果设置了 AstrBot `callback_api_base`，aiocqhttp 将后台拉取并原生发送文件（聊天中不显示链接）。
- 如果不使用回调、而想走本地路径：必须保证 Napcat 能直接读取该路径（容器需挂载，或本机路径一致），并配置 `platform_settings.path_mapping` 将宿主路径映射为适配器可见路径。

小贴士：
- 设置回调地址：`/filehub set_callback http://127.0.0.1:6185`
- 使用 LLM：给出关键词即可，LLM 会先 `search_local_files` 再 `send_local_file_by_id`
- YAML 索引不再支持，仅支持 `registry.json`

开源规范与质量：
- 代码遵循 AstrBot 插件开发文档与事件模型
- 仅依赖 Python 标准库，无额外第三方包
- 出错时给出明确提示，不影响主流程；空结果/权限不足会清晰说明
- 配置项最小可用，默认即开箱使用；高级能力可通过命令/配置开启

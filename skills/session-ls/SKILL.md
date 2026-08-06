---
name: session-ls
description: >-
  跨 agent 的历史会话检索工具。当用户想回忆/查找"我之前做过什么""某个任务在哪个 agent 里做过""帮我找出以前关于 X 的对话/session"，或需要定位某个 session 文件路径时使用。覆盖 pi、codex、claude、cursor 四个 agent 的本地会话存储。用法: session-ls [关键词] [-f] [-a AGENT] [-c CWD] [--since DATE] [--until DATE] [-n N] [-l] [--json]。不带关键词列出全部会话(按最后活跃倒序)，带关键词默认只搜标题(首个真实用户消息)，-f 全文检索(rg)。语义判断交给大模型自己:找到候选后用文件路径读取原始内容。
---

# session-ls

列出并检索本机所有 coding agent（pi / codex / claude / cursor）的历史会话。

## 何时使用

- 用户问"我之前是不是做过 X""哪个 session 处理过 Y"
- 需要跨 agent 查找过去的任务、排错记录、方案讨论
- 需要拿到某个 session 的原始文件路径做进一步分析

## 基本用法

```bash
session-ls                      # 全部会话，按最后活跃倒序
session-ls <关键词>              # 搜标题（首个真实用户消息），大小写不敏感子串
session-ls <关键词> -f           # 全文检索（慢，几 GB 日志，用 rg）
```

## 常用组合

```bash
session-ls <关键词> -a pi        # 只看某个 agent
session-ls <关键词> -c <目录子串>  # 只看某个项目目录下的会话
session-ls <关键词> --since 2026-08-01   # 时间过滤
session-ls <关键词> -n 10        # 只看最新 N 条
session-ls <关键词> -l           # 只输出文件路径，可接其他命令
session-ls <关键词> --json       # JSONL 输出（agent/cwd/started/last/title/file）
```

## 输出说明

- 每行: agent、开始时间、最后活跃、工作目录、标题(首个真实用户消息)
- 标题会跳过注入的系统指令（如 codex 的 `<recommended_plugins>`、AGENTS.md instructions）
- 缓存位于 `~/.cache/session_ls_cache.json`，按 size+mtime 失效，重复查询毫秒级
- cursor 会话无文件内时间戳，显示 mtime；cwd 从目录名反解可能不精确（仅显示影响）

## 配合大模型工作流

```bash
session-ls <关键词> -l | xargs head -1     # 快速查看匹配 session 的原始头部
session-ls <关键词> --json | jq -r .file   # 取文件路径
```

拿到文件路径后，用 read 工具读取对应 session 文件，提取用户原始需求、决策和结论，再组织回答。不要凭标题臆断内容，复杂任务要读文件确认。

## 注意

- 这是纯本地、无语义检索工具；不做 RAG，不做向量化，不联网
- 结果排序是"最后活跃"倒序，不是相关度
- 需要时用 `-f` 全文搜，但几 GB 日志要几秒，先试标题搜索

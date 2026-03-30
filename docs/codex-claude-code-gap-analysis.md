# Codex 与 Claude Code 适配差异与改造点（前因后果清单）

更新时间：2026-03-30

## 1. 目的与范围

本文只记录：

- 现有桥接器中哪些点是按 `codex` 设计、对 `claude code` 不适配
- 每个问题的前因、现象、后果
- 需要改造的点（不展开详细设计，不直接给实现方案）

## 2. 现状结论（TL;DR）

- 当前系统是“`Codex app-server` 主模型 + `Claude Code CLI` 兼容接入”架构。
- `claude_code` 已具备基础可对话能力，但很多会话控制、状态治理、账号策略仍沿用 Codex 假设。
- 若不做进一步改造，`claude_code` 会持续出现“能聊天但管理能力不完整/行为不一致”的问题。

## 3. 两类 Agent 的本质差异（导致不适配的根因）

### 3.1 运行时模型差异

- Codex：长期驻留 `app-server`，有线程状态流、turn 事件流、可中断/可 steer。
- Claude Code：CLI 调用为主（即便可 `--resume`），默认是“请求-返回”模型。

直接后果：

- 同一套“线程/turn 管控”接口语义不完全等价。

### 3.2 会话控制接口差异

- Codex 有原生内部命令流（如 `/status`、`/model use`、`/effort` 等工作流被广泛依赖）。
- Claude Code 的 CLI 不是这套 slash 命令语义。

直接后果：

- 会话管理面板沿用 Codex 命令时，Claude 下会出现无效或误导。

### 3.3 配额与健康信息差异

- Codex 可读 `rate_limits`（primary/secondary，usedPercent 等）。
- Claude Code 不暴露同构的 Codex 配额结构。

直接后果：

- 自动切号与健康检查如果按 Codex 配额逻辑统一执行，会在 Claude 上失真。

### 3.4 权限与沙箱模型差异

- Codex：`sandbox` + `approval_policy`。
- Claude Code：`permission-mode` + tools allow/deny + `--add-dir`。

直接后果：

- 两侧权限能力不能靠同一字段直接映射为“等价行为”。

## 4. 问题清单（前因 -> 现象 -> 后果 -> 需要改造）

## 4.1 P0：会话管理卡片仍是 Codex 专用流程

前因：

- 会话管理 UI 和动作全部按 Codex slash 命令设计。

现象：

- Claude 会话里仍显示 `/status`、`/approvals`、`/permissions`、`/model list`、`/effort` 按钮流程。

后果：

- 用户会触发不适配命令，造成“功能失效/反馈混乱”。

需要改造：

- 会话管理动作按 `agent_provider` 分流，至少拆成 Codex 面板与 Claude 面板两套能力入口。

代码位置：

- `long_conn.py:2351`
- `long_conn.py:2380`
- `long_conn.py:2890`
- `long_conn.py:2902`
- `long_conn.py:2956`

## 4.2 P0：Claude turn 生命周期仍是同步阻塞模型

前因：

- `claude_code` 通过单次 CLI 执行获取结果，桥内没有真正异步事件流。

现象：

- “运行中/中断/steer”语义与 Codex 不一致，进度与并发控制能力弱化。

后果：

- 长任务可观测性差，控制体验和 Codex 不一致，用户容易误判系统卡死。

需要改造：

- 为 Claude provider 建立可追踪的 turn 生命周期模型（至少实现真实运行态与可靠取消语义）。

代码位置：

- `app.py:650`
- `app.py:721`
- `app.py:864`
- `app.py:867`
- `app.py:4991`

## 4.3 P0：Claude 会话恢复 ID 未纳入 runtime 持久态

前因：

- Claude `session_id` 存在 adapter 内存结构中，未进入统一 runtime state。

现象：

- 进程重启后，表面仍有线程信息，但 Claude 侧上下文可能已断。

后果：

- 出现“看似续聊，实际新会话”的上下文丢失。

需要改造：

- 把 Claude `session_id` 纳入 `chat state` 持久化与恢复流程，作为 resume 的一等字段。

代码位置：

- `app.py:543`
- `app.py:576`
- `app.py:783`
- `app.py:1385`
- `app.py:4470`

## 4.4 P0：自动切号策略未按 provider 语义完全分离

前因：

- 自动切号核心触发点仍高度依赖 Codex 风格 `rate_limits`。

现象：

- Claude provider 也可能触发同一套切号流程，但依据并不稳定。

后果：

- 切号时机不准确，可能误切、漏切或跨 provider 产生不可预期状态。

需要改造：

- 自动切号触发器按 provider 区分：
- Codex 走 quota 驱动
- Claude 走错误分类+健康状态驱动

代码位置：

- `app.py:4191`
- `app.py:4222`
- `app.py:4915`
- `app.py:4922`
- `app.py:5124`

## 4.5 P0：`check-one` 临时探测 provider 固定为 codex

前因：

- 非本地存储探测时，临时 profile 安装硬编码 `provider="codex"`。

现象：

- Claude pool/pending 账号可能按 Codex 路径探测，结果偏差。

后果：

- 控制面健康结果不可信，影响分配与运维决策。

需要改造：

- 探测链路应从 auth 元数据继承真实 provider，不得在 probe 阶段降级成 codex。

代码位置：

- `app.py:3736`
- `app.py:3747`

## 4.6 P1：Claude 路径未消费附件 `image_paths`

前因：

- Claude turn 调用仅传递文本 prompt，未把附件路径纳入 CLI 输入流程。

现象：

- 飞书附件在 Claude provider 下“暂存了但不生效”。

后果：

- 多模态输入能力在 Claude 下缺失，与 Codex 行为不一致。

需要改造：

- Claude provider 增加附件输入桥接（与当前附件暂存机制打通）。

代码位置：

- `app.py:721`
- `app.py:751`
- `app.py:4991`

## 4.7 P1：状态展示结构仍偏 Codex tokenUsage schema

前因：

- `long_conn` 的 status 文本提取按 Codex `tokenUsage.total/last/modelContextWindow` 写死。

现象：

- Claude provider 下 token/usage 信息不完整或显示异常。

后果：

- 状态页可读性下降，问题定位困难。

需要改造：

- 统一 usage 展示抽象层，按 provider 渲染不同字段，不再假设 `tokenUsage.total/last` 恒存在。

代码位置：

- `long_conn.py:2035`
- `long_conn.py:2048`
- `long_conn.py:2076`

## 4.8 P1：MCP 安装/同步流程只覆盖 Codex CLI

前因：

- 运行时 MCP server 安装通过 `codex mcp ...` 执行，Claude 侧无对等流程。

现象：

- Claude provider 对同一桥接能力（如文件回传）依赖路径不明确。

后果：

- 跨 provider 工具能力不一致，操作预期不可控。

需要改造：

- MCP 注册与注入策略按 provider 明确区分并保证能力对齐。

代码位置：

- `app.py:1745`
- `app.py:1758`
- `app.py:1853`

## 4.9 P1：错误分类文案和规则仍偏 Codex token/refresh 语义

前因：

- 账号错误分类规则主要来自 Codex 登录态场景。

现象：

- Claude API key 类错误与 Codex refresh-token 类错误可能混在同一分类出口。

后果：

- `needs_reauth/temp_disabled/deactivated` 判断边界不准，影响可用性。

需要改造：

- 错误分类按 provider 扩展独立规则集，减少跨 provider 误判。

代码位置：

- `app.py:1939`
- `app.py:4236`

## 4.10 P1：文档与默认说明仍以 Codex 为中心

前因：

- README 与交互文案长期沿用 Codex 主叙事。

现象：

- 用户理解“Claude Code 的支持边界”成本高，容易误操作。

后果：

- 反馈大量“明明切过去了但功能不一致”的使用问题。

需要改造：

- 文档按 provider 明确能力矩阵、差异与限制，避免同一入口给出等价承诺。

代码位置：

- `README.md:3`
- `README.md:7`
- `README.md:255`
- `long_conn.py:6`
- `long_conn.py:2380`

## 5. 与近期事故的对应关系（前因后果复盘）

本次你遇到的两类问题可以映射到上面清单：

1) “Claude 只会聊天，不知道项目文件”

- 前因：早期走了纯 Anthropic 消息接口路径，没有 Claude CLI 工具/目录能力。
- 后果：模型失去代码代理上下文，只能文本闲聊。
- 对应清单：4.2、4.6、4.8。

2) “切回 Codex 后被 Claude 模型配置污染”

- 前因：provider 切换时模型字段未严格按 provider 重同步。
- 后果：Codex runtime 读到 Claude 模型名并报错。
- 对应清单：4.4（状态治理相关）+ provider 切换一致性问题。

## 6. 改造优先级建议（仅列点，不展开设计）

P0（先做）：

- 4.1 provider 分流会话管理入口
- 4.2 Claude turn 生命周期能力补齐
- 4.3 Claude session_id 持久化
- 4.4 provider-aware 自动切号
- 4.5 check-one provider 纠偏

P1（随后）：

- 4.6 Claude 附件输入
- 4.7 usage 展示抽象
- 4.8 MCP 跨 provider 对齐
- 4.9 错误分类规则分流
- 4.10 文档与文案收敛


# FeiCodex Rocket Bridge

基于 `codex app-server` 的飞书机器人桥接服务（独立版）。

## 功能特性

- 文本消息直通 Codex。
- 附件消息自动下载并暂存，供下一轮对话使用。
- 菜单点击可打开项目/会话管理卡片。
- 卡片交互支持项目切换、会话管理、模型切换、账号切换等流程。
- 长回复使用 Feishu `schema 2.0` markdown 卡片渲染。
- 支持 OpenClaw 风格的 CardKit streaming card 和消息 reaction typing 状态。
- 会话在无任务执行且 10 分钟无新消息时自动回收，后续可通过 Codex `resume` 恢复。

## 前置条件

- Linux 主机（安装 `python3` 和 `venv`）
- 已安装 Codex CLI，并可通过 `codex` 命令调用
- 已完成 Codex 登录：`codex login`
- 已创建飞书应用并拿到凭证：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`

## 快速开始

```bash
./scripts/init.sh
```

然后编辑 `.env`，至少填写以下配置：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`

分别在两个终端启动：

```bash
./scripts/run_api.sh
```

```bash
./scripts/run_bridge.sh
```

## 飞书后台配置

在飞书应用后台的「事件与回调」中配置：

- 订阅事件：`im.message.receive_v1`
- 订阅事件：`application.botmenu.v6`
- 回调配置：`card.action.trigger`

在应用菜单中建议保留以下 `event_key`：

- `menu_project_manage`
- `menu_session_manage`

菜单键映射通过 `.env` 里的 `BRIDGE_MENU_ACTIONS_JSON` 配置。

建议开通的应用身份权限：

- `im:message`
- `im:message:send_as_bot`
- `im:message:readonly`
- `im:message.p2p_msg:readonly`
- `im:message.group_at_msg:readonly`
- `im:message:update`
- `im:chat.members:bot_access`
- `im:chat.access_event.bot_p2p_chat:read`
- `im:resource`
- `im:resource:upload`
- `cardkit:card:read`
- `cardkit:card:write`

如果后台有单独的消息表情回应权限，也建议一并开通；typing 状态使用的是消息 reaction API。

## 环境变量

请参考 [.env.example](./.env.example)。默认值均为仓库内相对路径：

- 状态文件：`./data/state.json`
- 历史文件：`./data/history.json`
- 历史数据库：`./data/history.db`
- 上传目录：`./data/uploads`
- 账号源文件目录：`./data/auth_profiles`
- 账号运行目录：`./data/auth_homes`
- 默认工作目录：`.`
- 默认模型：`BRIDGE_DEFAULT_MODEL=gpt-5.3-codex`
- 默认沙箱：`BRIDGE_DEFAULT_SANDBOX=danger-full-access`
- 默认审批策略：`BRIDGE_DEFAULT_APPROVAL_POLICY=never`
- 默认人格：`BRIDGE_DEFAULT_PERSONALITY=pragmatic`
- 单轮超时：`BRIDGE_TURN_TIMEOUT_SEC=21600`（默认 6 小时）
- 进度刷新间隔：`BRIDGE_PROGRESS_PING_INTERVAL_SEC=180`（默认每 3 分钟）
- streaming card 刷新间隔：`BRIDGE_STREAMING_CARD_UPDATE_INTERVAL_SEC=5`（默认每 5 秒）
- streaming card 打印频率：`BRIDGE_STREAMING_CARD_PRINT_FREQUENCY_MS=1`
- streaming card 单次打印步长：`BRIDGE_STREAMING_CARD_PRINT_STEP=4096`
- 历史最多保留：`BRIDGE_HISTORY_MAX_TURNS=2000`
- 空闲会话回收：`BRIDGE_IDLE_EVICT_SEC=600`（默认 10 分钟）
- 回收扫描间隔：`BRIDGE_IDLE_SWEEP_INTERVAL_SEC=60`（默认 60 秒）
- 自动切号：`BRIDGE_AUTO_AUTH_SWITCH_ENABLED=true`
- 自动切号阈值：`BRIDGE_AUTO_AUTH_SWITCH_THRESHOLD_PCT=100`
- streaming card：`BRIDGE_STREAMING_CARD_ENABLED=true`
- typing reaction：`BRIDGE_TYPING_REACTION_ENABLED=true`
- 卡片按钮后自动删卡：`BRIDGE_CARD_AUTO_DELETE_ON_ACTION=true`
- 输出文件自动回传：`BRIDGE_OUTPUT_FILE_AUTO_SEND=false`
- 输出文件数量上限：`BRIDGE_OUTPUT_FILE_MAX_COUNT=0`
- 输出文件大小上限：`BRIDGE_OUTPUT_FILE_MAX_SIZE_MB=30`
- 输出文件扫描年龄：`BRIDGE_OUTPUT_FILE_MAX_AGE_SEC=3600`
- 默认文件回传 MCP 名称：`BRIDGE_MCP_SERVER_NAME=feishu-bridge-files`
- MCP 文件允许目录：`BRIDGE_MCP_FILE_ALLOWED_DIRS=/root/bridgespace/projects`
- MCP 文件大小上限：`BRIDGE_MCP_FILE_MAX_SIZE_MB=30`

`app.py` 和 `long_conn.py` 都会自动加载 `.env`。

## HTTP 控制 API

默认前缀：`/appbridge/api`（可通过 `BRIDGE_API_PREFIX` 修改）

- `GET /chat/{chat_id}/status`
- `GET /history`
- `GET /auth/profiles`
- `POST /chat/{chat_id}/config`
- `POST /chat/{chat_id}/auth-profile`
- `POST /chat/{chat_id}/thread/reset`
- `POST /chat/{chat_id}/turn`
- `POST /chat/{chat_id}/turn/steer`
- `POST /chat/{chat_id}/interrupt`

鉴权头：

- `Authorization: Bearer <BRIDGE_API_TOKEN>`

## 历史回溯页

- 固定入口：`GET /history/entry`
- 页面地址：`GET /history`
- 登出：`GET /history/logout`

网页授权相关环境变量：

- `HISTORY_ALLOWED_OPEN_IDS=ou_xxx`
- `HISTORY_SESSION_SECRET=...`
- `HISTORY_SESSION_TTL_SEC=604800`
- `HISTORY_COOKIE_NAME=feicodex_history_session`
- `FEISHU_OAUTH_AUTHORIZE_URL=...`
- `FEISHU_OAUTH_TOKEN_URL=...`
- `FEISHU_OAUTH_USERINFO_URL=...`

页面会按：

- 项目
- 会话（chat）
- turn 记录

展示最近历史，并包含：

- 用户输入
- 最终回复
- 中间过程事件
- 失败错误（如果有）

## 历史 API

对机器侧保留：

- `GET /appbridge/api/history?offset=0&limit=50`

对网页侧新增分层接口：

- `GET /history/api/projects`
- `GET /history/api/sessions?project=<name>`
- `GET /history/api/turns?project=<name>&chat_id=<id>&offset=0&limit=50`
- `GET /history/api/turn?turn_id=<id>`

说明：

- 结构按 `项目 -> 会话 -> 轮次` 拆开
- 支持 `offset + limit` 分页
- `turns` 默认不返回过程事件；加 `include_events=true` 才返回
- 过程记录改为前端按需懒加载，展开某一轮时再请求单轮明细
- 底层历史已切到 SQLite，旧的 `history.json` 只作为迁移来源/备份

## 冒烟测试

```bash
./.venv/bin/python smoke_test.py
```

## 多账号切换

将账号认证文件放到：

- `data/auth_profiles/<profile>.auth.json`

可选配套：

- `data/auth_profiles/<profile>.config.toml`

示例：

- `data/auth_profiles/work.auth.json`
- `data/auth_profiles/personal.auth.json`

桥接会自动校验这些账号能否通过 `codex login status`，并把可用结果写入：

- `data/auth_profiles_registry.json`

实际运行时，每个账号会使用独立 `CODEX_HOME`：

- `data/auth_homes/<profile>/`

在飞书里通过：

- `会话管理 -> 切换账号`

即可切换当前 chat 绑定的账号。切换后会自动重置线程，后续消息使用新的账号继续运行。

如果开启 `BRIDGE_AUTO_AUTH_SWITCH_ENABLED=true`，当当前账号额度达到阈值（默认 `100%`）时，桥接会自动切到下一个可用账号，并在返回消息顶部提示切换结果。

## systemd 部署（可选）

模板文件：

- `feicodex-rocket-api.service.example`
- `feicodex-rocket-bridge.service.example`

安装前请将模板中的 `__APP_DIR__` 替换为项目绝对路径。

示例：

```bash
APP_DIR="$(pwd)"
sed "s|__APP_DIR__|$APP_DIR|g" feicodex-rocket-api.service.example > /etc/systemd/system/feicodex-rocket-api.service
sed "s|__APP_DIR__|$APP_DIR|g" feicodex-rocket-bridge.service.example > /etc/systemd/system/feicodex-rocket-bridge.service
systemctl daemon-reload
systemctl enable --now feicodex-rocket-api.service feicodex-rocket-bridge.service
```

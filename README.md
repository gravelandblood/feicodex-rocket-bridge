# FeiCodex Rocket Bridge

基于 `codex app-server` 的飞书机器人桥接服务（独立版）。

## 功能特性

- 文本消息直通 Codex。
- 附件消息自动下载并暂存，供下一轮对话使用。
- 菜单点击可打开项目/会话管理卡片。
- 卡片交互支持项目切换、会话管理、模型切换、账号切换等流程。
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

## 环境变量

请参考 [.env.example](./.env.example)。默认值均为仓库内相对路径：

- 状态文件：`./data/state.json`
- 上传目录：`./data/uploads`
- 账号源文件目录：`./data/auth_profiles`
- 账号运行目录：`./data/auth_homes`
- 默认工作目录：`.`
- 单轮超时：`BRIDGE_TURN_TIMEOUT_SEC=21600`（默认 6 小时）
- 进度刷新间隔：`BRIDGE_PROGRESS_PING_INTERVAL_SEC=180`（默认每 3 分钟）
- 空闲会话回收：`BRIDGE_IDLE_EVICT_SEC=600`（默认 10 分钟）
- 回收扫描间隔：`BRIDGE_IDLE_SWEEP_INTERVAL_SEC=60`（默认 60 秒）

`app.py` 和 `long_conn.py` 都会自动加载 `.env`。

## HTTP 控制 API

默认前缀：`/appbridge/api`（可通过 `BRIDGE_API_PREFIX` 修改）

- `GET /chat/{chat_id}/status`
- `GET /auth/profiles`
- `POST /chat/{chat_id}/auth-profile`
- `POST /chat/{chat_id}/thread/reset`
- `POST /chat/{chat_id}/turn`
- `POST /chat/{chat_id}/interrupt`

鉴权头：

- `Authorization: Bearer <BRIDGE_API_TOKEN>`

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

# FeiCodex 项目看板 / 历史页接口文档

## 1. 文档目的

这份文档面向两类人：

- 产品/设计：理解这个页面要解决什么问题，页面应该承载哪些信息。
- 前端/集成：基于现有接口完成新的视觉设计、交互设计和页面集成。

这份文档对应的后端能力已经存在，重点是为新的项目看板与历史查看页面提供稳定的数据契约。

## 2. 页面要达成的目的

这个页面不是聊天窗口，而是一个面向长期使用的项目观察台。它要解决以下问题：

1. 让用户从多个项目中快速判断当前整体状态。
2. 让用户进入某个项目后，快速看懂最近有哪些会话、哪些任务刚完成、哪些任务失败了。
3. 让用户在会话层看到“人能读懂”的摘要，而不是只看到内部 ID。
4. 让用户进入某个会话后，按自然时间顺序回看每一轮对话。
5. 让用户默认先看到最新的轮次，但保留从最旧到最新的完整时间线。
6. 让用户在默认视图只看“用户提问 + 最终回答”，过程记录按需展开，避免信息爆炸。
7. 让项目总览页与原始历史页保持连通，用户可以继续深挖历史。

## 3. 适用范围

本页聚焦于以下数据：

- 项目列表
- 项目下的会话列表
- 会话下的轮次列表
- 单轮的过程记录详情

本页当前不负责：

- 实时任务流式推送
- 任务调度和操作控制
- 代码 diff 展示
- Git 统计、构建状态、服务探针

这些能力如果未来需要，可以在项目看板层继续向上扩展。

## 4. 信息架构

建议页面按三层结构组织：

1. 项目层
   - 显示项目名、最近活跃时间、轮次数、会话数。
2. 会话层
   - 显示该项目下的会话摘要。
   - 主展示文本应该是最近一轮的用户问题或结果摘要，而不是 `chat_id`。
3. 轮次层
   - 显示某个会话的完整轮次时间线。
   - 顺序固定为从最旧到最新。
   - 默认定位到最下方，也就是最新的一段。

建议默认交互路径：

1. 首屏拉取项目列表
2. 默认选中最近活跃项目
3. 拉取该项目的会话列表
4. 默认选中该项目中最近更新的会话
5. 轮次层默认显示该会话的最后一段分页数据
6. 用户按需上翻更早轮次
7. 用户展开某一轮时，再懒加载该轮的过程记录

## 5. 鉴权与访问方式

历史页当前支持两种访问方式：

1. 飞书网页登录态
   - 入口：`GET /history/entry`
   - 登录成功后进入：`GET /history`
2. API Token
   - 页面和接口都可以通过 `token` query 参数或 `Authorization: Bearer <token>` 访问

设计新页面时，建议默认使用网页登录态，不在页面设计中暴露 token 概念。

## 6. 接口总览

### 页面入口

- `GET /history/entry`
- `GET /history`
- `GET /history/logout`

### 数据接口

- `GET /history/api/projects`
- `GET /history/api/sessions`
- `GET /history/api/turns`
- `GET /history/api/turn`

## 7. 通用约定

### 7.1 返回结构

所有成功接口统一返回：

```json
{
  "ok": true,
  "data": {}
}
```

失败时统一返回：

```json
{
  "ok": false,
  "error": "..."
}
```

### 7.2 时间字段

所有时间字段均为 Unix 时间戳，单位为秒。

前端建议统一格式化为本地时间，例如：

- `2026-03-09 14:23:11`

### 7.3 分页规则

所有列表接口统一使用：

- `offset`
- `limit`

分页返回格式：

```json
{
  "offset": 0,
  "limit": 50,
  "total": 120,
  "has_more": true
}
```

说明：

- `offset` 表示当前页起始偏移
- `limit` 表示当前请求页大小
- `total` 表示总记录数
- `has_more` 表示是否还有下一页

## 8. 接口契约

### 8.1 获取项目列表

`GET /history/api/projects`

#### Query 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `offset` | int | 否 | `0` | 起始偏移 |
| `limit` | int | 否 | `50` | 页大小 |
| `token` | string | 否 | `""` | API Token，可省略 |

#### 返回示例

```json
{
  "ok": true,
  "data": {
    "projects": [
      {
        "name": "test",
        "started_at": 1772864032,
        "updated_at": 1773034447,
        "turn_count": 29,
        "session_count": 1
      }
    ],
    "pagination": {
      "offset": 0,
      "limit": 50,
      "total": 1,
      "has_more": false
    }
  }
}
```

#### 字段说明

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | string | 项目名，是项目层主键 |
| `started_at` | int | 项目最早一轮的时间 |
| `updated_at` | int | 项目最近活跃时间 |
| `turn_count` | int | 项目内轮次总数 |
| `session_count` | int | 项目内会话总数 |

#### 设计建议

- 项目卡片主标题使用 `name`
- 次级信息使用 `updated_at`
- 辅助统计可以显示 `session_count` 和 `turn_count`
- 默认优先选中最近活跃的项目，不建议按字母排序当默认聚焦

### 8.2 获取某个项目下的会话列表

`GET /history/api/sessions`

#### Query 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `project` | string | 是 | `""` | 项目名 |
| `offset` | int | 否 | `0` | 起始偏移 |
| `limit` | int | 否 | `50` | 页大小 |
| `token` | string | 否 | `""` | API Token，可省略 |

#### 返回示例

```json
{
  "ok": true,
  "data": {
    "project": "test",
    "sessions": [
      {
        "project": "test",
        "chat_id": "oc_xxx",
        "cwd": "/root/bridgespace/projects/test",
        "model": "gpt-5.4",
        "auth_profile": "0301",
        "started_at": 1772864032,
        "updated_at": 1773034447,
        "turn_count": 29,
        "latest_turn_id": "019cd114-0dae-7ca3-8518-caf95907bd02",
        "latest_status": "completed",
        "latest_started_at": 1773034278,
        "latest_ended_at": 1773034447,
        "latest_updated_at": 1773034447,
        "latest_user_text": "再测一次：你随便模拟一个需要大约3分钟才能完成的任务，需要有一些中间过程",
        "latest_user_preview": "再测一次：你随便模拟一个需要大约3分钟才能完成的任务，需要有一些中间过程",
        "latest_assistant_preview": "这轮模拟任务已完成，完整跑了约 2 分 20 秒...",
        "latest_error_preview": "",
        "display_title": "再测一次：你随便模拟一个需要大约3分钟才能完成的任务，需要有一些中间过程",
        "display_preview": "这轮模拟任务已完成，完整跑了约 2 分 20 秒..."
      }
    ],
    "pagination": {
      "offset": 0,
      "limit": 50,
      "total": 1,
      "has_more": false
    }
  }
}
```

#### 字段说明

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `project` | string | 所属项目 |
| `chat_id` | string | 会话内部标识，用于拉取轮次，不适合直接给用户当主标题 |
| `cwd` | string | 该会话最近一轮使用的工作目录 |
| `model` | string | 最近一轮的模型名 |
| `auth_profile` | string | 最近一轮的账号配置名 |
| `started_at` | int | 会话最早时间 |
| `updated_at` | int | 会话最近更新时间 |
| `turn_count` | int | 轮次数 |
| `latest_turn_id` | string | 最新一轮的唯一标识 |
| `latest_status` | string | 最新一轮状态，例如 `completed` / `failed` |
| `latest_started_at` | int | 最新一轮开始时间 |
| `latest_ended_at` | int | 最新一轮结束时间 |
| `latest_updated_at` | int | 最新一轮更新时间 |
| `latest_user_text` | string | 最新一轮用户原始输入 |
| `latest_user_preview` | string | 最新一轮用户输入摘要 |
| `latest_assistant_preview` | string | 最新一轮最终回复摘要 |
| `latest_error_preview` | string | 最新一轮错误摘要 |
| `display_title` | string | 推荐给 UI 当主标题使用的人类可读文本 |
| `display_preview` | string | 推荐给 UI 当副标题使用的摘要 |

#### 设计建议

- 主标题优先使用 `display_title`
- 副标题优先使用 `display_preview`
- 状态标签使用 `latest_status`
- 时间使用 `latest_updated_at`
- `chat_id` 只作为内部跳转键或次级信息展示，不应成为视觉主标题
- 如果需要“失败态”视觉，可以在 `latest_error_preview` 非空时加危险标记

### 8.3 获取某个会话的轮次列表

`GET /history/api/turns`

#### Query 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `project` | string | 是 | `""` | 项目名 |
| `chat_id` | string | 是 | `""` | 会话 ID |
| `offset` | int | 否 | `0` | 起始偏移 |
| `limit` | int | 否 | `50` | 页大小 |
| `include_events` | bool | 否 | `false` | 是否直接携带过程事件 |
| `token` | string | 否 | `""` | API Token，可省略 |

#### 返回示例

```json
{
  "ok": true,
  "data": {
    "project": "test",
    "chat_id": "oc_xxx",
    "turns": [
      {
        "id": "turn_1773034278000",
        "project": "test",
        "chat_id": "oc_xxx",
        "turn_id": "019cd114-0dae-7ca3-8518-caf95907bd02",
        "status": "completed",
        "started_at": 1773034278,
        "ended_at": 1773034447,
        "duration_sec": 169,
        "user_text": "再测一次：你随便模拟一个需要大约3分钟才能完成的任务，需要有一些中间过程",
        "assistant_text": "这轮模拟任务已完成，完整跑了约 2 分 20 秒...",
        "error_text": "",
        "events_count": 12
      }
    ],
    "pagination": {
      "offset": 0,
      "limit": 50,
      "total": 29,
      "has_more": false
    }
  }
}
```

#### 字段说明

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 历史库内部 ID |
| `project` | string | 所属项目 |
| `chat_id` | string | 所属会话 |
| `turn_id` | string | 轮次 ID，可用于单轮详情接口 |
| `status` | string | 轮次状态 |
| `started_at` | int | 提问时间 |
| `ended_at` | int | 完成时间 |
| `duration_sec` | int | 持续时长，单位秒 |
| `user_text` | string | 用户输入 |
| `assistant_text` | string | 最终回复 |
| `error_text` | string | 错误文本 |
| `events_count` | int | 过程事件数 |
| `events` | array | 仅在 `include_events=true` 时出现 |

#### 顺序约定

- 轮次顺序固定为从最旧到最新
- 前端默认应该定位到最后一段，也就是最新轮次所在区域
- 如果做“加载更早轮次”，应该向上补，而不是打乱当前顺序

#### 设计建议

- 默认只展示：
  - 用户提问
  - 最终回答
  - 时间信息
  - 状态信息
- 过程记录建议折叠展示
- 不建议首屏直接请求 `include_events=true`

### 8.4 获取单轮详情

`GET /history/api/turn`

#### Query 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `turn_id` | string | 是 | `""` | 轮次 ID 或内部 ID |
| `include_events` | bool | 否 | `true` | 是否返回事件列表 |
| `token` | string | 否 | `""` | API Token，可省略 |

#### 返回示例

```json
{
  "ok": true,
  "data": {
    "turn": {
      "id": "turn_1773034278000",
      "project": "test",
      "chat_id": "oc_xxx",
      "thread_id": "thread_xxx",
      "turn_id": "019cd114-0dae-7ca3-8518-caf95907bd02",
      "cwd": "/root/bridgespace/projects/test",
      "model": "gpt-5.4",
      "auth_profile": "0301",
      "status": "completed",
      "started_at": 1773034278,
      "ended_at": 1773034447,
      "duration_sec": 169,
      "user_text": "再测一次：你随便模拟一个需要大约3分钟才能完成的任务，需要有一些中间过程",
      "assistant_text": "这轮模拟任务已完成，完整跑了约 2 分 20 秒...",
      "error_text": "",
      "events_count": 12,
      "events": [
        {
          "ts": 1773034288,
          "text": "正在读取项目文件"
        }
      ]
    }
  }
}
```

#### 字段说明

单轮详情在轮次列表字段基础上，额外补充：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `thread_id` | string | 该轮所属线程 ID |
| `cwd` | string | 工作目录 |
| `model` | string | 模型 |
| `auth_profile` | string | 账号配置 |
| `events` | array | 过程事件列表 |

#### 过程事件结构

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ts` | int | 事件时间戳，秒 |
| `text` | string | 事件描述文本 |

#### 设计建议

- 仅在用户展开某一轮后请求这个接口
- 过程记录适合显示成时间线、事件列表或折叠明细
- `error_text` 建议在失败时单独高亮

## 9. 推荐的数据加载策略

建议新的前端页面按以下顺序请求：

1. `GET /history/api/projects`
2. 选择一个项目后：`GET /history/api/sessions?project=...`
3. 选择一个会话后：`GET /history/api/turns?project=...&chat_id=...&offset=...&limit=...&include_events=false`
4. 用户展开某一轮后：`GET /history/api/turn?turn_id=...&include_events=true`

这个顺序的设计目标是：

- 首屏足够轻
- 不一次性拉全量历史
- 默认只看重要信息
- 过程细节按需懒加载

## 10. 推荐的 UI 字段绑定方式

这部分不是视觉方案，而是给设计师和前端的字段使用建议。

### 10.1 项目卡片

- 标题：`name`
- 次级信息：`updated_at`
- 统计：`session_count`、`turn_count`

### 10.2 会话卡片

- 主标题：`display_title`
- 副标题：`display_preview`
- 状态角标：`latest_status`
- 时间：`latest_updated_at`
- 补充信息：`turn_count`、`model`、`auth_profile`
- 内部主键：`chat_id`

### 10.3 轮次卡片

- 标题：`user_text`
- 主内容：`assistant_text`
- 时间：`started_at`、`ended_at`
- 时长：`duration_sec`
- 状态：`status`
- 折叠入口：`events_count`

## 11. 与原始历史页的关系

新的项目看板不需要替代原始历史系统，而应该作为它的上层入口。

建议关系如下：

- 项目看板负责：
  - 项目总览
  - 会话摘要
  - 轮次浏览
  - 快速筛选和状态感知
- 原始历史页负责：
  - 完整回放
  - 深入查看单轮细节
  - 长期归档

如果新看板中需要“进入原始历史页”，建议携带以下上下文：

- `project`
- `chat_id`

这样可以在后续集成时，直接跳到对应项目和会话。

## 12. 设计时需要特别注意的约束

1. 会话主文案不要再直接展示 `chat_id`
2. 轮次默认只展示“提问 + 最终回答”
3. 过程记录必须可折叠
4. 顺序必须是从最旧到最新
5. 视觉上默认定位到最新一段
6. 页面要适合长期多项目使用，避免一次性堆满大量文本
7. 设计中应预留“更多项目 / 更多会话 / 更多轮次”的分页入口

## 13. 给 Gemini 的一句话任务定义

请基于这份接口文档，为 FeiCodex 设计一个面向长期多项目使用的项目看板与历史查看页面。它的核心目标不是聊天，而是帮助用户快速掌握多个项目的最新状态、进入某个项目的会话摘要，并继续深入查看按时间顺序排列的轮次历史。页面需要适合桌面端和移动端，强调清晰、耐久、可管理，而不是临时聊天感。

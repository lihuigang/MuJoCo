# HTTP API (`simulation-runtime/v1`)

Base URL 由部署决定，例如 `https://sim.example.internal`。**默认免令牌**：所有接口可直接访问，身份为公共项目 `public`，拥有全部 scopes。

启用 token 模式（`SIM_RUNTIME_AUTH_MODE=token`）后，除静态页面 `/`、`/viewer`、`/v1/auth-mode` 与探针 `/healthz`、`/readyz` 外，所有接口要求：

```
Authorization: Bearer <project-token>
```

令牌对应的 `project_id` 与 scopes 完全由服务端配置决定，客户端不能覆盖。跨项目访问统一返回 `404`，避免泄露资源是否存在。

当前模式可随时通过 `GET /v1/auth-mode` 查询。

## 通用约定

- JSON 请求/响应，时间戳为 Unix 秒（浮点）。
- ID 由服务端生成：`ses_...`、`op_...`、`obs_...`、`art_...`。
- 写操作支持 `Idempotency-Key` 请求头（≤200 字符）；也可在 JSON body 的 `idempotency_key` 字段给出，请求头优先。
- 写操作默认同步等待最多 `request_wait_sec`。完成返回 `200`（即使操作失败，也以 operation 状态表达）；未完成返回 `202`，带 `Location` 和 `Retry-After`。
- 错误响应统一为：

```json
{"error": {"code": "invalid_action", "message": "..."}}
```

## 探针

### `GET /healthz`

`200 {"ok": true}`。进程存活即可。

### `GET /readyz`

检查启用的后端能否加载、SQLite 能否查询。就绪返回 `200 {"ok": true}`；否则 `503 {"ok": false, "errors": {...}}`。

### `GET /v1/auth-mode`

无鉴权。返回当前认证模式：

```json
{"mode": "none", "token_required": false}
```

`mode` 为 `none`（默认，免令牌）或 `token`（需要 Bearer 令牌）。

## 查看页面

### `GET /`、`GET /viewer`

`/` 重定向到 `/viewer`。页面本身无鉴权（只返回静态 HTML），会先请求 `/v1/auth-mode`：免令牌模式下直接加载会话，不显示令牌输入框；token 模式下显示令牌输入框，并把令牌保存在浏览器标签页的 `sessionStorage` 中，不会出现在 URL 或服务端日志里。它轮询会话状态与最新观测，并排显示 `top_cam` / `wrist_cam`，支持新建 playground 会话、重置和关闭。

## 能力

### `GET /v1/capabilities`

```json
{
  "protocol": "simulation-runtime/v1",
  "service_version": "0.1.0",
  "backends": [
    {
      "name": "mujoco_so101",
      "tasks": [
        {
          "name": "pick_cube",
          "instruction": "Pick up the red cube and place it stably on the green target pad.",
          "native_action_limit": 300,
          "control_hz": 20.0,
          "cameras": [
            {"name": "top_cam", "width": 256, "height": 256, "media_type": "image/png"},
            {"name": "wrist_cam", "width": 256, "height": 256, "media_type": "image/png"}
          ]
        }
      ],
      "action_schemas": [
        {
          "name": "joint",
          "fields": {
            "arm_joint_state": {"shape": [5], "unit": "rad"},
            "ee_joint_state": {"shape": [1], "range": [0, 1], "unit": "normalized"}
          }
        },
        {
          "name": "ik",
          "fields": {
            "ee_position": {"shape": [3], "unit": "m", "frame": "world"},
            "ee_joint_state": {"shape": [1], "range": [0, 1], "unit": "normalized"}
          }
        }
      ],
      "coordinate_frame": "world (MuJoCo, metres)",
      "exec_compatible": true
    }
  ],
  "session_modes": ["typed", "exec"],
  "features": {"exec": true, "artifact_download": true, "async_operations": true, "live_viewer": true},
  "limits": {"max_sessions_per_project": 2, "execution_timeout_sec": 60.0, "request_wait_sec": 10.0}
}
```

`features.exec` 在免令牌模式下始终为 `true`；token 模式下反映当前令牌是否拥有 `exec` scope。后端加载失败时，对应条目为 `{"name": ..., "unavailable": true, "error": ...}`。

## 会话

### `POST /v1/sessions`

请求：

```json
{
  "backend": "mujoco_so101",
  "task": "pick_cube",
  "phase": "playground",
  "mode": "typed",
  "seed": 0,
  "exec_budget": null
}
```

字段：

| 字段 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `backend` | 否 | `mujoco_so101` | 必须是服务启用的后端 |
| `task` | 否 | `pick_cube` | 见 capabilities |
| `phase` | 否 | `playground` | `playground` / `test`；需要对应 scope |
| `mode` | 否 | `typed` | `typed` / `exec`；需要对应 scope |
| `seed` | 否 | `0` | 非负整数；`playground` 重置使用同一 seed |
| `exec_budget` | 否 | 服务默认值 | 仅 `exec` 模式；正整数且不超过上限 |

响应 `201`：

```json
{
  "session": {
    "session_id": "ses_...",
    "project_id": "demo",
    "task": "pick_cube",
    "phase": "playground",
    "mode": "typed",
    "seed": 0,
    "state": "active",
    "reason": "active",
    "success": false,
    "terminated": false,
    "truncated": false,
    "native_action_limit": 300,
    "native_steps_used": 0,
    "native_steps_remaining": 300,
    "execution_budget": 0,
    "executions_used": 0,
    "executions_remaining": 0,
    "observation_sequence": 0,
    "latest_observation_id": "obs_...",
    "created_at": 1730000000.0,
    "updated_at": 1730000000.1,
    "closed_at": null
  },
  "observation": { "...见观测..." }
}
```

错误：`429 capacity_exhausted`、`400 invalid_request`、`500 simulator_failure`（worker 初始化失败）。

### `GET /v1/sessions`

`?limit=100`（1–500）。返回 `{"sessions": [...]}`，只包含当前项目。

### `GET /v1/sessions/{session_id}`

返回 session 对象（结构同上）。状态字段：

| 字段 | 说明 |
| --- | --- |
| `state` | `starting` / `active` / `closed` / `interrupted` / `failed` |
| `reason` | `active`、`closed_by_client`、`session_ttl`、`execution_timeout`、`service_shutdown` 等 |
| `success` / `terminated` / `truncated` | 最近一次尝试的结果 |
| `native_steps_used` | 累计原生动作数（重置不清零） |
| `native_steps_remaining` | 当前尝试剩余动作数（playground 重置后续满） |
| `executions_used` / `executions_remaining` | 程序执行预算（仅 exec） |
| `observation_sequence` | 最新观测序号 |

### `POST /v1/sessions/{session_id}/steps`

`typed` 会话专用。需要 `typed` scope。

```json
{"action": {"arm_joint_state": [0, -0.4, 0.4, 0, 0], "ee_joint_state": [1.0]}}
```

或 IK：

```json
{"action": {"ee_position": [0.2, -0.1, 0.1], "ee_joint_state": [1.0]}}
```

动作键必须**恰好**是上述两组之一；向量长度、有限值、夹爪 `[0,1]` 会被校验。错误返回 `422 invalid_action`（同步校验）或操作失败 `invalid_action`。

成功响应（`200`）是一个 operation：

```json
{
  "operation_id": "op_...",
  "session_id": "ses_...",
  "kind": "step",
  "status": "succeeded",
  "result": {
    "applied": true,
    "already_terminal": false,
    "is_reset": false,
    "success": false,
    "terminated": false,
    "truncated": false,
    "native_steps_used": 1,
    "native_steps_remaining": 299,
    "control_hz": 20.0,
    "sequence": 1,
    "observation": { "...观测元数据..." }
  },
  "error": null,
  "outcome_uncertain": false,
  "idempotency_key": null,
  "created_at": 1730000000.0,
  "started_at": 1730000000.01,
  "completed_at": 1730000000.05
}
```

终态会话仍接受 step/reset 请求：`applied=false, already_terminal=true`，不推进物理世界、不消耗预算。

### `POST /v1/sessions/{session_id}/executions`

`exec` 会话专用。需要 `exec` scope。

```json
{"code": "print(get_instruction())\nstep({'ee_position': [0.2, -0.1, 0.1], 'ee_joint_state': [1.0]})"}
```

结果 `result`：

```json
{
  "ok": true,
  "error": null,
  "stdout": "Pick up the red cube...\n",
  "stderr": "",
  "observation": { "...观测元数据..." },
  "success": false,
  "terminated": false,
  "truncated": false,
  "native_steps_used": 1,
  "native_steps_remaining": 299,
  "executions_used": 1,
  "executions_remaining": 99,
  "sequence": 1
}
```

- 程序内普通异常 → `ok=false` 且 `error` 有值，会话继续。
- 预算耗尽 → 操作 `failed`，`error.code=budget_exhausted`，会话继续。
- 执行超时 → 操作 `failed`，`error.code=execution_timeout`，`outcome_uncertain=true`，会话变为 `interrupted`。
- 代码大小上限 256 KiB；只允许 `numpy`、`math` 导入和受限内置函数。

### `POST /v1/sessions/{session_id}/reset`

仅 `playground`。请求体可省略或传 `{}`。两种模式均可用。

```json
{
  "operation_id": "op_...",
  "status": "succeeded",
  "result": {
    "is_reset": true,
    "applied": true,
    "native_steps_used": 137,
    "native_steps_remaining": 300,
    "...": "..."
  }
}
```

`test` 阶段返回操作失败 `error.code=forbidden`，会话保持 `active`。

### `POST /v1/sessions/{session_id}/close`

请求：`{"reason": "closed_by_client"}`（可选）。关闭 worker，会话变 `closed`。历史观测与操作仍可读。重复关闭返回 `409 session_closed`。

## 操作

### `GET /v1/sessions/{session_id}/operations/{operation_id}`

返回 operation 对象，字段同写操作响应。`status` 取值：`pending`、`running`、`succeeded`、`failed`。

### `GET /v1/sessions/{session_id}/operations`

`?limit=50`（1–500），按创建时间倒序，返回 `{"operations": [...]}`。

## 观测与工件

### `GET /v1/sessions/{session_id}/observation`

```json
{
  "id": "obs_...",
  "session_id": "ses_...",
  "sequence": 1,
  "created_at": 1730000000.0,
  "observation_id": "obs_...",
  "instruction": "Pick up the red cube and place it stably on the green target pad.",
  "state": {
    "arm_joint_state": [0, -0.4, 0.4, 0, 0],
    "ee_joint_state": [1.0],
    "ee_pose": [0.2, -0.1, 0.12, 1, 0, 0, 0]
  },
  "action": {"arm_joint_state": [0, -0.4, 0.4, 0, 0], "ee_joint_state": [1.0]},
  "control_hz": 20.0,
  "cameras": {
    "top_cam": {
      "artifact_id": "art_...",
      "href": "/v1/artifacts/art_...",
      "media_type": "image/png",
      "width": 256,
      "height": 256
    }
  }
}
```

### `GET /v1/artifacts/{artifact_id}`

返回二进制 PNG，带 `Content-Type: image/png`、`Cache-Control: private, max-age=31536000, immutable`。只允许创建该工件所属项目访问。

## 错误码

| HTTP | `code` | 含义 |
| --- | --- | --- |
| 400 | `invalid_request` | 参数/schema 错误 |
| 401 | `unauthorized` | 缺失或无效令牌（仅 token 模式） |
| 403 | `forbidden` | scope 不足，或在 test 阶段重置 |
| 404 | `not_found` | 资源不存在或不属于本项目 |
| 409 | `budget_exhausted` | 执行预算耗尽（同步阶段） |
| 409 | `session_closed` | 会话已关闭 |
| 409 | `session_interrupted` | 会话已中断，不能继续 |
| 409 | `action_mode_mismatch` | 用错 steps/executions |
| 409 | `session_busy` | 同会话已有进行中的操作 |
| 409 | `idempotency_conflict` | 幂等键复用于不同请求 |
| 422 | `invalid_action` | 动作结构/取值范围错误 |
| 429 | `capacity_exhausted` | 全局或项目并发会话已满 |
| 500 | `simulator_failure` | 后端初始化/执行失败 |
| 500 | `internal_error` | 未预期服务错误 |

操作内部失败（例如 `forbidden`、`budget_exhausted`、`invalid_action`）通常以 `200` + `status=failed` 返回，`error` 中给出同样的 `code`。这样调用方总是可以通过 operation 记录审计失败请求。

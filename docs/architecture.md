# 架构与会话语义

## 分层

```
simulation_runtime/
├── core/                 与后端无关的会话状态机
│   ├── backend.py        BackendSession / SimulationBackend 协议
│   ├── types.py          观测与动作校验
│   ├── session.py        预算、阶段、模式、执行入口
│   ├── sandbox.py        受限 Python 程序执行
│   └── errors.py         稳定错误码与 HTTP 状态
├── backends/
│   ├── mujoco_so101/     SO-101 pick_cube（含内置 MJCF/STL）
│   └── fake.py           确定性测试后端
├── service/
│   ├── app.py            FastAPI 路由与错误映射
│   ├── auth.py           可选 Bearer → project_id + scopes；默认公共项目
│   ├── config.py         认证模式、令牌、限制、环境变量
│   ├── manager.py        会话生命周期、worker、操作队列、回收
│   ├── worker.py         每个会话一个进程
│   ├── store.py          SQLite 元数据/操作/观测/工件索引
│   └── artifacts.py      PNG 编码与项目隔离读取
├── sdk/client.py         标准库 HTTP 客户端
└── cli.py                命令行入口
```

服务进程只持有元数据与操作队列；**物理世界只存在于会话 worker 进程**。所有动作通过 `multiprocessing.Pipe` 以请求/响应方式串行发送，任何超时或进程退出都不会重发动作。

## 会话状态机

```
starting ──► active ──► closed
                │
                └────► interrupted
```

- `starting`：worker 已创建但首个观测尚未落库。
- `active`：可接受新操作。
- `closed`：客户端主动关闭或 TTL 回收；只读接口仍可访问历史操作与观测。
- `interrupted`：执行超时、worker 崩溃、重置失败等；不可恢复，不可重放。
- 服务重启时，数据库中所有 `starting/active` 会话会被标记为 `interrupted`，原因 `service_restart`；所有 `pending/running` 操作标记为失败且 `outcome_uncertain=true`。

## 预算

| 预算 | 作用域 | 语义 |
| --- | --- | --- |
| 原生动作次数 | 每次尝试 | SO-101 为 300；`playground` 重置后续满，`test` 不可重置 |
| exec 执行次数 | 整个会话 | 默认 100；只在会话创建时设定 |
| 等待时长 | 单次操作 | 服务端 `request_wait_sec`，超时返回 `202` |
| 程序执行时长 | 单次执行 | worker 内 `SIGALRM`；超时后会话中断 |
| 会话 TTL | 全局 | 空闲 / 最长生命周期后自动关闭 |

`native_steps_used` 是**累计**计数，重置不会清零；`native_steps_remaining` 表示当前尝试剩余的步数。因此评测可以同时看到总动作开销与本次尝试的剩余额度。

动作到达 300 步时：`truncated=true`、`terminated=true`（对外），会话进入终态。之后提交动作不会推进物理世界，返回 `applied=false, already_terminal=true`，不消耗原生预算。

## typed 与 exec

- `typed` 会话只接受 `POST /steps`；动作必须精确匹配关节 schema 或 IK schema（见 `docs/api.md`）。
- `exec` 会话只接受 `POST /executions`；程序内只能通过四个原语与仿真交互。
- 显式 `POST /reset` 在两种模式下都可用，但只允许 `playground`。

程序执行使用持久化全局命名空间：

- 第一次执行定义的变量在下一次执行中仍存在（同一 worker 进程、同一会话）；
- 每次执行都会重新注入 `get_instruction`、`get_observation`、`step`、`reset`；
- 程序抛出的普通异常会成为 `ok=false` 的结果，不中断会话；
- 程序抛出 `BaseException`、触发执行超时或 worker 崩溃会中断会话，且结果不可重放。

## 操作与幂等

每个写请求在同步校验通过后生成一条 operation 记录，字段包括：

- `kind`：`step` / `execute` / `reset` / `close`
- `idempotency_key`（可选）：同一会话内唯一
- `request_hash`：`{kind, payload}` 的规范化 SHA-256
- `status`：`pending` → `running` → `succeeded` / `failed`
- `outcome_uncertain`：动作可能已作用到物理世界，但服务未获得确认

语义：

1. 相同键 + 相同请求 → 返回原操作（不重复执行）。
2. 相同键 + 不同请求 → `409 idempotency_conflict`。
3. 同一会话已有 `pending/running` 操作 → `409 session_busy`。
4. 默认同步等待最多 `request_wait_sec`；超时返回 `202` 和 `Location`。
5. 执行超时、模拟器异常、worker 崩溃、服务重启 → 操作失败且 `outcome_uncertain=true`，会话中断。

## 观测与工件

- 每次动作/重置/程序执行结束后，worker 返回完整的原生观测（含相机数组）。
- 服务端将每路相机编码为 PNG，写入 `${data_dir}/artifacts/<project>/<session>/<artifact>.png`。
- 观测元数据写入 SQLite：指令、关节状态、动作、频率、相机尺寸和 `href`。
- 客户端只看到服务端生成的 ID 和相对 `href`；`GET /v1/artifacts/{id}` 按项目鉴权。
- 初始观测在会话创建时落库，因此客户端无需额外请求即可拿到第一帧。

## 并发与容量

- 每个会话一个 daemon worker 进程，一个后台驱动线程，队列长度为 1（通过 `session_busy` 保证）。
- 全局 `max_sessions` 与项目级 `max_sessions_per_project`；容量检查与插入在同一把锁内完成。
- 状态写入 SQLite（WAL、单连接、进程内锁）。这是单实例设计；多实例会共享同一个 `data_dir` 时不受支持。

## 失败模型

| 场景 | 结果 |
| --- | --- |
| 动作 schema 错误 | 操作失败 `invalid_action`，会话继续 |
| exec 预算耗尽 | 操作失败 `budget_exhausted`，会话继续 |
| test 阶段重置 | 操作失败 `forbidden`，会话继续 |
| 单次程序执行超时 | 操作失败 `execution_timeout`，会话中断 |
| worker 退出/管道断开 | 操作失败 `worker_crashed`，会话中断 |
| worker 初始化失败 | 会话 `failed`，创建请求返回 `500` |
| 服务重启 | 会话中断，未完成操作标记 uncertain |
| TTL 到期 | 会话 `closed`，原因 `session_ttl` |

第一阶段不做动作重放、不做状态快照迁移、不做跨实例恢复。

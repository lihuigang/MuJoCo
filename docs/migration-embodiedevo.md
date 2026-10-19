# 从 EmbodiedEvo 解耦的接入方式

## 变化

以前 EmbodiedEvo 的 host session（`orchestrator/session.py`）直接持有 MuJoCo 世界、代码沙箱和反馈目录，agent 容器通过 Unix/TCP socket 发请求。现在这些职责属于 `simulation-runtime`：

| 以前（EmbodiedEvo 内） | 现在（simulation-runtime） |
| --- | --- |
| `Session.handle({"op": "exec", "files": [...]})` | `POST /v1/sessions/{id}/executions` |
| `Session.handle({"op": "observe"})` | `GET /v1/sessions/{id}/observation` |
| `Session.handle({"op": "instruction"})` | 观测中的 `instruction` 字段 |
| `Session.handle({"op": "reset"})` | `POST /v1/sessions/{id}/reset`（仅 playground） |
| `Session.handle({"op": "status"})` | `GET /v1/sessions/{id}` |
| `session-result.json` / `ledger.jsonl` | 会话状态 + operation 历史（SQLite） |
| `workspace/observations/NNNNNN` 反馈目录 | 观测元数据 + `/v1/artifacts/{id}` PNG |
| 本地 MuJoCo、模型资产、反馈视频 | 服务端 MuJoCo；不再需要资产副本 |
| 本地 token/socket 鉴权 | 默认免令牌（内网）；可选 Bearer 令牌 + HTTPS |

会话语义保持一致：

- `playground` 允许重置，重置后原生动作次数续满；
- `test` 禁止重置；
- 原生动作上限 300（SO-101 `pick_cube`）；
- `exec` 预算按会话累计，程序全局变量在同一会话内持久化；
- 超时/崩溃不重放，结果标记 `outcome_uncertain`。

## 最小客户端

```python
import os
from simulation_runtime.sdk import Client

client = Client(
    os.environ["SIM_RUNTIME_URL"],            # 例如 https://sim.example.internal
    os.environ.get("SIM_RUNTIME_TOKEN"),      # token 模式才需要
)

with client.session(
    backend="mujoco_so101",
    task="pick_cube",
    phase="test",                          # 评测使用 test token
    mode="exec",
    seed=seed,
    exec_budget=100,
) as session:
    result = session.execute(solution_source)
    # result: ok, error, stdout, stderr, observation, budgets, sequence
```

`Session` 在退出时自动关闭会话；如果会话已中断/关闭，关闭错误会被吞掉，不会覆盖原始异常。

## 原有多文件快照

旧接口接受最多 256 个源文件（用于保留 agent 的编辑历史）。HTTP API 接受单个 `code` 字符串，因此适配层可以：

```python
code = "\n\n".join(
    f"# --- {item['path']} ---\n{item['code']}"
    for item in files
)
client.execute(session_id, code)
```

或者只提交入口文件。若需要逐文件审计，把 `files` 列表作为附件/日志保存在 EmbodiedEvo 侧；`simulation-runtime` 的操作记录只保存最终代码的哈希与结果。

## 结果与观测

每次执行返回：

- `ok`：程序是否正常结束（程序内异常为 `false`，但 operation 仍为 `succeeded`）；
- `error` / `stdout` / `stderr`；
- `success` / `terminated` / `truncated`；
- `native_steps_used` / `native_steps_remaining`；
- `executions_used` / `executions_remaining`；
- `observation`：指令、关节状态、动作、频率、相机工件引用。

相机帧不再以视频形式返回。需要视觉记录时，按 `observation["cameras"][name]["href"]` 下载 PNG；如需视频，调用方可以按步采样后自行编码（后续阶段可在服务端增加视频工件）。

## 评测隔离

默认免令牌模式下所有调用方共享公共项目。多个团队/用途共享同一服务时，建议启用 token 模式，并为 EmbodiedEvo 的不同用途创建独立项目和令牌：

```json
{
  "tokens": [
    {
      "token": "...",
      "project_id": "embodiedevo-playground",
      "scopes": ["playground", "typed", "exec"],
      "max_sessions": 2
    },
    {
      "token": "...",
      "project_id": "embodiedevo-test",
      "scopes": ["test", "typed"],
      "max_sessions": 2
    }
  ]
}
```

- agent 自迭代使用 `playground` + `exec`（或 `typed`）；
- 正式评测使用 `test` + `typed`（或 `test` + `exec`），确保不能重置场景；
- 不同项目之间不可见彼此的会话和工件。

## EmbodiedEvo 侧需要删除/保留什么

- 删除：MuJoCo 模型资产拷贝、`simulators/mujoco_so101.py` 的运行时依赖、host session 的 socket 服务、本地反馈视频生成。
- 保留：agent 容器逻辑、任务指令/评测编排、结果归档；只需把与仿真交互的部分替换为 `simulation_runtime.sdk.Client`。
- 若要保留原有 Gymnasium 适配层，可在 EmbodiedEvo 内实现一个 `gymnasium.Env` 包装器：`reset()` → 创建/重置服务会话，`step(code)` → `/executions`，观测从返回的元数据构造。该包装器应放在 EmbodiedEvo 仓库，而不是本服务中。

## 兼容性注意

- 服务协议版本是 `simulation-runtime/v1`；`/v1/capabilities` 返回 `protocol` 和 `service_version`，客户端应在启动时校验。
- `POST /close` 之后会话不可复用；评测循环应每个 episode 创建新会话。
- 服务重启会中断所有活动会话；客户端需要检测 `session_interrupted` 并重新创建。
- 服务自带只读查看页 `/viewer`（轮询 PNG 快照，双相机），但没有视频工件和失败自动回放；`features.live_viewer=true`。

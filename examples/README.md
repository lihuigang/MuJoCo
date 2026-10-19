# 示例

两个示例都需要一个正在运行的服务；服务默认免令牌，直接运行即可：

```bash
export SIM_RUNTIME_URL=http://127.0.0.1:8080
export SIM_RUNTIME_SEED=0
# 仅 token 模式需要：
# export SIM_RUNTIME_TOKEN=<具备 playground + typed 或 playground + exec scope 的令牌>
```

| 文件 | 模式 | 说明 |
| --- | --- | --- |
| `typed_pick_cube.py` | typed | 固定 IK 轨迹执行抓取放置；演示观测、PNG 下载与预算字段 |
| `exec_pick_cube.py` | exec | 提交一个受限 Python 程序，程序内循环调用 `step` |

说明：

- 轨迹是手写示例，不保证对所有 seed 成功；用于验证接入链路。
- 图片写入 `examples/output/`（已被 `.gitignore` 忽略）。
- `exec` 沙箱只适用于可信团队，详见 `docs/security.md`。

# 安全与信任边界

## 信任模型

第一阶段面向**可信的内部项目**：

- 调用方是内部服务或受控脚本，而不是匿名互联网用户；
- `exec` 模式只发给可信团队；
- 服务部署在受控主机或内网，外层有 TLS 与访问控制。

在此模型下，服务提供的是**项目级逻辑隔离 + 进程级故障隔离**，不是针对恶意多租户的强隔离。不要把 `exec` 能力开放给不可信用户。

## 免令牌模式（默认）

未配置令牌时服务以 `SIM_RUNTIME_AUTH_MODE=none` 运行：

- 所有请求映射到公共项目 `public`，拥有全部 scopes；
- 任何能访问服务端口的人都可以创建会话、提交动作、执行 `exec` 程序、下载工件；
- 没有身份区分，所有会话对所有人可见；
- 项目并发上限退化为全局并发上限。

因此免令牌模式只适用于单机、可信内网或受控访问（VPN、防火墙 allowlist、带认证的反向代理）。对外或多人共享时必须启用 token 模式。服务启动时会在日志中打印明确的警告。

## 认证与授权（token 模式）

设置 `SIM_RUNTIME_AUTH_MODE=token` 后：

- 令牌通过 `Authorization: Bearer <token>` 传递；服务端使用常量时间比较。
- 令牌只映射到服务端配置的 `project_id` 和 scopes，客户端无法指定或伪造项目身份。
- 支持 scopes：`playground`、`test`、`typed`、`exec`。
  - 创建会话需要同时具备 `phase` 与 `mode` 对应 scope；
  - `POST /steps` 需要 `typed`；
  - `POST /executions` 需要 `exec`；
  - 项目内所有读接口按令牌的 `project_id` 过滤。
- 令牌不会写入 SQLite、操作记录、错误响应或应用日志。反向代理若开启请求头日志，需自行脱敏。

## 项目隔离（token 模式）

- 会话、操作、观测、工件表都带 `project_id`，查询时强制附加该条件；跨项目 ID 返回 `404`。
- 工件路径由服务端生成：`<data_dir>/artifacts/<project_id>/<session_id>/<artifact_id>.png`，项目/会话 ID 必须匹配安全字符集。
- 客户端永远看不到服务器绝对路径，只看到 `artifact_id` 和相对 `href`。
- 同一项目的不同令牌共享该项目的会话列表和工件。

## exec 沙箱（重要）

`exec` 模式使用 AST 校验 + 受限内置函数 + 受限 NumPy 命名空间，目标是**防止误操作**（误读文件、误导入、误改全局状态），不是抵抗蓄意逃逸：

- 禁止以下划线开头的名称/属性、`open`、`eval`、`exec`、`compile`、`getattr`、`setattr`、`globals`、`locals`、`vars`、`dir`、`input`、`breakpoint`、`env`、`APIS`、`INPUTS` 等；
- 只允许 `import numpy` / `import math`；
- 只暴露 `get_instruction`、`get_observation`、`step`、`reset`；
- 代码大小上限 256 KiB，stdout/stderr 各保留 64 KiB 尾部；
- 单次执行默认 60 秒（`SIGALRM`，`BaseException` 不可被程序吞掉）；超时后会话立即中断。

仍存在的风险：

- Python 级沙箱历史上多次被绕过；NumPy 等 C 扩展也可能提供间接能力。
- worker 与服务共享同一 OS 用户、文件系统和网络命名空间，没有每会话容器、seccomp、cgroup 或内存/CPU 限额。
- 程序可以消耗 CPU 直至执行超时；大量会话可能造成资源压力。

因此：

1. 仅向可信团队发放 `exec` scope；其他团队只发 `playground,typed` 或 `test,typed`。
2. 服务进程使用专用低权限用户运行。
3. 通过 `max_sessions`、项目级上限和外层容器限制资源。
4. 第二阶段计划：每会话容器/沙箱、资源限额、审计导出。

## 网络与部署

- 默认只监听 `127.0.0.1:8080`；对外必须由反向代理终止 TLS（示例见 `deploy/nginx.conf.example`）。
- **默认免令牌**：不要把服务端口直接暴露到不可信网络。远程访问时保持回环绑定，使用 SSH 隧道或带认证/限制来源的反向代理；需要区分调用方时启用 token 模式。
- 建议只在内网/专线可达，必要时加 mTLS 或 IP allowlist。
- 服务自身不做 TLS、不做速率限制；请在代理层配置请求体上限（应用内也有 2 MiB 拒绝）和速率限制。
- 健康探针 `/healthz`、`/readyz` 无鉴权，只暴露最小状态。
- `/viewer` 只返回静态 HTML。免令牌模式下不显示令牌输入框；token 模式下令牌保存在浏览器标签页的 `sessionStorage`，不进入 URL、服务端或日志。页面已设置 CSP、`X-Frame-Options: DENY` 与 `Referrer-Policy: no-referrer`，且不加载任何第三方资源。

## 进程与数据

- 每个会话一个独立 worker 进程；worker 崩溃只会中断该会话，不影响其他会话或服务进程。
- 动作不重放：worker 超时/崩溃后，操作标记 `outcome_uncertain=true` 并中断会话，避免同一物理动作被执行两次。
- SQLite 保存会话状态、操作历史（含请求 JSON）、观测元数据；工件目录保存 PNG。请按组织的留存策略清理旧会话和工件（当前没有自动删除）。
- `data_dir` 应位于受控磁盘；备份时同时备份 SQLite 与 `artifacts/`。

## 依赖与供应链

- 运行时依赖：`numpy`、可选 `mujoco`、服务端 `fastapi`/`uvicorn`。
- 仓库内置 SO-101 MJCF/STL（Apache-2.0），来源与版本见 `THIRD_PARTY_NOTICES.md` 与 `src/simulation_runtime/backends/mujoco_so101/MODEL_PROVENANCE.md`。
- 建议在生产镜像中固定依赖版本（锁文件）并定期扫描镜像漏洞。

## 已知缺口（第一阶段）

- 免令牌模式没有身份、权限与访问审计；只能靠网络边界保护。
- 令牌轮换需要重启服务；没有令牌管理 API。
- 没有每会话容器、cgroup、内存/CPU 限额。
- 没有速率限制、审计日志导出、细粒度操作权限。
- 单实例 SQLite，不支持多副本/水平扩展。
- viewer 为轮询 PNG 快照，没有视频流、视频工件与失败自动回放。

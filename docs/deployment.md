# 部署指南（第一阶段：单机）

目标是单台 Linux 主机 + Docker Compose，外层反向代理终止 TLS。第一阶段不使用 Kubernetes。

## 1. 准备令牌

```bash
cd deploy
cp tokens.example.json tokens.json
chmod 600 tokens.json
# 为每个项目生成独立随机令牌：
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

令牌要求：

- 长度至少 16 字符，建议 32 字节随机值；
- 每个项目一个，不跨项目复用；
- `project_id` 使用稳定、无空格的标识；
- scopes 按最小权限发放。`exec` 只给可信团队。

修改令牌后需要重启服务（当前没有热更新）。

## 2. Docker Compose

```bash
cd deploy
docker compose build
docker compose up -d
docker compose logs -f simulation-runtime
```

- 服务只绑定 `127.0.0.1:8080`，由同机反向代理对外。
- 数据保存在命名卷 `deploy_runtime-data`；令牌以只读方式挂载。
- 健康检查请求 `/readyz`；首次启动需要加载 MuJoCo 后端。
- 容器内设置 `MUJOCO_GL=egl`。若宿主机/驱动不支持 EGL，将环境变量改为 `osmesa`（镜像已安装 `libosmesa6`），或挂载可用 GPU 驱动。

手动验证：

```bash
set -a; . ./.env 2>/dev/null; set +a
TOKEN=$(python -c "import json; print(json.load(open('tokens.json'))['tokens'][0]['token'])")
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/v1/capabilities | python -m json.tool
```

## 3. TLS 反向代理

`deploy/nginx.conf.example` 提供了最小示例：

```bash
sudo cp nginx.conf.example /etc/nginx/nginx.conf   # 替换证书/域名
sudo nginx -t && sudo systemctl reload nginx
```

要点：

- `client_max_body_size` 至少 2m；
- `proxy_read_timeout` 必须大于 `request_wait_sec + execution_timeout_sec`；
- 不要把 `Authorization` 头写入访问日志；
- 如需代理头，设置 `SIM_RUNTIME_FORWARDED_ALLOW_IPS=127.0.0.1`（Compose 已设置）。

Caddy 等价配置：

```
sim.example.internal {
    reverse_proxy 127.0.0.1:8080
}
```

## 4. 非容器部署（systemd）

```bash
sudo useradd --system --home /opt/simulation-runtime simulation
sudo mkdir -p /opt/simulation-runtime /var/lib/simulation-runtime /etc/simulation-runtime
sudo chown simulation:simulation /var/lib/simulation-runtime
# 将仓库放到 /opt/simulation-runtime，创建 venv 并安装
sudo -u simulation /opt/simulation-runtime/.venv/bin/pip install '/opt/simulation-runtime[service,mujoco]'

sudo install -m 600 deploy/tokens.json /etc/simulation-runtime/tokens.json
sudo tee /etc/simulation-runtime/env >/dev/null <<'EOF'
SIM_RUNTIME_TOKENS_FILE=/etc/simulation-runtime/tokens.json
SIM_RUNTIME_DATA_DIR=/var/lib/simulation-runtime
SIM_RUNTIME_HOST=127.0.0.1
SIM_RUNTIME_PORT=8080
SIM_RUNTIME_BACKENDS=mujoco_so101
MUJOCO_GL=egl
EOF
sudo install -m 644 deploy/simulation-runtime.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now simulation-runtime
```

## 5. 容量与资源

- 每个并发 MuJoCo 会话是一个独立进程，包含模型、渲染器和相机。按 **300–500 MB/会话** 预留内存，再留 1–2 GB 给服务与系统；以实测为准。
- 默认全局 4 个会话、每项目 2 个。CPU 紧张时先降低上限。
- 渲染需要 OpenGL 上下文；无 GPU 时使用 Mesa 软件渲染，CPU 开销明显。
- `exec` 程序可能占满 CPU 直到超时；生产环境应限制 `exec` scope 和会话数。

## 6. 日志与监控

- 应用日志输出到 stdout/stderr，由 Docker/systemd 收集。
- 健康探针：`/healthz`（存活）、`/readyz`（后端与存储就绪）。
- 重要事件写入 SQLite：操作历史、失败原因、`outcome_uncertain`。需要长期审计时定期导出/备份。
- 服务重启会把未完成会话标记为 `interrupted`；这是预期行为，不做恢复。

## 7. 备份与清理

需要备份：

- SQLite：`<data_dir>/runtime.sqlite3`（以及运行时的 `-wal`/`-shm`）；
- 工件：`<data_dir>/artifacts/`。

建议：

- 停服后复制，或使用 SQLite 在线备份 API；
- 会话/工件没有自动删除策略；按项目留存要求定期清理旧目录，并同步清理 `sessions`、`operations`、`observations`、`artifacts` 表（当前版本需人工操作）。

## 8. 升级

1. 停止服务（`docker compose down` 或 `systemctl stop`）。
2. 更新代码/镜像，保持 `data_dir` 卷不变。
3. 启动服务；数据库 schema 使用 `CREATE TABLE IF NOT EXISTS`，当前版本没有迁移框架。
4. 升级会使所有活动会话中断，客户端需重新创建会话。

滚动升级/多副本不在第一阶段范围内。

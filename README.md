# Fault Engine

基于 **mitmproxy** 的开发测试反向代理。客户端更改 base URL，即可复现错误序列、响应超时、断连和真实 TCP Reset，检查 retry / error handling。

- 多后端、多监听端口，未匹配请求正常转发。
- 支持 GET、POST、PUT、PATCH、DELETE、HEAD、OPTIONS、TRACE 和合法自定义 HTTP 方法。
- 支持 `429 → 429 → 真实后端`、第 n 次开始失败、循环场景和固定 mock 响应。
- 按测试请求头隔离计数，管理 API 查询状态和重置。
- Linux Docker 中验证真实 `ECONNRESET`（errno 104），与 HTTP 504、普通断连分别测试。
- 支持后端已执行后再返回错误或断连，验证写入重试与幂等性；附带 33 条可运行场景。
- 支持固定 seed 的故障采样与延迟抖动，reset 后可重放，不同 scope 不共享随机数状态。
- 提供条件匹配、维护窗口豁免、循环恢复、缓存 304、DELETE 204 和二进制下载示例。
- 使用非回溯正则和只读执行计划；连接与在途请求可设上限，管理状态支持分页。
- `init` 生成配置，`explain` 离线解释匹配；`requests` 查看脱敏记录，`verify` 为 CI 检查请求次数、状态序列和进入间隔。

## 快速体验

前提：Docker/OrbStack 正在运行。**所有项目命令均通过 runner 在容器中执行**，无需在宿主机安装 Python 或 uv。

```bash
bash .agent/run.sh uv sync --locked
bash .agent/run.sh uv run fault-engine validate examples/scenarios.yaml
bash .agent/run.sh --publish uv run python examples/demo.py
```

最后一条启动两个演示后端与代理。宿主机入口：orders `http://127.0.0.1:18080`、inventory `http://127.0.0.1:18081`、管理 `http://127.0.0.1:19090`。仅绑定宿主机 loopback；停止使用 Ctrl-C。演示管理 token 为 `local-demo-token`。

另开终端，在容器中调用宿主机映射的演示端口（Docker Desktop / OrbStack）：

```bash
bash .agent/run.sh sh -c 'for i in 1 2 3; do curl -sS -o /dev/null -w "%{http_code}\n" -H "X-Test-Run-ID: demo" http://host.docker.internal:18080/retry; done'
```

应依次看到 `429`、`429`、`200`。同一 ID 继续调用会正常转发；换 ID 或 reset 可从头开始。

下一步可按[场景手册](docs/scenarios.md)选择用例，按测试 ID 查询状态和重置。编辑自定义 YAML 前可运行 `bash .agent/run.sh uv run fault-engine schema` 查看配置 JSON Schema；语法和重复键错误会指出行、列。runner 用法见 `bash .agent/run.sh --help`。

把上面的重试结果作为自动验收（演示进程尚在运行，且 scope `demo` 恰好调用过三次）：

```bash
bash .agent/run.sh sh -lc 'export FAULT_ADMIN_TOKEN=local-demo-token; uv run fault-engine verify --admin-url http://host.docker.internal:19090 --service orders --scope demo --count 3 --statuses 429 429 200'
```

匹配退出 0，断言失败或记录不完整退出 1，参数/网络错误退出 2。到达间隔包含前一次响应耗时，不能作为客户端退避时间的证明。

接入自己的后端时可先生成并解释配置（以下假定后端位于宿主机 9000）：

```bash
bash .agent/run.sh uv run fault-engine init retry.yaml --upstream http://host.docker.internal:9000 --preset retry
bash .agent/run.sh uv run fault-engine explain retry.yaml --service backend --path /retry --header 'X-Test-Run-ID: run-1' --ordinal 1
```

retry 模板是两次 503 后透传；还有 timeout、reset、jitter 模板。`init` 拒绝覆盖已有文件。完整的[生成→解释→运行→验收→重跑步骤](docs/usage.md#生成配置并完成客户端验收)包含无需外部后端的可复制示例。

## 文档与验证

常用故障模式及后续改善取舍见 [模式调研](docs/fault-patterns.md)。

- [使用文档](docs/usage.md)：配置完整说明、故障区别、真实后端接入、管理 API。
- [场景手册](docs/scenarios.md)：完整场景目录、定向故障、并发隔离与重复运行步骤。
- [开发文档](docs/development.md)：架构、生命周期、扩展、Docker/uv/ruff/ty 流程。
- [验收记录](docs/verification.md)：需求与测试对应、验证命令和适用范围。
- [场景示例](examples/scenarios.yaml)：可以直接使用和修改。

```bash
bash .agent/run.sh bash .agent/check.sh
```

已实现能力以验收记录为准。HTTP/1.1 为测试基线；支持 HTTP/HTTPS 后端，客户端演示入口使用 HTTP。HTTP/2/3、WebSocket、流式中途故障、DNS/连接建立超时和网络丢包不属于当前验证范围。本工具用于开发测试，不是生产网关。

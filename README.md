# Fault Engine

基于 **mitmproxy** 的开发测试反向代理。把客户端各后端服务的 base URL 改为对应代理端口，用 YAML 制造故障，再检查客户端的重试、异常分类和最终结果。未命中规则的请求正常转发，不需要修改后端业务代码。

| 你要测试什么 | 如何表达 / 演示入口 |
| --- | --- |
| 多个真实后端 | 每个 service 配一个 upstream 和监听端口；[双后端接入](docs/usage.md#接入真实后端) |
| 两次 4xx 后第三次固定成功 | respond 400 × 2 → respond 200；演示 `/mock` |
| 从第 n 次开始失败两次 | start_at + repeat: 2；演示 `/nth` 第 5–6 次 503，可改为任意合法 4xx/5xx |
| 客户端读超时 | timeout 或 delay_before/delay_after；演示 `/timeout`、`/slow-after` |
| HTTP 错误与错误处理 | 持续 401/503、HTTP 504、无效 JSON 等；完整目录有 33 条场景 |
| 重试次数与恢复顺序 | 每次逻辑调用使用独立测试 ID；requests 查看记录，verify 返回 CI 退出码 |

还支持真实 TCP Reset、后端执行后的故障、概率采样和延迟抖动。**固定成功用 respond 200；passthrough 的结果由真实后端决定。** 客户端最终异常、界面提示、业务幂等性仍需在被测应用中断言；代理记录不能替代这些结果。

## 快速体验

前提：Docker/OrbStack 正在运行。**所有项目命令均通过 runner 在容器中执行**，无需在宿主机安装 Python 或 uv。

```bash
bash .agent/run.sh uv sync --locked
bash .agent/run.sh uv run fault-engine validate examples/scenarios.yaml
bash .agent/run.sh --publish uv run python examples/demo.py
```

最后一条启动两个演示后端与代理。宿主机入口：orders `http://127.0.0.1:18080`、inventory `http://127.0.0.1:18081`、管理 `http://127.0.0.1:19090`。仅绑定宿主机 loopback；停止使用 Ctrl-C。演示管理 token 为 `local-demo-token`。

另开终端，使用示例 GET 客户端发起一次逻辑调用（Docker Desktop / OrbStack）：

```bash
bash .agent/run.sh uv run python examples/retry_client.py http://host.docker.internal:18080/retry --run-id readme-client
```

JSON attempts 应依次为 `429`、`429`、`200`，outcome 为 success，退出 0。这个示例根据响应决定是否再请求：仅重试 429/503/读超时，最多三次；成功或其他状态立即停止。它演示固定等待策略，不解析 Retry-After。测试真实应用时替换为应用自身客户端。同一 ID 继续调用会正常转发；重跑前三次故障须换 ID 或 reset。

下一步可按[场景手册](docs/scenarios.md)选择用例，按测试 ID 查询状态和重置。编辑自定义 YAML 前可运行 `bash .agent/run.sh uv run fault-engine schema` 查看配置 JSON Schema；语法和重复键错误会指出行、列。runner 用法见 `bash .agent/run.sh --help`。

检查代理是否确实收到上述三次请求（演示进程尚在运行）：

```bash
bash .agent/run.sh sh -lc 'export FAULT_ADMIN_TOKEN=local-demo-token; uv run fault-engine verify --admin-url http://host.docker.internal:19090 --service orders --scope readme-client --count 3 --statuses 429 429 200'
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

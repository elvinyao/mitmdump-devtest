# Fault Engine

基于 **mitmproxy** 的开发测试反向代理。客户端更改 base URL，即可复现错误序列、响应超时、断连和真实 TCP Reset，检查 retry / error handling。

- 多后端、多监听端口，未匹配请求正常转发。
- 支持 GET、POST、PUT、PATCH、DELETE、HEAD、OPTIONS、TRACE 和合法自定义 HTTP 方法。
- 支持 `429 → 429 → 真实后端`、第 n 次开始失败、循环场景和固定 mock 响应。
- 按测试请求头隔离计数，管理 API 查询状态和重置。
- Linux Docker 中验证真实 `ECONNRESET`（errno 104），与 HTTP 504、普通断连分别测试。
- 支持后端已执行后再返回错误或断连，验证写入重试与幂等性；附带 30 条可运行场景。
- 提供条件匹配、维护窗口豁免、循环恢复、缓存 304、DELETE 204 和二进制下载示例。
- 使用非回溯正则和只读执行计划；连接与在途请求可设上限，管理状态支持分页。

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

## 文档与验证

- [使用文档](docs/usage.md)：配置完整说明、故障区别、真实后端接入、管理 API。
- [场景手册](docs/scenarios.md)：完整场景目录、定向故障、并发隔离与重复运行步骤。
- [开发文档](docs/development.md)：架构、生命周期、扩展、Docker/uv/ruff/ty 流程。
- [验收记录](docs/verification.md)：需求与测试对应、验证命令和适用范围。
- [场景示例](examples/scenarios.yaml)：可以直接使用和修改。

```bash
bash .agent/run.sh bash .agent/check.sh
```

已实现能力以验收记录为准。HTTP/1.1 为测试基线；支持 HTTP/HTTPS 后端，客户端演示入口使用 HTTP。HTTP/2/3、WebSocket、流式中途故障、DNS/连接建立超时和网络丢包不属于当前验证范围。本工具用于开发测试，不是生产网关。

# 验收记录

验证日期：2026-09-22。环境：Docker/OrbStack 中的 Linux、Python 3.12.14。依赖由 uv.lock 锁定，其中 mitmproxy 12.2.3、ruff 0.16.8、ty 0.0.83、pytest 9.1.1。

## 结果

- **119 tests passed，0 failed，0 skipped**。
- coverage 同时统计行与分支，并包含 CLI/demo 子进程：**94%**（669 个 statement、198 个 branch）。覆盖率是测试范围指标，不是对任意网络环境的正确性保证。
- `uv lock --check`、`uv sync --locked`、`ruff format --check .`、`ruff check .`、`ty check` 全部通过。
- `uv build` 生成 wheel 和 sdist。隔离安装 wheel 后运行 `fault-engine validate examples/scenarios.yaml`，返回 `valid: 2 services, 9 rules`。
- 程序和 Docker runner 都实际运行过，测试没有用 mock 替代 mitmproxy 或 TCP reset。
- 测试保留了 **42 条第三方弃用警告**：mitmproxy 使用 pyparsing 的旧 API，以及 ldap3 对 pyasn1 旧导出的引用。没有将这些警告隐藏或描述为零警告。

复现全部质量门槛：

```bash
bash .agent/run.sh bash .agent/check.sh
```

## 需求与证据

| 需求 | 验证文件与行为 |
| --- | --- |
| mitmproxy 代理底座、多后端 | runtime.py 组装真实 Master；test_integration.py 实际转发；test_network_edges.py 验证两个不同 upstream 和独立计数 |
| uv / ruff / ty | pyproject.toml、uv.lock、.agent/check.sh；实际运行以上质量命令 |
| Docker 执行约束 | .agent/run.sh 使用 python:3.12-bookworm、docker run --rm、/workspace 挂载与工作目录；执行命令均经 runner |
| GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS/TRACE | 参数化真实 HTTP 测试，验证 method、二进制 body、重复 query、业务头和 upstream Host |
| 合法自定义方法 | 原始 HTTP upstream 接收 CUSTOM 与请求体；使用原始后端避免 aiohttp parser 的固定方法集合限制 |
| 非法方法和 CONNECT | test_boundaries.py 验证 4xx 且不访问后端、不分配非法请求序号 |
| 模拟 4xx/5xx/成功响应 | 200/204/205/304/400/401/404/408/429/500/502/503/504 参数化；文本、JSON、二进制编码测试 |
| 两次错误后恢复真实后端 | test_real_retry_two_errors_then_upstream_once：429/429/200，真实后端恰好一次调用 |
| 两次 4xx 后固定成功 | test_two_4xx_then_fixed_mock_success_without_upstream：400/400/200/200，JSON 确认成功且后端零次调用 |
| 第 n 次触发、repeat、结束策略 | test_engine.py 检查起始边界、repeat、passthrough、repeat_last、cycle |
| 匹配与 first-match | test_engine.py 检查 method/path/full regex/header/query AND 匹配；test_boundaries.py 验证匹配原始 Host |
| scope 隔离、缺失头、并发序号 | test_engine.py 按 scope 独立递增、100 个协程分配唯一序号；integration 验证缺失 scope 的明确 400 |
| 容量和 TTL | 可控 monotonic clock 验证闲置过期、日志、活跃条目不被静默淘汰；真实 HTTP 容量满返回 scenario 错误 |
| 前置延迟、后置延迟、读超时 | test_integration.py 验证客户端 ReadTimeout、后端调用次数分别为 0/1/0、延迟最终成功、其他请求不被阻塞 |
| 连接重置 104 | test_transport.py 及 test_real_reset_and_unrelated_connection_survives 使用真实 Linux socket，断言 ECONNRESET=104；其他连接仍正常 |
| 普通断连与有界 timeout | test_transport_fault_is_not_http_response 验证 transport error，未伪造 HTTP 500；timeout 到期终止 |
| HTTP 语义 | HEAD mock 和错误响应在同一 keepalive 连接上没有 body 泄漏；204/205/304 不带 body；二进制 mock 字节完全一致 |
| HTTP/HTTPS upstream | 临时私有 CA + TLS 后端，可信证书成功、不可信证书 502；未关闭校验；连接拒绝后端返回 502 |
| 管理接口 | /health、受鉴权保护的 /rules /state /reset，精确过滤、错误 JSON/未知字段、4 KiB 限制 |
| reset 与在途请求 | test_admin_reset_does_not_change_inflight_decision：重置后的后续请求从第一步开始，在途请求仍执行原动作 |
| 客户端取消与关闭 | 取消仍消耗序号，不破坏后续请求；inflight timeout 可及时关闭；背压时关闭不会等到无限排空；端口可重新绑定 |
| 启动失败回滚与 IPv6 | 管理端口占用时释放已启动监听器；IPv6 wildcard 返回 [::1] 地址且实际代理/health 请求成功 |
| 事件和日志 | test_events_are_structured_and_redact_request_data：阶段、规则、动作和序号可读取，不记录请求体/Authorization/Cookie，scope 截断 |
| CLI | validate/help、具体错误字段路径、秘密输入不回显、缺失 token 失败、真实 serve ready/mock/SIGTERM 子进程 |
| 中文文档及示例 | README.md、usage.md、development.md、scenarios.yaml、demo.py；test_demo.py 用真实 curl 验证重试/状态/reset/第二个服务/退出 |

测试分布：boundaries 11、CLI 6、config 36、demo 1、engine 10、integration 36、network_edges 10、transport 9。

## 实际端口映射体验

另外执行了 README 的 `--publish` 演示及跨容器 curl 命令，而不只测试容器内端口：

| 操作 | 当前 OrbStack 环境的实际结果 |
| --- | --- |
| 宿主机映射 orders 18080，固定 scope 连续请求 /retry | 429、429、200 |
| 管理 19090 查询 /state 并按 service/rule/scope reset | count=3，reset=1 |
| /timeout，curl --max-time 1 | curl 28，无响应超时 |
| POST /slow-after，curl --max-time 1 | 后端响应已到达的日志存在；curl 28 |
| /reset，经 host.docker.internal:18080 | curl 52（Empty reply），没有保留容器内直连的 104 表现 |
| Ctrl-C 停止演示 | 正常退出，临时演示服务已关闭 |

精确 RST 的验收证据是**容器内直连的两层真实 socket 测试**。不能把该结论扩展到任何 NAT/TCP 代理之后；当前 OrbStack 映射的行为差异已写入使用文档。

## 独立审查与修复

采用 superpowers 的设计、计划、TDD、根因定位与完成前验证流程，子代理执行 transport 实现和独立审查。先做需求审查，再做质量审查，反馈修复后复核。

已修复并回归验证：背压关闭卡住、SO_LINGER 失败丢失映射、HEAD mock 破坏连接分帧、非法 method 未拒绝、原始 Host 无法匹配、跨字段配置错误缺少字段路径、IPv6 wildcard URL 指向错误地址。各审查项已关闭。

## 未覆盖的协议与环境

当前完整验证基线为 Linux Docker 内 HTTP/1.1、HTTP/HTTPS upstream、单进程状态。HTTP/2/3、WebSocket、SSE/流式中途截断、客户端 HTTPS 入口、DNS 失败、建连超时、丢包、限速、多副本共享状态不在此次实现承诺中，也没有以普通 HTTP 测试代替这些能力的证据。外部客户端错误包装和额外转发层需要在实际客户端环境中另行验证。

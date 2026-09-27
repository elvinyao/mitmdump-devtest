# 验收记录

最近完整验证日期：2026-09-26。环境：Docker/OrbStack 中的 Linux、Python 3.12.14。依赖由 uv.lock 锁定，其中 mitmproxy 12.2.3、ruff 0.16.8、ty 0.0.83、pytest 9.1.1。

## 结果

- **304 tests passed，0 failed，0 skipped**，在上一轮 230 项基础上增加 74 项架构与边界回归。
- coverage 同时统计行与分支，并包含 CLI/demo 子进程：**96%**（1196 个 statement、344 个 branch）。覆盖率是测试范围指标，不是对任意网络环境的正确性保证。
- `uv lock --check`、`uv sync --locked`、`ruff format --check .`、`ruff check .`、`ty check` 全部通过。
- `uv build` 成功生成 wheel 和 sdist。独立虚拟环境离线安装 wheel，确认从安装路径导入，schema、HTTP mock 及请求/响应 trailer 拒绝检查通过。生产锁文件作为版本约束，依赖由 wheel 元数据决定。配置验证返回 `valid: 2 services, 30 rules`。
- 完整检查通过 `sh -lc` 执行。上一轮已验证登录 shell 的 `uv` 与 `uvx` 均为 0.12.17，本轮沿用同一工具链。
- 程序和 Docker runner 都实际运行过，测试没有用 mock 替代 mitmproxy 或 TCP reset。
- 测试保留了 **42 条第三方弃用警告**：mitmproxy 使用 pyparsing 的旧 API，以及 ldap3 对 pyasn1 旧导出的引用。没有将这些警告隐藏或描述为零警告。

复现全部质量门槛：

```bash
bash .agent/run.sh bash .agent/check.sh
```

本轮实际运行完整检查，并在末次审查加强 Engine 正则回归与 wheel 依赖检查后分别复验：

```bash
bash .agent/run.sh sh -lc 'uv run --no-sync ruff format . && bash .agent/check.sh'
bash .agent/run.sh uv run --no-sync pytest tests/test_regex_safety.py -q
bash .agent/run.sh bash .agent/check-wheel.sh
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
| 后端执行后注入故障 | test_extended_scenarios.py 验证 respond_after、reset_after、disconnect_after 的真实后端调用、原始 socket RST、重试再次写入；上游连接失败仍为真实 502 |
| 延迟 mock 与 HEAD 故障 | respond/respond_after 延迟和客户端超时不阻塞其他请求；HEAD 替换响应及 reset 失败的 503 不污染 keepalive 下一响应 |
| HTTP 字节与解析边界 | test_http_review.py 验证混合大小写方法、已压缩二进制不二次压缩、Latin-1 头、绝对 URL 固定路由、重复 scope 拒绝、chunked 和 Expect |
| HTTP trailers | 已声明/未声明的请求 trailer 均及时关连接且不访问后端，响应 trailer 返回 502；无 parser crash 或挂起，不宣称支持透传 |
| 生命周期隔离 | test_lifecycle_review.py 验证慢管理请求、并发/取消 close、挂起后端、两阶段延迟、启动取消重试、第二 Runtime 拒绝以及排队关闭/启动所有权 |
| 配置与状态额外边界 | test_state_review.py 验证版本类型、UTF-8 字节上限、错误值和字典 key 脱敏、保留头、YAML 别名、TTL 边界、700 请求/7 scope 和管理错误不改状态 |
| 客户端业务场景 | test_example_catalog.py 执行示例规则：持续 401/409/503、HTML 错误、HTTP 200 业务错误、无效/空 JSON、307 保留 POST、超时→503→200 |
| 配置编辑与发现 | test_cli.py 验证无需配置/token 的 JSON Schema、全部 10 种动作、缺失/未知 action 候选提示、YAML 行列与秘密 key/value 脱敏 |
| 定向与恢复场景 | test_example_catalog.py 直接加载示例：方法/正则/header/query AND、重复 query 解码、scope 隔离、维护健康检查豁免、预热后循环与单 scope reset |
| 无 body 与二进制场景 | 同一目录验证条件 GET/HEAD 304、DELETE 204、HEAD 后 GET 的二进制连接分帧、HTTP 504，与后端调用次数分别断言 |
| 完整目录可达性 | test_every_catalog_rule_is_reachable_in_the_full_ordered_configuration 使用完整 YAML 顺序逐条选中规则，防止新增宽匹配遮蔽已有场景 |
| 管理查询与维护 | test_maintainability.py 验证 AND 精确查询、未知/重复参数 400、未认证 401、空值不扩大选择、查询不延长 TTL、过期 reset 返回 0、规则摘要不泄露 body/header/query 值 |
| Docker runner 体验 | test_runner.py 验证无参数/帮助/缺 Docker 提示、参数与空字符串原样转发、/workspace 挂载、loopback 发布与退出码保留；替代 Docker CLI 仅用于 runner 参数测试 |
| 正则计算边界 | test_regex_safety.py 使用带外层超时的子进程，将嵌套重复表达式与 10000 字符近似匹配送入 Engine；验证完整路径、分支、inline flags、长度和不兼容语法拒绝 |
| 不可变执行计划 | test_execution_plan.py 与 test_architecture_integration.py 验证源配置列表/字典变更不影响匹配、已分配动作、实际延迟响应、后续路由或管理目录；响应只编码一次 |
| 启动回滚与失败重试 | test_runtime_rollback.py 验证重复取消仍后台清理、并发 close 加入回滚、失败保留资源/进程所有权、保留原始启动异常且可重试清理 |
| 依赖兼容与安装包 | test_dependency_compat.py 验证版本、reader 能力、外部适配器保护、安装恢复幂等；check-wheel.sh 从已安装 wheel 验证 CLI 和双向协议适配 |
| 连接/在途资源限额 | test_resource_limits.py 验证跨服务共享预算、慢上传先占位、首请求 POST/HEAD 容量拒绝、延迟期间不提前释放、RST 客户端互不影响及首步前取消 |
| 拒绝响应的协议与释放 | 同一文件验证输出阻塞最多等待 0.5 秒后释放；已成功响应 4 MiB 数据后的管道请求若超限，只关闭连接而不把 503 插入上一响应 |
| 有界管理分页与过期清理 | test_execution_plan.py 与 test_resource_limits.py 验证过滤绑定游标、创建顺序、固定 ID 上界、空页继续、历史 reset 空洞、过期积压和当前 scope 重启 |

测试分布：architecture_integration 1、boundaries 11、CLI 12、config 36、demo 1、dependency_compat 5、engine 10、example_catalog 18、execution_plan 23、extended_scenarios 19、http_review 13、integration 36、lifecycle_review 9、maintainability 14、network_edges 10、regex_safety 17、resource_limits 24、runner 6、runtime_rollback 4、state_review 26、transport 9。

## 实际端口映射体验

首轮（2026-09-22）另外执行了 README 的 `--publish` 演示及跨容器 curl 命令；下表保留该环境实测记录，本轮未重复宿主机映射测试：

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

第二轮先由 HTTP、状态/配置、生命周期三个方向独立检查并保留失败测试，再修复和自审。修复了慢管理请求拖延关闭、并发关闭异常、取消关闭泄漏、第二 Runtime 覆盖全局 context、错误消息泄露配置、文件大小按字符而非字节计算、压缩 mock 二次压缩、Latin-1 头编码、方法大小写和重复 scope 冲突。对锁定版本增加最小 trailer 拒绝适配；请求侧接受“明确关闭”作为拒绝契约，而非伪称一定返回 400。最终自审额外修复了排队启动时所有权被先前清理释放，以及 reset_after 失败的 HEAD body 泄漏，两者均有专门回归测试。

2026-09-26 第一轮从配置编辑、规则发现、并行测试操作和示例复用四条路径复审。管理接口新增回归先得到 13 失败/1 通过，随后修复；全部 35 项新增测试最终通过。自审补充了首次/重跑的 scope 说明，避免按文档重复执行时跳过故障；将场景目录集中到 `docs/scenarios.md`。另做 runner/CLI 与场景文档交叉审查。

随后架构审查复现了正则阻塞和启动回滚双重取消的监听器泄漏，落实非回溯匹配、受取消保护的回滚、兼容版本边界、只读计划和资源限额。交叉审查修复了拒绝响应超时后再次无限等待、历史状态 ID 空洞导致首屏不可用，以及复用连接直接注入 503 可能损坏前一响应的问题。新增失败测试分别验证了修复；实际测试还暴露端口辅助函数可重复选中同一端口，现已避免进程内重复分配，测试后端改为直接监听端口 0。

行为变更已写入使用文档：不兼容的 Python 正则语法会在启动前拒绝；超过单页上限的状态需要翻页；资源超限可能关闭复用连接。这些均有明确测试与操作说明。最终审查未留下未解决的阻塞项，协议验证范围保持如下。

## 未覆盖的协议与环境

当前完整验证基线为 Linux Docker 内 HTTP/1.1、HTTP/HTTPS upstream、单进程状态。HTTP/2/3、WebSocket、SSE/流式中途截断、客户端 HTTPS 入口、DNS 失败、建连超时、丢包、限速、多副本共享状态不在此次实现承诺中，也没有以普通 HTTP 测试代替这些能力的证据。外部客户端错误包装和额外转发层需要在实际客户端环境中另行验证。

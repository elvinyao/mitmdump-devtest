# 验收记录

## 2026-10-03 原始用途复评

结论：在已声明的 HTTP/1.1 开发调试范围内，工具满足多后端反向代理、读超时、4xx/5xx、按次故障与恢复的用途。它负责制造故障和提供请求证据；被测应用的最终结果、异常分类、业务幂等和精确退避需要应用侧断言。

| 原始要求 | 当前实现与验收依据 |
| --- | --- |
| 为各后端提供 reverse proxy | 一个 service 对应固定 upstream/端口；多服务真实网络测试与完整双后端接入配置，客户端替换各自 base URL |
| 模拟超时 | timeout、转发前/后 delay；真实客户端 ReadTimeout，curl 28 与 HTTP 504 分别验收；不将读超时冒充 DNS/建连超时 |
| 模拟 4xx、5xx | 200–599 的 respond/respond_after；现有状态码、响应体、后端调用断言 |
| 第 n 次开始连续两次 4xx，之后 OK | start_at + repeat + 固定 respond 200；新增真实客户端测试证明第 1–4 次正常转发、第 5–6 次 429、第 7 次固定 200，后端恰好 4 次调用 |
| 验证 retry/error handling | 新增响应驱动的参考客户端及 7 项黑盒测试：429/503 后恢复、401 不重试、503 次数上限、读超时后恢复、无效 gzip 不把 HTTP 200 当作客户端成功 |
| 清晰易用、使用手册完整 | README 按原始需求导航，场景手册先列基础配方，使用手册提供双后端配置、base URL/端口对应、客户端断言矩阵、checkpoint 与重复运行流程 |
| 易扩展、开发手册完整 | 补齐 config→只读 plan→决策→执行→诊断/验收的动作扩展流程、匹配扩展、jitter 参数实例、修改到测试的对应表；不声称支持热插拔插件 |

本轮独立审查分为用途/协议、使用手册、开发与扩展三个方向。文档验证直接提取四段新命令，在同一 Docker 容器内连接真实 demo，连续运行两轮均通过；只将 host.docker.internal 映射地址替换成容器内直连地址。双真实后端配置通过模型校验，demo SIGTERM 正常退出。本轮没有重新实测宿主机端口映射，相关结果仍引用下面的历史环境记录。

修复了自定义方法 `head` / `hEaD` 被底层误当作标准 `HEAD`，导致 body 缺失或 chunked 响应不完整的问题。原始 socket 回归先得到 12 失败 / 6 通过，再验证 mock、after、透传、Content-Length/chunked、后续 keepalive 请求及真实上游 method。适配保持版本/接口检查、外部适配保护与关闭恢复；仅精确 `HEAD` 不发送 body。参考客户端的无效 gzip 回归也先复现未捕获异常，再改为不重试的 decode_error/退出 1。

独立质量复核进一步发现，对上述自定义方法直接移除复合 Transfer-Encoding 会丢失 gzip/deflate 等编码含义。新增回归先得到 8 失败 / 4 通过，再改为明确拒绝有 body 的复合 chunked 编码，走正常上游 502 错误路径；Journal 为 transport_error/status=null，而非伪造成功。普通方法、精确 HEAD、无 body 的状态语义保持不变，普通 Content-Encoding 压缩也不受此限制。HTTP/适配器 focused 测试合计 56 项通过；开发手册写明了这项兼容边界。

最近完整验证日期：2026-10-03。环境：Docker/OrbStack 中的 Linux、Python 3.12.14。依赖由 uv.lock 锁定，其中 mitmproxy 12.2.3、ruff 0.16.8、ty 0.0.83、pytest 9.1.1。

## 结果

- **509 tests passed，0 failed，0 skipped**，在上一轮 464 项基础上增加 45 项：30 项真实 HTTP 协议回归、8 项依赖适配边界、7 项参考客户端黑盒验收；pytest 用时 90.23 秒。
- coverage 同时统计行与分支，并包含 CLI/demo 子进程：**96%**（1698 个 statement、520 个 branch）。覆盖率是测试范围指标，不是对任意网络环境的正确性保证。
- `uv lock --check`、`uv sync --locked`、`ruff format --check .`、`ruff check .`、`ty check` 全部通过。
- `uv build` 成功生成 wheel 和 sdist。独立虚拟环境离线安装 wheel，确认从安装路径导入，schema、init/validate/explain、requests/verify/reset/journal-clear、HTTP mock、自定义方法响应分帧及请求/响应 trailer 拒绝检查通过。生产锁文件作为版本约束，依赖由 wheel 元数据决定。示例目录保持 2 个服务、33 条规则。
- 本轮完整检查由 `bash .agent/run.sh bash .agent/check.sh` 执行，退出 0；格式检查覆盖 60 个文件。上一轮已验证登录 shell 的 uv 与 uvx 均为 0.12.17，本轮沿用同一工具链。
- 程序和 Docker runner 都实际运行过，测试没有用 mock 替代 mitmproxy 或 TCP reset。
- 测试保留了 **42 条第三方弃用警告**：mitmproxy 使用 pyparsing 的旧 API，以及 ldap3 对 pyasn1 旧导出的引用。没有将这些警告隐藏或描述为零警告。

复现全部质量门槛：

```bash
bash .agent/run.sh bash .agent/check.sh
```

早期架构轮次加强 Engine 正则回归与 wheel 依赖检查的历史复验命令（当前验收以本轮完整 check 为准）：

```bash
bash .agent/run.sh sh -lc 'uv run --no-sync ruff format . && bash .agent/check.sh'
bash .agent/run.sh uv run --no-sync pytest tests/test_regex_safety.py -q
bash .agent/run.sh bash .agent/check-wheel.sh
```

2026-09-27 继续复核不可变计划、分页、资源配额、启动回滚和协议适配的实现及回归测试，重新执行 `bash .agent/run.sh bash .agent/check.sh`：304 项全部通过，覆盖率 96%，构建及独立 wheel 检查通过，未发现新的阻塞项。

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
| 配置模板与离线诊断 | test_local_commands.py 的 31 项验证四种合法模板、无 token、校验先于独占写入、文件不覆盖、匹配/采样复用、状态与 TTL 不变、scope 脱敏和真实 mitmproxy 非法 UTF-8 query 解码一致 |
| 请求记录与证据完整性 | test_journal.py 的 11 项覆盖进入顺序、幂等终结、容量/clear 丢失边界、跨实例/未来游标、过滤分页、pending/关闭时禁止通过、精确次数/状态/单调间隔与只读返回 |
| Journal 真实网络集成 | test_journal_integration.py 的 28 项验证 503/503/200 恰好三条、后端调用、请求数据不存储、reset/timeout/after/取消/上游失败、鉴权、严格参数和 4 KiB 上限 |
| 管理客户端错误契约 | test_admin_client.py 的 60 项覆盖四命令、token/URL、超时、重定向、代理隔离、401/拒绝连接、畸形或过大响应、深层 JSON 序列化失败及 0/1/2 退出码 |
| 完整 CLI 验收与重跑 | test_usability_workflow.py 的 6 项通过真实子进程运行 init→validate→explain→serve→503/503/200→requests→verify，断言错误退出 1、scope 隔离、reset/clear/checkpoint 重跑，以及 HTTP/HTML/重定向/超时/拒绝连接退出 2 |

新增 test_sampling.py 的 24 项验证：0/1 概率边界、严格配置、seed/reset 重放、并发 scope 隔离、五种延迟动作、只读计划、概率与延迟通道独立、start_at/cycle 位置、真实 HTTP 后端调用次数、管理摘要和实际延迟日志。全目录可达性测试同时覆盖新增三个示例。

历史测试分布（328 项基线）：architecture_integration 1、boundaries 11、CLI 12、config 36、demo 1、dependency_compat 5、engine 10、example_catalog 18、execution_plan 23、extended_scenarios 19、http_review 13、integration 36、lifecycle_review 9、maintainability 14、network_edges 10、regex_safety 17、resource_limits 24、runner 6、runtime_rollback 4、sampling 24、state_review 26、transport 9。2026-09-29 易用性扩展增加上表五组共 136 项，达到 464 项。本轮新增 client_contract 7 项，dependency_compat 增至 13 项，http_review 增至 43 项，总计 509 项。

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

2026-09-29 易用性扩展先提交 superpowers 设计和实施计划。TDD 失败证据包括：init/explain 28 项因命令/方法不存在失败，Journal 导入失败，HTTP /requests 返回 404，管理客户端导入失败，以及完整 CLI 流程因 journal-clear 未注册失败。实现后分别通过，再执行独立需求审查和质量审查。

需求审查发现 explain 回显测试 scope 原值，与“无 header/query 值”约定冲突；已删除输出字段并保留内部采样。质量审查复现非法 UTF-8 query 被替换为 U+FFFD 导致离线与实际匹配不同，改用 mitmproxy 相同的 surrogateescape；另复现约 2.5 KiB 的深层管理 JSON 可解析却在输出时抛 RecursionError，现于任何 stdout 输出前捕获并返回静态错误/退出 2。三个问题均先保留失败回归、修复后通过，独立复核确认关闭；最终规格审查 A/B/C 无未完成项。需求审查另执行 journal+集成 39 项、local+完整 CLI 37 项通过。

采用 superpowers 的设计、计划、TDD、根因定位与完成前验证流程，子代理执行 transport 实现和独立审查。先做需求审查，再做质量审查，反馈修复后复核。

已修复并回归验证：背压关闭卡住、SO_LINGER 失败丢失映射、HEAD mock 破坏连接分帧、非法 method 未拒绝、原始 Host 无法匹配、跨字段配置错误缺少字段路径、IPv6 wildcard URL 指向错误地址。各审查项已关闭。

第二轮先由 HTTP、状态/配置、生命周期三个方向独立检查并保留失败测试，再修复和自审。修复了慢管理请求拖延关闭、并发关闭异常、取消关闭泄漏、第二 Runtime 覆盖全局 context、错误消息泄露配置、文件大小按字符而非字节计算、压缩 mock 二次压缩、Latin-1 头编码、方法大小写和重复 scope 冲突。对锁定版本增加最小 trailer 拒绝适配；请求侧接受“明确关闭”作为拒绝契约，而非伪称一定返回 400。最终自审额外修复了排队启动时所有权被先前清理释放，以及 reset_after 失败的 HEAD body 泄漏，两者均有专门回归测试。

2026-09-26 第一轮从配置编辑、规则发现、并行测试操作和示例复用四条路径复审。管理接口新增回归先得到 13 失败/1 通过，随后修复；全部 35 项新增测试最终通过。自审补充了首次/重跑的 scope 说明，避免按文档重复执行时跳过故障；将场景目录集中到 `docs/scenarios.md`。另做 runner/CLI 与场景文档交叉审查。

随后架构审查复现了正则阻塞和启动回滚双重取消的监听器泄漏，落实非回溯匹配、受取消保护的回滚、兼容版本边界、只读计划和资源限额。交叉审查修复了拒绝响应超时后再次无限等待、历史状态 ID 空洞导致首屏不可用，以及复用连接直接注入 503 可能损坏前一响应的问题。新增失败测试分别验证了修复；实际测试还暴露端口辅助函数可重复选中同一端口，现已避免进程内重复分配，测试后端改为直接监听端口 0。

行为变更已写入使用文档：不兼容的 Python 正则语法会在启动前拒绝；超过单页上限的状态需要翻页；资源超限可能关闭复用连接。这些均有明确测试与操作说明。最终审查未留下未解决的阻塞项，协议验证范围保持如下。

## 未覆盖的协议与环境

当前完整验证基线为 Linux Docker 内 HTTP/1.1、HTTP/HTTPS upstream、单进程状态。HTTP/2/3、WebSocket、SSE/流式中途截断、客户端 HTTPS 入口、DNS 失败、建连超时、丢包、限速、多副本共享状态不在此次实现承诺中，也没有以普通 HTTP 测试代替这些能力的证据。外部客户端错误包装和额外转发层需要在实际客户端环境中另行验证。

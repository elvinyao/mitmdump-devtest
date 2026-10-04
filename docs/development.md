# 开发文档

## 从哪里开始

先按 [README](../README.md) 启动演示，再用 [使用文档](usage.md) 跑一遍配置生成、离线解释和请求验收。修改实现前，按以下顺序阅读即可建立完整调用链：

1. `src/fault_engine/config.py` → `plan.py`：输入验证与运行期只读快照的边界。
2. `engine.py` → `addon.py`：同步选择一个 Decision，再异步执行故障；匹配、序号与网络动作分开。
3. `runtime.py` → `transport.py` / `limits.py`：服务路由、生命周期、真实 TCP 行为与配额所有权。
4. `journal.py` → `admin.py` / `admin_client.py`：请求证据、查询和断言；`local_commands.py` 负责离线模板与解释。

下文的源文件名均相对于 `src/fault_engine/`。日常执行命令见“工具链与日常命令”，按改动选择测试见“测试策略”；新增行为的具体接入点见最后的扩展指南。

## 架构

```text
客户端 → 每服务 TCPBridge → 内部 mitmproxy HTTP/1.1 → 服务对应 upstream
                                  ↓
                         FaultAddon → Engine → Decision
                                          ↑
                          独立 Admin API 查询/重置
```

内部 mitmproxy 仅在 loopback 随机端口监听。每个外部服务监听器建立独立内部连接，在开始转发字节前注册内部 socket 地址映射。addon 使用 `HTTPFlow.client_conn.peername` 查回服务和公开客户端连接。客户端不能通过 URL/Host 改变固定 upstream。

不为每个服务创建一个 Master：mitmproxy 使用进程级 context。同一进程应只有一个 Runtime，多服务共用一个 Master 和一个内部监听器。`Runtime.start()` 显式调用 proxyserver setup 与 Master running lifecycle，`close()` 按逆序清理管理服务、桥接连接、延迟 hook、内部监听器、Master 和临时证书目录。mitmproxy 的嵌入接口升级后必须重新跑集成测试。

Runtime 现在在构造 Master 前拒绝第二个活跃实例。生命周期锁串行化启动和清理，并发 close 共享受 shield 保护的清理任务；取消 close 的调用者不会取消后台清理。管理请求关闭宽限为 0.2 秒，未完成的请求体不能让关闭等待默认的 60 秒。启动取消或失败后释放监听器和进程占用，可重新启动。API 应在同一 asyncio event loop 内使用。

启动失败回滚也通过独立 `_cleanup_task` 执行。重复取消 start 的调用者时，后台仍继续回收资源，后续 close 加入同一个清理任务。失败资源引用和 Runtime 所有权会保留，显式 close 可重试；所有清理成功后才恢复协议适配并释放所有权。启动异常保留为主异常，回滚失败通过异常链和日志报告。

## 源文件职责

| 文件 | 职责 |
| --- | --- |
| config.py | strict pydantic 配置、YAML 重复 key 检查、响应编码与交叉引用 |
| matching.py | 有长度限制的 Rust regex 完整匹配、语法错误脱敏 |
| plan.py | 将输入模型编译为只读规则、服务和动作快照，预编码响应与累计 repeat |
| engine.py | 共享的纯匹配/采样与 explain、序号分配、TTL/容量、查询与重置 |
| addon.py | mitmproxy HTTP hooks、异步延迟、mock、故障事件 |
| transport.py | 不解析 HTTP 的 TCP 字节中继、地址映射、SO_LINGER RST |
| limits.py | 单事件循环内的共享连接/请求配额与幂等释放凭据 |
| journal.py | 有界的请求元数据、幂等终结、实例游标和完整性验证 |
| admin.py | 独立 aiohttp 管理 API、鉴权、输入大小和 reset 校验 |
| runtime.py | 组装、启动、异常回滚和关闭 |
| http1_compat.py | 活跃 Runtime 期间安装并在关闭后恢复的 trailer 拒绝、HEAD 大小写定界与中间响应适配 |
| local_commands.py | 四种配置模板、独占创建文件、离线请求解析与解释 |
| admin_client.py | 有界管理 HTTP 客户端、环境凭证、断言退出码 |
| cli.py / __main__.py | 命令注册、配置错误脱敏、serve 信号处理 |

## 状态与并发不变量

`Engine.decide()` 没有 await；同一 asyncio event loop 内匹配和递增为一个同步步骤。不要从其他线程调用 Engine，也不要在计数分配前插入 await。异步动作只在 Decision 已生成后执行。Decision 为冻结 dataclass，运行期间不修改配置。

状态键为 `(service, rule, scope)`。OrderedDict 按最后命中排序，先批量移除过期键，再检查容量；不会为新 scope 静默踢掉活跃场景。每批至多清理 1000 条并汇总日志，避免大量过期状态使一次普通请求处理全部清理。当前命中 scope 若位于尚未清理的过期区间，仍另行删除并从 1 开始。reset 删除计数，不修改已经选中的 Decision。尚未命中规则的请求不占用状态容量。

`snapshot()` 与 `reset()` 共用 `_matching_keys()`，跳过所有已过期条目再执行 service/rule/scope 的 AND 精确过滤；读取不会刷新最后命中时间。管理 `/state` 使用 `snapshot_page()`，按独立的条目创建 ID 分页，避免命中顺序变化造成重复。游标绑定实例、过滤条件和本轮 ID 上界；扫描与输出都有上限，可出现带后续游标的空页。游标不是事务快照，reset/TTL 会使条目消失。管理接口拒绝未知或重复 query 参数。`/rules` 通过字段允许列表输出动作摘要；新增动作时显式决定哪些参数可公开，不要直接序列化完整规则。

输入 Config 仍采用便于 YAML/JSON 验证的 Pydantic 模型，其 list/dict 可变；Runtime 创建时通过 `compile_plan()` 生成独立 ExecutionPlan。Plan 使用冻结 dataclass、tuple、只读映射和 bytes，Engine、Addon、Admin 共享同一个对象。响应 body/header 只编码一次，动作重复次数预计算为累计区间，通过二分选择。外部修改输入配置不会改变在途或后续请求；没有配置热替换 API。以后增加热加载时，需要明确计划版本与计数迁移策略。

`path_regex` 用 Pydantic Core 的 Rust regex 显式编译，禁止回退到 Python re。先验证原表达式，再加完整路径锚点，避免不平衡分组逃出包装。原模式限制 4096 字符；不支持环视和反向引用。反例回归放在有外层超时的独立子进程中，防止意外恢复回溯实现时挂住整套测试。

## 可复现采样

RulePlan 保存 probability/seed，ActionPlan 保存 jitter_seconds。Engine 分配序号并选择步骤后，以版本化 SHA-256 输入生成概率与延迟的独立样本；未抽中直接选择共享 PASSTHROUGH，不尝试下一条规则。抖动通过 dataclasses.replace 创建本次动作，只读配置仍保留原始基础值与 jitter。禁止改为进程全局 random 状态，否则不同 scope 的并发会改变重放结果。更新采样算法需明确版本兼容性；tests/test_sampling.py 覆盖 reset、scope 隔离、序列位置、通道独立及真实 HTTP。

## 诊断与观察证据

Engine.explain 与 decide 共用匹配失败维度、scope 验证及指定序号的动作选择，但 explain 不访问时钟、计数或过期清理。输出只包含安全摘要，scope 值参与内部采样而不出现在 explain JSON。local_commands 的模板先通过完整 Config 验证，再以 x 模式创建；不可改成检查存在后普通覆盖写入。

每个 Runtime 共享一个 Journal 给 Addon/Admin。Addon 在完整请求体和场景选择之后开始一条观察；不同 hook 的日志事件不直接作为请求计数。Journal 使用 OrderedDict 按进入顺序保存最新 N 条、单调时钟计时；finish 只允许 pending 转为最终结果，客户端断开、请求取消或关闭也必须终结。它不持有 HTTPFlow、网络凭据或任意 body/header/query/path。

Journal 的递增 ID 与 dropped_through 保证历史丢失可见；clear 更新丢失边界而不复用 ID，旧请求完成不会重新插入。cursor 为不应由客户端解析的实例/位置标记，仅作排他下界，不绑定过滤和上界（与 state 游标不同）；翻页保持相同过滤，验收调用方须等待客户端完成。verify 即时校验完整窗口，关闭、缺失或 pending 时不得通过；arrival gap 不能被描述成客户端 backoff。

管理 CLI 不依赖本地配置，凭证只从命名环境变量获取。HTTP 有总超时、响应字节上限，禁止重定向与环境代理；不把远端错误 body 或连接异常详情写入 stderr。错误退出 2，断言失败/不完整退出 1，完整通过退出 0。变更这些契约时同时更新 API、CLI、真实子进程和 wheel 测试。

## 资源配额

每个 Runtime 只创建一个 ResourceBudget，所有 TCPBridge 与 FaultAddon 共享。连接接受时取得连接凭据，结束时幂等释放；超过上限即关闭。请求从 requestheaders 开始占位，覆盖请求体、上游等待和各阶段延迟，响应 hook 完成、错误、客户端断开或关闭时归还。管理监听器不消耗数据面配额。

mitmproxy 在 requestheaders 设置 response 后仍可能缓冲 body，故首个请求的容量拒绝由 Addon 构造少量 HTTP 字节，交给 Bridge 在停止中继后有界发送；Bridge 不解析 HTTP。公开 writer 及凭据始终由原中继任务持有，拒绝调用方取消不会丢失清理。发送超时后立即 abort。复用/管道连接可能仍有上一响应未转发完，超限只能关闭，不能注入新的 503。新增 hook 或动作时必须检查取得/归还凭据的路径，不得在拒绝后调用 Engine 分配序号。

## RST 与关闭

普通 `flow.kill()` 不等于可证明的 TCP reset。`TCPBridge.reset(peer)` 找到该公开 socket，设置 `SO_LINGER=(1,0)` 后 abort；Linux socket 测试必须确实观察 `ConnectionResetError` 且 errno=104。SO_LINGER 失败时保留映射并报告失败，不把失败当作成功。

协议验证分层记录：容器内直连的 RST 测试不能证明经过 OrbStack/Docker 映射后的异常形式。当前宿主机映射演示测到 curl 52（EOF），而容器内直连测到 ECONNRESET 104；开发客户端需要精确 reset 语义时应使用直接网络路径。

正常中继维持半关闭语义：一方向 EOF 后继续接收另一方向。取消、重置或异常时 abort 两边 transport；否则有缓冲数据且接收方不读时，`wait_closed()` 可能永远等不到排空。回归测试覆盖这一风险。关闭还需处理已接受但协程尚未开始的连接。

## 工具链与日常命令

宿主机只用于文件查看和编辑。依赖安装、格式化、类型检查、测试、构建、运行示例均在 Docker 中：

```bash
bash .agent/run.sh uv sync --locked
bash .agent/run.sh uv run pytest tests/test_engine.py -q
bash .agent/run.sh uv run pytest tests/test_integration.py tests/test_network_edges.py -q
bash .agent/run.sh uv run ruff format .
bash .agent/run.sh uv run ruff check .
bash .agent/run.sh uv run ty check
bash .agent/run.sh bash .agent/check.sh
```

Python 固定 3.12，包元数据和锁文件共同约束 mitmproxy==12.2.3、h11==0.16.0；Pydantic Core 的公开 schema API 也显式列为依赖。runner 引导 uv 0.12.17。`.venv-docker` 不能在 macOS 宿主机运行。ruff/ty 排除该第三方虚拟环境，ty 检查 src、tests 和 examples。

[`examples/retry_client.py`](../examples/retry_client.py) 是仓库中的 GET 重试教学示例，使用 `uv sync --locked` 安装的开发依赖 httpx；它不随生产 wheel 发布。默认最多尝试 3 次，仅在 429、503 或读取超时后固定等待并重试；它不解析业务 payload、不解释 Retry-After，单次网络操作超时也不是整体重试截止时间。接入真实应用时替换请求循环，并分别断言应用结果和代理 Journal，不能用代理记录的 200 代替客户端成功。

runner 的 `--help` 与缺少命令提示无需 Docker；实际执行仍全部经 Docker。容器入口将缓存中的 uv/uvx 链接到 `/usr/local/bin`，保证 `sh -lc` 重置 PATH 后仍能找到工具。`tests/test_runner.py` 在容器内用替代 Docker CLI 验证参数原样传递、发布地址、工作目录和失败退出码，不启动嵌套容器。

`fault-engine schema` 直接从 Pydantic 模型输出 JSON Schema，不另存一份需要同步的静态定义。YAML 错误只输出行列；字段错误只保留已知 schema 路径，禁止回显配置值或自定义字典 key。

管理 CLI 对 `verify` 单独使用 4 MiB 响应上限：最大 100000 条记录的状态码/null 和有限浮点间隔加 JSON 分隔符小于 3.4 MB，另留固定字段空间。其他命令仍为 2 MiB，不能取消流式累计大小检查。修改 Journal 容量或验收输出结构时重新核算此上界，并运行 `test_large_verification.py` 的真实 CLI 往返和分块边界测试；管理请求体的 4 KiB 限制独立生效。

完整 check 在构建后调用 `.agent/check-wheel.sh`：将锁定生产依赖导出为版本约束，仅以实际 wheel 为安装目标，离线安装到临时虚拟环境，从仓库外执行 schema、init/validate/explain、管理 CLI、HTTP mock 及双向 trailer 拒绝检查。所需依赖由 wheel 元数据决定，缓存由前面的 `uv sync --locked` 准备。这样验证安装包的依赖声明和导入路径，而不只验证 editable 源码环境。

依赖升级使用 `bash .agent/run.sh uv lock --upgrade-package <package>`，随后执行完整 check。runner 的多个容器共享虚拟环境；不要并发修改依赖或格式化同一文件。无需 sudo 在宿主机安装工具。

## 测试策略

先运行与改动对应的测试，最后运行完整 check。下表文件名加上 `tests/` 前缀后，填入 `bash .agent/run.sh uv run pytest <测试路径> -q`：

| 改动 | 优先测试 | 必须保留的证据 |
| --- | --- | --- |
| 配置字段、错误提示与只读快照 | `test_config.py`、`test_execution_plan.py`、`test_state_review.py` | 拒绝隐式转换/冲突输入，错误脱敏，原配置变动不改变运行计划 |
| 匹配、序号、scope、TTL 与采样 | `test_engine.py`、`test_regex_safety.py`、`test_sampling.py`、`test_local_commands.py` | 优先级、序列边界、scope 隔离、非回溯匹配、explain 与 decide 一致 |
| HTTP 动作及协议行为 | `test_integration.py`、`test_extended_scenarios.py`、`test_network_edges.py`、`test_boundaries.py`、`test_http_review.py`、`test_interim_responses.py` | 真实客户端结果和后端调用次数；TLS、HEAD、取消、中间响应与最终响应、原始 Host、控制头、body 上限及原始 HTTP 字节 |
| TCP、连接和请求配额 | `test_transport.py`、`test_resource_limits.py` | ECONNRESET 104、其他连接不受影响、半关闭、背压、慢上传、复用连接及凭据归还 |
| 生命周期、计划共享与依赖适配 | `test_lifecycle_review.py`、`test_runtime_rollback.py`、`test_architecture_integration.py`、`test_dependency_compat.py` | 并发/取消关闭、启动回滚重试、端口释放、全局状态隔离及同一 Plan |
| 管理摘要、观察记录与验收 | `test_maintainability.py`、`test_journal.py`、`test_journal_integration.py`、`test_admin_client.py`、`test_usability_workflow.py`、`test_reset_contract.py`、`test_large_verification.py` | 过滤/分页、拒绝错误重置范围、脱敏、保留窗口、终结、最大记录容量与响应大小边界、真实 CLI 退出码和重置重跑 |
| 客户端是否按策略重试 | `test_client_contract.py` | 子进程执行真实示例客户端；429/503、读取超时、尝试上限、非重试状态、响应解码失败，以及第 5/6 次失败、第 7 次固定成功；同时断言客户端与后端结果 |
| CLI、示例与工具链 | `test_cli.py`、`test_demo.py`、`test_example_catalog.py`、`test_runner.py` | 真实子进程/curl/SIGTERM、完整 YAML 目录的规则可达性、runner 参数传递；安装包另由 `.agent/check-wheel.sh` 验证 |

`tests/conftest.py` 提供 `rule()`、真实后端 `upstream` 和启动 Runtime 的 `proxy` fixture。HTTP 动作测试同时检查客户端结果与后端收到的请求，区分“故障在发送前发生”和“后端已经执行”。涉及超时或取消时用有上限的等待，不要求精确毫秒；涉及 wire 格式或 reset 时使用原始 socket 断言。

测试后端直接监听端口 0；需要先写入 Config 的端口由 `free_port()` 保证同一测试进程内不重复分配，避免服务/admin 尚未绑定时得到同一端口。该辅助函数不提供跨进程保留保证；监听器测试应在隔离的 runner 网络中执行。

`http1_compat.py` 对 mitmproxy 12.2.3 安装三个局部适配。私有 HTTP/1 reader 工厂在 h11 解析出非空 trailer 时转为协议错误，避免依赖内部抛出未处理的 NotImplementedError；请求侧原生错误路径会先关连接，不能承诺返回 400，响应侧为 502。`expected_http_body_size()` 的适配只让自定义方法 `head` / 混合大小写变体按普通响应定界；使用请求的浅副本，不改变发往后端的原始 method 或上传定界。仅精确 `HEAD` 因方法语义而无响应 body。Addon 将这些自定义方法的完整缓冲、单一 chunked 响应改用 Content-Length，避免依赖省略最后一个 chunk 而破坏 keepalive。

第三个适配包装 `Http1Client.read_headers()` 的生成器，在原解析器已解析响应头、尚未建立 body reader 或结束流的暂停点消费非 101 的 1xx，再迭代读取最终响应。中间响应不进入 response hooks，不转发给客户端；因此后置故障、Journal 和请求配额只随最终响应推进。每请求最多 100 个中间响应，超过时走正常上游 502 路径；最终响应及错误清理计数，弱引用避免连接对象滞留。101 保留原路径，不借此声明支持升级协议。

这些自定义 HEAD 大小写方法若收到需要读取 body、且使用 `gzip, chunked` 等组合 Transfer-Encoding 的上游响应，会走正常协议错误路径返回 502；不移除编码元数据后转发压缩字节。精确 HEAD、其他方法和 304 等本来无 body 的响应不受此额外限制影响。该错误可能发生在后端已执行之后，Journal 记录 `transport_error`、`status=null`、`upstream_received=false`，也说明后者不能证明后端未执行。

安装前检查依赖版本、三个入口的签名与行为，包括中间响应生成器暂停时尚未推进流状态的契约，拒绝覆盖其他适配器；关闭时恢复各自原函数。没有复制整套 HTTP parser，也不静默丢弃 trailer。依赖升级需要同步包元数据、支持版本表和锁文件，重跑真实 HTTP 的方法大小写/后续请求对齐、中间响应/后置动作，以及源码与已安装 wheel 的双向 trailer 测试；上游原生处理修复后应移除相应适配。`test_dependency_compat.py` 覆盖外部适配冲突、API 漂移、安装/恢复和原请求不变性，真实 socket 回归覆盖 mock、after、透传、分片、最终响应缺失与 keepalive。

仓库开发流程是：先明确行为与边界，添加能复现缺失行为或缺陷的测试，再修改实现、独立审查并完成验证。使用 Docker runner 即可执行，不要求安装额外的工作流插件。历史设计决策保存在 [初始设计](superpowers/specs/2026-09-22-fault-engine-design.md) 和 [易用性扩展设计](superpowers/specs/2026-09-29-usability-design.md)；当前行为以实现、使用文档与测试为准。不要把网络异常全部放宽为“任何 exception”来让测试通过；reset 的原始 socket 断言必须保留。

## 扩展动作

现有动作通过显式类型与分支连接，没有自动注册插件。新增动作或参数必须走完 `config → plan → Engine → Addon → 诊断/验收`，仅增加 YAML 字段不会自动成为运行期行为。

1. 先定义发送前还是收到完整上游响应后执行、是否调用后端、客户端应看到什么，以及取消/失败时的结果。添加对应配置和行为失败测试。在 `config.py` 为新动作建立严格模型并加入带 discriminator 的 `Action` union；增加现有动作参数时补齐类型、上下界、互斥与组合约束。
2. 在 `plan.py` 的 `ActionPlan` 和 `_action_plan()` 显式承接新参数。该转换只复制已列出的字段，配置验证通过不代表字段已经进入运行计划。可变集合须复制并冻结，响应编码在编译时完成；用 `test_execution_plan.py` 验证值被保留，修改原配置不会影响计划或在途 Decision。
3. 保留 Engine 的同步选择边界。普通网络动作无需在 Engine 加入 I/O；涉及概率、延迟或序列选择时检查 `_decision()`、`_select()` 与 explain。抖动必须使用既有独立采样通道，不得让执行顺序推进全局随机状态，也不能修改共享 ActionPlan。
4. 在 `addon.py` 的 `_request()` 或 `_response()` 对应阶段实现动作。异步等待使用能被关闭/取消路径管理的方式；TCP 行为通过 `transport.py` 接口，不能直接访问私有 socket 或把 kill 叫作 reset。检查 `requestheaders` 取得请求凭据之后，成功、异常、客户端断开和关闭都能释放；容量拒绝不能分配场景序号。
5. 同步决定观察语义。每条记录只终结一次，`Journal.mark_upstream()` 表示已收到上游响应；`upstream_received=false` 不证明后端从未执行，`response_prepared` 不表示客户端已经读完响应。新增提前返回、断开或异常分支须明确 outcome/status，避免遗留 pending 或把本地合成响应算作上游响应。
6. 审查 `admin._action_summary()`、`Engine.explain()` 和 `FaultAddon._event()` 的字段允许列表，显式选择可以公开的新参数。不要序列化整个动作对象；Journal 只保存既定元数据，scope 使用非敏感测试 ID，body/header/query/path 不进入记录。
7. 添加真实客户端集成测试，同时断言后端调用次数、其他连接是否受影响和资源回收。更新 [动作表](usage.md)、[场景手册](scenarios.md)、`examples/scenarios.yaml` 与 [验收映射](verification.md)，执行完整 check。目录测试读取真实 YAML；添加规则时同步补齐 `test_example_catalog.py` 的代表请求，确保没有被更早的规则遮蔽。

### 现有参数的完整例子：jitter_seconds

已有 `respond` 步骤可设置 `delay_seconds: 0.1` 和 `jitter_seconds: 0.2`。追踪这个参数即可检查类似扩展是否漏掉某一层：

| 层 | 已有实现 |
| --- | --- |
| 输入 | `Respond.valid_body()` 验证基础延迟与抖动之和不超过 3600；`Delay.bounded_delay()` 对延迟动作执行同一约束 |
| 计划 | `_action_plan()` 将基础值与 jitter 复制到只读 `ActionPlan`，`RulePlan` 保存 probability/seed |
| 决策 | `_decision()` 先决定是否抽中，再由 `delay` 通道计算附加时间；`replace()` 产生本次有效延迟并清零该副本的 jitter |
| 执行 | `_respond()` 等待本次 `delay_seconds`；延迟动作读取本次 `seconds`。Addon 不再次抽样 |
| 诊断 | `/rules` 返回计划的基础值及非零 jitter；explain 和事件日志返回本次有效延迟；Journal 记录实际 action/sampled 与从场景选择到终结的时长 |
| 验证 | `test_sampling.py` 覆盖范围、重放、scope 隔离、概率/延迟通道独立与真实 HTTP；只读快照由 `test_execution_plan.py` 补充 |

## 扩展匹配条件

1. 在 `config.Match` 定义输入、缺省值、规范化和互斥规则，再在 `MatchPlan` / `compile_plan()` 显式复制。需要预编译的表达式在计划构建阶段处理；正则沿用 `matching.py` 的长度限制及非回溯实现。
2. 在 `Engine._match_failures()` 增加匹配维度，使 decide 与 explain 共用一个判定。失败原因只返回维度名称，不回显请求值。保持第一条匹配规则胜出、条件之间 AND，以及未匹配请求不占序号的契约。
3. 检查实时请求和离线输入的等价性：`FaultAddon._request()` 在改写 upstream/Host、移除 scope 头之前匹配；`local_commands.explain_request()` 负责离线解析。若新条件需要额外请求信息，应向 Engine 显式传入必要数据，不让它依赖 HTTPFlow 或网络。方法大小写、去除 query 后的 path、重复 query 值及 header 规范化必须保持一致。CLI 拒绝重复 header 行，普通重复头的测试应传入实时请求合并后的等价值；scope 头实时也禁止重复。
4. 更新 `/rules` 的 match 摘要时单独决定公开字段，禁止自动输出新的敏感匹配值。补充配置、计划不可变性、命中/未命中、优先级、explain 无状态副作用，以及真实 HTTP 与离线解释一致性的回归测试。
5. 同步 schema 所来自的模型、使用文档及可达的 YAML 示例；无需维护另一份静态 JSON Schema。执行对应测试和完整 check。

## 当前边界

单进程、内存状态、无配置热加载、无分布式协调；不提供生产高可用保证。请求/响应完整缓冲，有 body_limit。完整验收仅覆盖 HTTP/1.1，HTTP/HTTPS upstream；没有宣称 HTTP/2/3、WebSocket、SSE、DNS 失败、建连超时、限速、丢包或流式中途截断已实现。后续扩展应新增验收需求和测试，不借用现有普通 HTTP 通过结果证明其他协议正确。

# 开发文档

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
| http1_compat.py | 活跃 Runtime 期间安装并在关闭后恢复的 trailer 拒绝适配 |
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

runner 的 `--help` 与缺少命令提示无需 Docker；实际执行仍全部经 Docker。容器入口将缓存中的 uv/uvx 链接到 `/usr/local/bin`，保证 `sh -lc` 重置 PATH 后仍能找到工具。`tests/test_runner.py` 在容器内用替代 Docker CLI 验证参数原样传递、发布地址、工作目录和失败退出码，不启动嵌套容器。

`fault-engine schema` 直接从 Pydantic 模型输出 JSON Schema，不另存一份需要同步的静态定义。YAML 错误只输出行列；字段错误只保留已知 schema 路径，禁止回显配置值或自定义字典 key。

完整 check 在构建后调用 `.agent/check-wheel.sh`：将锁定生产依赖导出为版本约束，仅以实际 wheel 为安装目标，离线安装到临时虚拟环境，从仓库外执行 schema、init/validate/explain、管理 CLI、HTTP mock 及双向 trailer 拒绝检查。所需依赖由 wheel 元数据决定，缓存由前面的 `uv sync --locked` 准备。这样验证安装包的依赖声明和导入路径，而不只验证 editable 源码环境。

依赖升级使用 `bash .agent/run.sh uv lock --upgrade-package <package>`，随后执行完整 check。runner 的多个容器共享虚拟环境；不要并发修改依赖或格式化同一文件。无需 sudo 在宿主机安装工具。

## 测试策略

- 配置单元测试拒绝模糊、冲突或隐式转换输入。
- 引擎单元测试覆盖序号边界、规则优先级、scope、TTL、容量和 reset。
- transport 使用真实 TCP socket 测试重置、隔离、二进制、半关闭和背压关闭。
- integration 启动真实 mitmproxy 与记录调用的后端，用调用次数区分“注入错误”和“后端已执行”。
- network_edges 覆盖私有 CA TLS、证书拒绝、多服务、HEAD keepalive、取消和在途 reset。
- boundaries 验证非法方法、原始 Host 匹配、控制头移除、容量、日志、大小上限和端口释放。
- CLI 和 demo 测试使用子进程，实际运行 curl 与 SIGTERM；超时断言用有边界的等待，不要求精确毫秒。
- http_review 使用原始 HTTP 字节验证 method 大小写、压缩 body、Latin-1、chunked、Expect、绝对 URL 和 trailer 拒绝。
- lifecycle_review 覆盖并发关闭、调用者取消、慢管理请求、启动回滚和进程全局状态隔离。
- state_review 覆盖错误脱敏、配置字节边界、保留 scope 头、多 scope 并发与 TTL；example_catalog 实际执行仓库中的示例规则。
- maintainability 覆盖管理过滤、摘要脱敏和过期 reset；example_catalog 还验证完整规则目录的可达性，避免宽泛规则遮蔽其他例子。
- regex_safety/execution_plan 覆盖非回溯匹配、语法边界、深层只读快照、累计序列、分页和过期积压；architecture_integration 覆盖真实在途响应与 Admin 使用相同 Plan。
- runtime_rollback/dependency_compat 覆盖重复取消、清理失败重试、依赖版本/API 边界及适配器安装恢复；resource_limits 覆盖慢上传、跨服务配额、拒绝发送超时、复用连接和释放路径。
- local_commands 覆盖生成器不覆盖文件、纯匹配解释与采样一致；journal/journal_integration 覆盖保留窗口、终结、真实 HTTP 与脱敏；admin_client/usability_workflow 覆盖错误响应、真实 CLI 退出码和 503→503→200 重置重跑。

测试后端直接监听端口 0；需要先写入 Config 的端口由 `free_port()` 保证同一测试进程内不重复分配，避免服务/admin 尚未绑定时得到同一端口。该辅助函数不提供跨进程保留保证；监听器测试应在隔离的 runner 网络中执行。

`http1_compat.py` 有意适配 mitmproxy 12.2.3 的私有 HTTP/1 reader 工厂：h11 解析出非空 trailer 时转为协议错误，避免依赖内部抛出未处理的 NotImplementedError。安装前检查依赖版本、工厂签名与 reader 能力，拒绝覆盖其他适配器。没有复制整套 HTTP parser，也不静默丢弃 trailer。依赖升级需要同步包元数据、支持版本表和锁文件，并重跑源码与已安装 wheel 的双向 trailer 测试；上游原生处理修复后应移除此适配。请求侧原生错误路径会先关连接，不能承诺返回 400；响应侧为 502。

采用 superpowers 的设计审批、TDD、根因排查、独立审查和完成前验证。先复现缺失行为或缺陷，再改实现。不要把网络异常全部放宽为“任何 exception”来让测试通过；reset 的原始 socket 断言必须保留。

## 扩展动作

1. 在 config.py 新建严格动作模型，将 `action` 加入带 discriminator 的 Action union；明确允许参数及互斥项。
2. 先添加配置和行为失败测试。Engine 只负责选择动作，一般不需为新动作增加网络分支。
3. 在 addon 对应阶段实现动作。需要 TCP 行为时通过 transport 接口，不能直接访问私有 socket 或把 kill 叫作 reset。
4. 添加真实客户端集成测试，同时断言后端是否收到请求、其他连接是否受影响。
5. 更新使用文档的动作表、场景手册、示例和验收映射，跑整套 check。目录测试会读取真实 YAML；添加正则匹配示例时，在代表路径表补一个可匹配请求。

## 当前边界

单进程、内存状态、无配置热加载、无分布式协调；不提供生产高可用保证。请求/响应完整缓冲，有 body_limit。完整验收仅覆盖 HTTP/1.1，HTTP/HTTPS upstream；没有宣称 HTTP/2/3、WebSocket、SSE、DNS 失败、建连超时、限速、丢包或流式中途截断已实现。后续扩展应新增验收需求和测试，不借用现有普通 HTTP 通过结果证明其他协议正确。

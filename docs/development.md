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

## 源文件职责

| 文件 | 职责 |
| --- | --- |
| config.py | strict pydantic 配置、YAML 重复 key 检查、响应编码与交叉引用 |
| engine.py | 匹配、序号分配、重复/循环选择、TTL/容量、查询与重置 |
| addon.py | mitmproxy HTTP hooks、异步延迟、mock、故障事件 |
| transport.py | 不解析 HTTP 的 TCP 字节中继、地址映射、SO_LINGER RST |
| admin.py | 独立 aiohttp 管理 API、鉴权、输入大小和 reset 校验 |
| runtime.py | 组装、启动、异常回滚和关闭 |
| http1_compat.py | 活跃 Runtime 期间安装并在关闭后恢复的 trailer 拒绝适配 |
| cli.py / __main__.py | validate / serve、token 环境变量、信号处理与退出码 |

## 状态与并发不变量

`Engine.decide()` 没有 await；同一 asyncio event loop 内匹配和递增为一个同步步骤。不要从其他线程调用 Engine，也不要在计数分配前插入 await。异步动作只在 Decision 已生成后执行。Decision 为冻结 dataclass，运行期间不修改配置。

状态键为 `(service, rule, scope)`。OrderedDict 按最后命中排序，先移除过期键，再检查容量；不会为新 scope 静默踢掉活跃场景。reset 删除计数，不修改已经选中的 Decision。尚未命中规则的请求不占用状态容量。

`snapshot()` 与 `reset()` 共用 `_matching_keys()`，先清理过期条目再执行 service/rule/scope 的 AND 精确过滤；读取不会刷新最后命中时间。管理 `/state` 拒绝未知或重复 query 参数，避免拼写错误被当成无过滤查询。`/rules` 通过字段允许列表输出动作摘要；新增动作时显式决定哪些参数可公开，不要直接序列化完整规则。

规则与动作 pydantic 对象禁止字段赋值，但其 list/dict 不作深层冻结；它们是启动后只读的内部数据，不提供热修改 API。扩展时如引入动态配置，应增加不可变快照及版本化状态迁移，不要直接修改列表。

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

Python 固定 3.12，mitmproxy 限定 12 系列并由 uv.lock 固定实际版本。runner 引导 uv 0.12.17。`.venv-docker` 不能在 macOS 宿主机运行。ruff/ty 排除该第三方虚拟环境，ty 检查 src、tests 和 examples。

runner 的 `--help` 与缺少命令提示无需 Docker；实际执行仍全部经 Docker。容器入口将缓存中的 uv/uvx 链接到 `/usr/local/bin`，保证 `sh -lc` 重置 PATH 后仍能找到工具。`tests/test_runner.py` 在容器内用替代 Docker CLI 验证参数原样传递、发布地址、工作目录和失败退出码，不启动嵌套容器。

`fault-engine schema` 直接从 Pydantic 模型输出 JSON Schema，不另存一份需要同步的静态定义。YAML 错误只输出行列；字段错误只保留已知 schema 路径，禁止回显配置值或自定义字典 key。

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

`http1_compat.py` 有意适配 mitmproxy 12.2.3 的私有 HTTP/1 reader 工厂：h11 解析出非空 trailer 时转为协议错误，避免依赖内部抛出未处理的 NotImplementedError。没有复制整套 HTTP parser，也不静默丢弃 trailer。依赖升级必须重跑请求和响应两个方向的真实字节测试；上游原生处理修复后应移除此适配。请求侧原生错误路径会先关连接，不能承诺返回 400；响应侧为 502。

采用 superpowers 的设计审批、TDD、根因排查、独立审查和完成前验证。先复现缺失行为或缺陷，再改实现。不要把网络异常全部放宽为“任何 exception”来让测试通过；reset 的原始 socket 断言必须保留。

## 扩展动作

1. 在 config.py 新建严格动作模型，将 `action` 加入带 discriminator 的 Action union；明确允许参数及互斥项。
2. 先添加配置和行为失败测试。Engine 只负责选择动作，一般不需为新动作增加网络分支。
3. 在 addon 对应阶段实现动作。需要 TCP 行为时通过 transport 接口，不能直接访问私有 socket 或把 kill 叫作 reset。
4. 添加真实客户端集成测试，同时断言后端是否收到请求、其他连接是否受影响。
5. 更新使用文档的动作表、场景手册、示例和验收映射，跑整套 check。目录测试会读取真实 YAML；添加正则匹配示例时，在代表路径表补一个可匹配请求。

## 当前边界

单进程、内存状态、无配置热加载、无分布式协调；不提供生产高可用保证。请求/响应完整缓冲，有 body_limit。完整验收仅覆盖 HTTP/1.1，HTTP/HTTPS upstream；没有宣称 HTTP/2/3、WebSocket、SSE、DNS 失败、建连超时、限速、丢包或流式中途截断已实现。后续扩展应新增验收需求和测试，不借用现有普通 HTTP 通过结果证明其他协议正确。

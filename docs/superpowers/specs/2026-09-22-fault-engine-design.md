# HTTP 故障场景引擎设计

状态：用户已批准，已实现并完成验收。证据、实际网络边界和测试结果见 `docs/verification.md`。

## 目标与交付

基于 mitmproxy 为开发和自动化测试提供可复现的反向代理故障注入，验证客户端 retry、timeout、error handling 和写入幂等性。交付 Python 程序、中文使用文档、中文开发文档、示例配置、自动化测试和可复现的质量检查命令。

工具链：Python 3.12、uv（依赖与锁文件）、ruff（格式和 lint）、ty（静态类型检查）、pytest。实际依赖兼容性须在 Docker 中解析验证，不以此设计代替验证。所有项目执行通过 `.agent/run.sh`，使用 `python:3.12-bookworm`；宿主机只读取、检查和编辑文件。

## 方案选择

1. 推荐：mitmproxy addon + 独立规则引擎 + 小型连接故障适配层。HTTP 解析和转发复用 mitmproxy，规则与状态可独立测试，精确 TCP reset 由可验证的传输层能力实现。
2. 仅使用 addon：实现较小，但终止 HTTP flow 不能直接证明客户端收到 TCP RST，无法满足精确连接重置的验收。
3. 自建完整 HTTP 代理：传输控制自由，但需要重新承担 TLS、协议和转发正确性的维护成本，与已选技术方向不符。

## 路由与 HTTP 行为

- 一个配置文件声明多个服务，每个服务有独立监听端口和固定 HTTP/HTTPS upstream。客户端仅需更改 base URL，保留原始路径和 query。
- 支持 GET、POST、PUT、PATCH、DELETE、HEAD、OPTIONS、TRACE 以及合法自定义 method。CONNECT 属于隧道建立，不当成普通反向代理业务请求；非法 method 拒绝。
- 正常转发保留请求体字节、查询参数和业务头；Host 遵循 upstream，测试控制头默认移除。支持二进制请求体与重复 query。
- 无规则命中时透传；规则按声明顺序 first-match-wins，不叠加、不偷偷重试。
- HTTP/1.1 为所有故障动作的完整验收基线；HTTP/HTTPS upstream 必须测试。HTTP/2、WebSocket、流式中途故障的能力必须单独标注，不以普通 HTTP 测试声称全覆盖。

## 配置与场景

使用严格校验的 YAML。未知字段、重复 ID、未知服务、无效状态码、负延迟、空序列、无效正则和不支持的动作组合都在启动前报错。

规则包含 id、service、match、scope、start_at、sequence、after_sequence。match 支持 method 集合、精确 path 或 path regex、headers、query；path 不包含 query，header 名称大小写不敏感。省略 methods 表示所有普通 HTTP 方法。

计数键为 `(service_id, rule_id, scope_value)`，scope 可为显式 global 或请求头，例如 `X-Test-Run-ID`。要求请求头但缺失时返回清晰的配置约定错误，不悄悄合并到全局。scope 同时可用于一次测试或一次逻辑调用，客户端重试必须复用相同值。

匹配后先原子分配序号，再执行任何异步等待。序号从 1 开始；start_at 之前透传，start_at 对应 sequence 的第一步。步骤可声明 repeat 正整数；序列结束策略支持 passthrough、repeat_last、cycle。默认 passthrough。

例如 start_at=5、sequence=[429,429,passthrough] 表示第 1–4 次透传，第 5–6 次 429，第 7 次及以后透传。第三次固定成功必须配置 respond 200；passthrough 只保证访问真实后端，不保证返回 200。

状态在单进程内存中保存；重启清空，不承诺多实例共享。状态条目设置容量和闲置 TTL，淘汰/容量耗尽的行为必须显式记录，避免静默重新开始场景。重置影响后续请求，已分配动作的在途请求保持原决策。

## 故障动作

| 动作 | 契约 |
| --- | --- |
| passthrough | 原样访问 upstream |
| respond | 不访问 upstream，返回配置的最终 HTTP 状态、headers、文本/JSON/base64 body；支持 4xx、5xx 和 mock 成功 |
| delay_before | 在转发前异步等待配置时长，再转发；不阻塞其他请求 |
| delay_after | upstream 已完成响应后延迟交付，验证写入已发生但客户端超时的情况 |
| timeout | 不访问 upstream，保持不响应直到有界 hold 时间，到期终止；hold 必须大于待测客户端 timeout |
| disconnect | 终止响应，不承诺具体 errno 或 TCP RST |
| reset | 对客户端 TCP 连接执行可观测的 RST；须用真实 Linux socket 验证，不能用 flow.kill 或 HTTP 500 冒充 |

HTTP 104 与 Linux errno 104 不同。本需求按 `ECONNRESET`（Linux 常见 errno 104）理解。其他操作系统或 HTTP 客户端可能包装成不同异常名称或编号。

精确 reset 先做技术验证：如果 mitmproxy 公共 addon API 无法直接控制客户端 socket，则使用程序持有客户端 socket 的本地 TCP 前置层，按连接映射接受 addon 的 reset 指令并通过 SO_LINGER 的 abortive close 产生 RST。它只负责字节中继和连接故障，不重新实现 HTTP 解析。内部 mitmproxy 端口仅绑定 loopback，连接映射无效必须显式失败。TCP reset 会影响连接内所有请求，包括 HTTP/2 多路复用，不能声称只影响一个 stream。实现以端到端证据确定兼容范围。

HEAD、204、304 的响应体遵守 HTTP 语义；暂不将 1xx 当作独立最终响应。连接建立超时、DNS 失败和带宽/丢包不等同于这里的响应超时；文档须解释差别，不能宣称已实现未验证的网络故障。

## 控制与可观测性

CLI 提供配置校验和启动。独立管理监听端口提供 health、规则摘要、计数查询、按 service/rule/scope 重置；使用 token 保护管理写操作，默认只绑定 loopback。管理请求不计入业务规则。Docker 对宿主机暴露管理端口时只绑定宿主机 loopback。

结构化事件包含时间、请求 ID、服务、规则、scope、序号、动作、阶段、结果；不默认记录 Authorization、Cookie 或请求体。scope 的日志表达须限制长度。CLI 错误应有非零退出码和具体字段路径。关闭服务取消延迟任务、清理连接和监听端口。

## 组件边界

- config：解析和验证配置，不依赖 mitmproxy。
- engine：匹配、计数、动作选择、reset、状态生命周期，不执行网络 I/O。
- addon：将 HTTP flow 映射为引擎输入，执行 HTTP 动作，记录完成/失败。
- transport：连接映射、中继和精确 reset，隔离非 HTTP 能力。
- admin：鉴权、健康检查、状态查询和重置。
- cli：配置加载、生命周期和多服务启动。

## 验收与测试

采用 superpowers TDD：先写行为测试并观察预期失败，再实现；修复缺陷先添加回归测试。

1. 单元测试覆盖规则优先级、全部匹配字段、大小写、n 次边界、repeat/cycle/repeat_last、scope 隔离、重置、TTL/容量、非法配置；并发分配序号不得重复或越界。
2. 集成测试启动真实 mitmproxy 和记录调用次数的 upstream，参数化验证上述 HTTP methods、二进制 body、重复 query、响应 headers、多个服务，以及 HTTP/HTTPS upstream。
3. 验证连续两次 429 后透传的真实客户端重试：动作序号为 1/2/3，upstream 只收到一次请求；用 mock 200 单独验证固定成功。
4. 验证 delay_before、delay_after、timeout 的客户端异常与 upstream 调用次数，验证慢请求不阻塞无关请求。计时测试使用合理上下界，避免依赖精确毫秒。
5. 使用原始 Linux TCP socket 验证 reset 导致 ECONNRESET；另用 HTTP 客户端验证异常包装。disconnect 与 reset 分别验收。并发连接测试确保 reset 不误杀其他连接。
6. 管理鉴权、reset 隔离、错误请求、服务关闭与端口释放必须测试。HEAD、204、304、上游失败和客户端提前断开必须有回归测试。
7. 完成门槛：`uv lock --check`、`uv run ruff format --check .`、`uv run ruff check .`、`uv run ty check`、完整 pytest 和构建命令通过；以上均在 Docker runner 内执行。
8. 使用文档包含真实执行过的启动、配置校验、curl、重置和测试命令；开发文档包含模块结构、动作契约、并发语义、扩展步骤及已知限制。测试结果必须区分已验证与未验证能力。

## 实施顺序

确认设计后：建立 Docker runner 与 uv 项目；验证 TCP reset 技术路径；测试驱动实现配置和引擎；实现 addon/多服务/管理接口；跑真实端到端测试；完成中文文档和示例；运行完整质量门槛并按需求逐项审计。不因某个动作难以实现而把它悄悄删除或改名为较弱行为。

## 参考

- https://docs.mitmproxy.org/stable/concepts/modes/
- https://docs.mitmproxy.org/stable/addons/examples/
- https://docs.mitmproxy.org/stable/api/mitmproxy/flow.html
- https://docs.mitmproxy.org/stable/api/events.html

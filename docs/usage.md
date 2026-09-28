# 使用文档

## 1. 运行方式

所有执行命令使用 `bash .agent/run.sh ...`。runner 将本项目挂载到容器 `/workspace`，使用 Python 3.12，自动引导固定版本 uv；虚拟环境放在 `.venv-docker`，uv 下载缓存使用 Docker volume。依赖由 `uv.lock` 锁定。

```bash
bash .agent/run.sh uv sync --locked
bash .agent/run.sh uv run fault-engine --help
bash .agent/run.sh uv run fault-engine validate examples/scenarios.yaml
bash .agent/run.sh uv run fault-engine schema
```

`validate` 不启动网络服务，也不要求管理 token。字段错误返回退出码 2 并指出配置路径；YAML 语法、重复键和非字符串键错误给出从 1 开始的行列位置，不回显输入内容。未知动作提示列出可选动作。

`schema` 无需配置文件或 token，直接输出从当前模型生成的 JSON Schema，可供编辑器或其他工具使用；服务引用、端口冲突等跨字段规则仍以 `validate` 为准。需要保存时将重定向放在容器内：

```bash
bash .agent/run.sh sh -lc 'uv run fault-engine schema > /tmp/fault-engine.schema.json && cat /tmp/fault-engine.schema.json'
```

上面的 `/tmp` 文件随容器退出删除；持久保存可将路径改为 `/workspace/` 下的自选文件。runner 无参数时显示用法并返回 2，`--help` 不需要 Docker；普通项目命令需要 Docker/OrbStack 正在运行。

### 完整演示

```bash
bash .agent/run.sh --publish uv run python examples/demo.py
```

演示在同一个容器中启动 orders 后端（9000）、inventory 后端（9001）、两个代理及管理 API。后端返回 method、path、body_base64、服务名和累计 upstream_calls，方便确认失败请求是否真的访问了后端。演示 token 固定为 `local-demo-token`，仅用于本地演示。

| 服务 | 容器内 | 宿主机 |
| --- | --- | --- |
| orders | 8080 | 127.0.0.1:18080 |
| inventory | 8081 | 127.0.0.1:18081 |
| 管理 | 9090 | 127.0.0.1:19090 |

runner 不带 `--publish` 时不发布任何端口，适用于测试。端口映射固定在 runner 中；需要其他端口时同时调整 runner 与 YAML。示例 YAML 的 `host: 0.0.0.0` 用于容器内接受映射流量；默认配置值为 `127.0.0.1`。管理端口对宿主机始终只绑定 loopback。

### 接入真实后端

复制并编辑场景 YAML，将 `services[*].upstream` 改成真实服务的 origin。容器内的 `127.0.0.1` 指容器自身。在 Docker Desktop/OrbStack 中访问宿主机后端可使用 `http://host.docker.internal:9000`；Linux 原生 Docker 的宿主机地址需要按其网络配置填写。

```bash
bash .agent/run.sh --publish sh -c 'export FAULT_ADMIN_TOKEN="replace-with-your-local-token"; uv run fault-engine serve examples/scenarios.yaml'
```

此命令只启动代理，不启动演示后端。token 从 YAML 的 `admin.token_env` 指定的环境变量读取，必须是非空、无空格的可打印 ASCII。不要把实际 token 提交到仓库。输出 `event: ready` 后才表示全部监听器已就绪。SIGINT/SIGTERM 会关闭监听器与在途任务。

## 2. 配置结构

```yaml
version: 1
services:
  - id: orders
    host: 0.0.0.0
    port: 8080
    upstream: http://host.docker.internal:9000
admin:
  host: 0.0.0.0
  port: 9090
  token_env: FAULT_ADMIN_TOKEN
state:
  capacity: 10000
  ttl_seconds: 3600
limits:
  max_connections: 256
  max_inflight_requests: 128
  state_page_size: 1000
body_limit: 10485760
rules:
  - id: retry-payment
    service: orders
    match:
      methods: [POST, PUT]
      path: /payments
      headers: {X-Client: mobile}
      query: {mode: test}
    scope: X-Test-Run-ID
    start_at: 1
    sequence:
      - action: respond
        status: 429
        repeat: 2
        headers: {Retry-After: '1'}
        json_body: {error: retry_later}
      - action: passthrough
    after_sequence: passthrough
```

服务、规则 ID 必须唯一，使用 1–64 位字母、数字、点、下划线或短横线，第一位为字母或数字。服务与管理端口必须不同。`upstream` 只接受 HTTP/HTTPS origin，不接受路径前缀、query、fragment 或用户名密码。路径和 query 原样转发，Host 改写为 upstream；绝对重定向 URL 不自动重写。

HTTPS upstream 默认验证证书，系统信任根可直接使用；私有 CA 可配置 `upstream_ca: /workspace/certs/ca.pem`。CA 路径在容器中解析。当前未提供绕过证书校验的配置。

配置不热加载；修改后重启。文件使用 UTF-8，最大 1 MiB（按字节计）。未知字段、重复 YAML key、无效引用和不合法的动作组合会在启动前拒绝。version 只接受整数 1。

创建 Runtime 时，配置会编译成独立的只读执行计划。嵌入调用方之后修改原始 Config 的列表或字典，不会改变正在执行的请求、后续规则或管理接口；应用新配置需要创建新的运行实例。

### 资源限额

| 字段 | 默认值 | 行为 |
| --- | --- | --- |
| `limits.max_connections` | 256 | 所有服务共享的公开 TCP 连接上限，包含空闲 keepalive；超限连接立即关闭 |
| `limits.max_inflight_requests` | 128 | 从收到请求头开始，覆盖上传、上游响应缓冲和故障延迟，直到响应 hook 完成或请求终止 |
| `limits.state_page_size` | 1000 | `/state` 默认及最大单页条数，查询可通过 limit 选择更小的页 |

均为正整数；连接/请求上限最大 100000，单页最大 10000。管理监听器独立于上述数据面配额，因此代理繁忙时仍可查询和重置。`state.capacity` 限制计数条目，`body_limit` 限制单条请求/响应大小；它们与连接及在途请求限额分别生效。

在途请求超限时，新连接的首个请求无需上传完整 body 就收到 **503 + `X-Fault-Engine-Error: capacity` + `Connection: close`**；HEAD 不返回错误 body。已经处理过请求的 keepalive/管道连接会直接关闭并记录 `capacity_exhausted`，避免把 503 拼进上一条未发送完的响应。被拒请求不访问后端、不消耗场景序号。

拒绝响应的发送最多等待 0.5 秒；客户端不读或发生 I/O 故障时直接关闭，不能保证收到完整 503。正常关闭、取消、reset、断连都会归还配额。配额不代表所有缓冲区的精确内存总量限制，慢客户端仍会占用连接配额。

## 3. 匹配和计数

- 规则按配置顺序匹配，**第一条命中生效**，不叠加其他规则；未命中透传且不计数。
- `match` 的字段为 AND 关系。省略 match 表示匹配服务的全部普通 HTTP 请求。
- `methods` 是区分大小写的 HTTP method 列表；省略即全部方法。CONNECT 隧道不支持，非法 method 拒绝。
- `path` 精确匹配原始路径，不含 query，不额外 URL 解码。与 `path_regex` 互斥；正则匹配整个路径。
- header 名称大小写不敏感，值精确匹配；匹配看到的是客户端原始 Host。重复普通 header 按 mitmproxy 的合并值匹配。
- query 名称/值经过 URL 解码，重复 key 中只要有一个值满足配置即可。
- `scope: global` 是默认值，所有匹配该规则的请求共享计数。其他值代表请求头名，例如 `X-Test-Run-ID`。
- 计数键为 `(服务, 规则, scope值)`。同一逻辑调用的重试使用相同 scope；并发测试或不同逻辑调用使用不同 scope。
- scope 头缺失、为空或超过 256 字符返回 **400 + `X-Fault-Engine-Error: scenario`**，不消耗次数。它与刻意模拟的业务错误不同。
- 配置的 scope 头在同一请求中只能出现一次；重复头返回 400，不消耗次数，避免合并后的值与另一个测试 ID 冲突。
- 所有配置中的 scope 控制头默认在转发前删除，包括未匹配请求。禁止使用 Authorization/Cookie/Host 等保留头作为 scope。

序号从 1 开始，先分配再执行延迟。并发请求按进入引擎的顺序分配，不保证按响应完成顺序递增。请求取消仍消耗已分配序号。

`path_regex` 最长 4096 字符，使用非回溯的 Rust regex 引擎；环视、反向引用及不兼容语法在 `validate` 时拒绝，错误信息不会回显表达式。原来依赖这些 Python `re` 特性的配置需要改写；路径、方法、header/query 组合通常可以表达相同的测试条件。引擎仍按同步步骤分配计数，不把危险匹配丢入无法取消的后台线程。语法依据见 [Pydantic regex engine](https://pydantic.dev/docs/validation/latest/api/pydantic/config/#regex_engine)。

`start_at: 5` 与一个 `repeat: 2` 的 503 步骤表示：1–4 次透传，5–6 次 503，随后执行结束策略。`repeat` 是该步骤占据的请求数，不是在代理内部重试。

| after_sequence | 序列结束后 |
| --- | --- |
| passthrough（默认） | 正常访问 upstream |
| repeat_last | 永久重复最后一个动作 |
| cycle | 从 sequence 第一个动作循环，不重新执行 start_at 的前置透传 |

**passthrough 不保证成功**，真实后端仍可能返回错误。要保证第三次固定 200，使用 `respond(status=200)`。

计数保存在单进程内存中，重启清空，不在多个副本之间共享。TTL 为自最后一次命中起的闲置时间；过期时移除并记录 `state_expired`，下次该 scope 从 1 开始。容量满时不会淘汰活跃测试，而是返回带 scenario 标记的 400 并记录 `state_capacity`。请把 TTL 设置得大于一次完整重试测试的持续时间。

## 4. 故障动作

### 可复现的比例与抖动

规则可设置 `probability`（0–1，默认 1）和 `seed`（0–2147483647 的整数，默认 0）。先按匹配及次数选择步骤，再决定执行；未抽中的请求透传，仍消耗该规则序号，不再尝试后续规则。0 全部透传，1 保持旧行为。有限请求中的比例不保证精确等于配置值；精确“两次失败一次成功”仍用 sequence/cycle。

`respond`、`respond_after`、`delay_before`、`delay_after`、`timeout` 可设置 `jitter_seconds`（默认 0）。实际等待为基础秒数加上 `[0, jitter_seconds)` 的均匀抖动；基础值与抖动上限之和不得超过 3600 秒。基础值分别取 delay_seconds 或 seconds；其他动作不接受 jitter。偶发长延迟可组合低 probability 与较大 seconds，尚无 lognormal 分布。

```yaml
- id: flaky-api
  service: orders
  match: {path: /flaky}
  scope: X-Test-Run-ID
  probability: 0.25
  seed: 42
  sequence:
    - action: respond
      status: 503
      delay_seconds: 0.05
      jitter_seconds: 0.15
  after_sequence: repeat_last
```

约 25% 的匹配请求等待 50–200 ms 后返回 503，其余访问后端。seed、服务、规则 ID、scope 和序号共同决定样本；同 scope reset、TTL 过期或重启后重放相同序列。其他 scope 的请求顺序不影响它；同 scope 内并发仍按进入引擎的顺序分配，重排可能改变某个业务请求对应的故障。改变 seed 或 scope 会改变样本。

概率与延迟使用不同通道，修改 probability 不改变相同序号被选中时的等待值。`/rules` 返回 probability、seed 和配置的 jitter_seconds；日志新增 sampled 与实际 delay_seconds。sampled 仅表示通过概率门控，start_at 前、passthrough 步骤或序列结束后仍可能透传。在途实际动作已经固定，reset 不会改变它。

每个动作可带 `repeat`（正整数，默认 1）。

| action | 参数 | 行为 |
| --- | --- | --- |
| passthrough | 无 | 访问真实后端 |
| respond | status、可选 headers/body/json_body/body_base64、delay_seconds | 不访问后端，生成最终 HTTP 响应 |
| respond_after | 与 respond 相同 | 收到完整后端响应后，将其替换为配置的响应 |
| delay_before | seconds | 转发前异步等待，然后正常转发 |
| delay_after | seconds | 收到完整后端响应后，再延迟交付客户端 |
| timeout | seconds | 不访问后端，在有限时间内不响应，时间到后终止请求 |
| disconnect | 无 | 终止当前 flow，不承诺特定 TCP 标志或 errno |
| reset | 无 | 对该客户端 TCP 连接发送真实 RST |
| disconnect_after | 无 | 收到完整后端响应后终止 flow |
| reset_after | 无 | 收到完整后端响应后，对客户端连接发送真实 RST |

`seconds` 必须大于 0 且不超过 3600。测试客户端读超时时，让 seconds 明显大于客户端 timeout。delay_before 到期后如果客户端已断开，不承诺仍会写入后端；验证“后端已成功而客户端超时”应使用 delay_after。

`respond.status` 接受 200–599；1xx 是中间响应，不能作为该动作的最终响应。文本使用 UTF-8，json_body 自动生成 JSON Content-Type，body_base64 用于二进制。三种 body 字段互斥；`json_body: null` 表示 JSON null。headers 值须为字符串，例如 `Retry-After: '1'`。Content-Length 等分帧头由程序管理，不能手动配置。204/205/304 不允许非空 body，HEAD 不发送 body。

`respond` 和 `respond_after` 的 `delay_seconds` 默认为 0，允许 0–3600；等待期间不阻塞其他请求。响应头按 Latin-1 编码，body 为实际发送的字节，不根据 Content-Encoding 自动压缩。模拟 gzip 时，将已压缩内容放入 body_base64，并配置 `Content-Encoding: gzip`；也可以故意提供错误编码，测试客户端解码失败。

后置动作适合验证写入幂等性：例如 POST 已到达后端，但客户端只看到 503 或断连，再重试可能造成第二次写入。它们只在收到完整 HTTP 响应后触发；如果上游连接/TLS 失败或返回不完整响应，保留真实代理错误，不执行替换或后置 reset。是否真正完成业务写入仍需检查后端记录，HTTP 响应本身不是业务提交证明。

### 可直接运行的客户端场景

完整演示自动加载 `examples/scenarios.yaml` 的 30 条规则。[场景手册](scenarios.md)集中维护路径、规则 ID、匹配前提、scope 要求和预期结果，另附定向故障、写入重试和循环恢复命令。

引擎只制造场景，不自动判定客户端重试策略是否符合业务要求。演示后端返回的累计调用次数包含其他请求，比较前请记录基线。

### 超时、504 和 errno 104

- 返回 HTTP 504：客户端正常收到一份错误响应，测试的是状态码处理。
- `timeout` 或足够长的 delay：客户端等待响应超时，测试的是读超时处理。
- `reset`：TCP 被重置。Linux 原始 socket 的错误为 `ECONNRESET=104`；httpx 等库可能包装成 `ReadError`/其他 transport error，其他系统的 errno 也可能不同。

RST 作用于整个 TCP 连接，不能承诺只影响一个复用的请求。测试时应关注客户端异常分类而非跨平台固定 errno。连接建立超时、DNS 错误、丢包和带宽控制没有用响应延迟冒充。

**额外转发层会影响客户端所见错误。** 本项目在 Linux 容器内直接连接时已测得 errno 104；在当前 OrbStack 环境，经 `host.docker.internal:18080` 的宿主机端口映射访问时，实际 curl 返回 52（Empty reply），不是 56（reset 接收错误）。不能用这一映射路径保证精确 104。若验收要求精确 RST，客户端应直接连接引擎所在容器的监听器，避免中间 TCP 代理；可运行下面的直接连接验收：

```bash
bash .agent/run.sh uv run pytest tests/test_integration.py::test_real_reset_and_unrelated_connection_survives -q
```

### 演示调用

以下命令从另一个 runner 容器访问已发布的演示端口：

```bash
# 首次使用 usage-demo：429、429、200；重跑前换 ID 或 reset
bash .agent/run.sh sh -c 'for i in 1 2 3; do curl -sS -o /dev/null -w "%{http_code}\n" -H "X-Test-Run-ID: usage-demo" http://host.docker.internal:18080/retry; done'

# 超时；curl 非零退出是预期结果
bash .agent/run.sh curl --max-time 1 http://host.docker.internal:18080/timeout

# 后端先执行 POST，客户端等待响应超时
bash .agent/run.sh curl --max-time 1 -X POST -d 'payment=1' http://host.docker.internal:18080/slow-after

# 引擎发送 RST；具体 curl 错误受额外端口转发层影响，见上文
bash .agent/run.sh curl -v http://host.docker.internal:18080/reset
```

测试规则不代表推荐的客户端重试政策。例如大部分 4xx 不适合自动重试，但可以用来确认客户端确实停止重试；429 和可重试的 5xx 可用来验证次数、退避以及 Retry-After。

## 5. 管理 API

独立端口，不经过故障规则。`GET /health` 公开，其余接口均要求 `Authorization: Bearer <token>`。没有提供跨域配置或浏览器 UI。

| 接口 | 响应 |
| --- | --- |
| GET /health | `{"status":"ok"}` |
| GET /rules | 规则 ID、服务、scope、起始序号、匹配摘要和动作参数，不暴露 mock body 或 header/query 值 |
| GET /state | 可选 service/rule/scope、limit/cursor；响应包含 counters，有后续页时另含 next_cursor |
| POST /reset | JSON 可选 service/rule/scope，返回实际删除的计数条目数 |

```bash
bash .agent/run.sh curl -sS -H 'Authorization: Bearer local-demo-token' http://host.docker.internal:19090/state
bash .agent/run.sh curl -sS --get -H 'Authorization: Bearer local-demo-token' --data-urlencode 'service=orders' --data-urlencode 'scope=demo' http://host.docker.internal:19090/state
bash .agent/run.sh curl -sS -X POST -H 'Authorization: Bearer local-demo-token' -H 'Content-Type: application/json' -d '{"service":"orders","rule":"retry-twice","scope":"demo"}' http://host.docker.internal:19090/reset
```

`/state` 的过滤条件使用 AND 精确匹配，不传参数返回第一页有效计数；未知或重复参数返回 400，空过滤值不会扩大选择，查询不会延长 TTL。scope 含空格、`&` 或其他特殊字符时使用 `--data-urlencode`。

`limit` 必须为 1 到 `limits.state_page_size` 的十进制整数，省略时使用上限。响应没有 `next_cursor` 就是最后一页；有该字段时，将它原样作为下一次查询的 `cursor`，保持 service/rule/scope 不变。分页按条目创建顺序，不因后续命中而移动；游标绑定当前 Engine 实例和过滤条件，重启后失效。查询不是事务快照：首次查询之后创建或重建的计数条目不进入本轮，已有计数会更新，已 reset/过期条目会消失。

每页扫描量也有上限，因此即使 counters 为空，只要仍有 next_cursor 就应继续。旧客户端若只读取一次 `/state`，需要改为持续翻页；默认 1000 条以内且无需继续扫描的结果仍只有 counters 字段。

```bash
bash .agent/run.sh curl -sS --get -H 'Authorization: Bearer local-demo-token' --data-urlencode 'service=orders' --data-urlencode 'limit=100' http://host.docker.internal:19090/state
# 有下一页时，保留上面的过滤条件并添加 --data-urlencode 'cursor=响应中的next_cursor'
```

`/rules` 保留原来的 `actions` 名称数组，另提供 `sequence`：每步包含 action/repeat，以及适用的 status/seconds/delay_seconds。`match` 提供 methods/path/path_regex、header_names/query_names；匹配值以本地 YAML 为准。

`{}` 重置全部。filter 不匹配返回 `{"reset":0}`；过期条目不计入 reset 数量，其物理删除分批进行。错误 JSON、未知字段返回 400，未经认证返回 401，超过 4 KiB 的管理请求体返回 413。重置只影响之后的分配，在途请求保留已选动作。

## 6. 排查

事件日志包含时间、request_id、服务、规则、scope（最多 64 字符）、序号、动作、阶段和结果。默认不记录请求体、Authorization 或 Cookie。scope 不应放入秘密数据。

真实上游连接或 TLS 校验失败通常由 mitmproxy 返回 502；这与 respond 生成的 502 不同，可用选中动作日志和后端调用次数区分。场景配置约定错误携带 `X-Fault-Engine-Error: scenario`。

当前缓冲请求/响应体，上限默认为 10 MiB；不保证流式/SSE 或无限响应。HTTP/2 被禁用，以保持连接级故障可复现。当前客户端入口只验证 HTTP/1.1 明文访问；HTTPS 是指代理到后端的链路。

当前不支持非空 HTTP trailers：带 trailer 的请求会立即关闭连接且不访问后端；后端响应带 trailer 则返回 502。普通 chunked 请求、chunk extension 和 100-continue 已验证。这个明确拒绝行为避免当前 mitmproxy 版本在 trailer 解析后异常并让客户端一直等待。

| 现象 | 检查与处理 |
| --- | --- |
| runner 提示 Docker CLI 不存在或无法连接 daemon | 安装并启动 Docker/OrbStack；不要改为宿主机运行项目 |
| 端口已占用 | 停止旧演示；修改端口时同时调整 YAML 与 runner 的发布映射 |
| `serve` 提示缺少 token | 在 runner 的容器内设置 `admin.token_env` 指定的变量；宿主机环境变量不会自动透传 |
| 配置通过但返回真实后端结果 | 检查服务端口、method 大小写、path、header/query 和第一条命中规则；看 `/rules` 的匹配摘要 |
| 返回 400 且有 scenario 标记 | 检查 scope 头是否缺失、重复或过长；容量问题可按 scope reset，或等待闲置 TTL |
| 返回 503 且有 capacity 标记，或新连接立即关闭 | 检查连接/在途请求限额、过长延迟和未关闭的 keepalive；减少并发或调整 limits |
| 重跑没有重新报错 | 同 scope 保留计数；换新 ID 或精确 reset；`global` 规则需要按规则重置 |
| 容器访问宿主机后端返回 502 | 检查 upstream 是否误写容器自身 `127.0.0.1`，以及 TLS CA 和后端是否监听可达地址 |
| `/state` 查不到计数 | 未命中规则、过滤值不符、TTL 已过期或进程重启都会导致空结果 |
| `/state` 第一页不含目标 scope | 有 next_cursor 时继续翻页；即使该页 counters 为空也不要提前结束 |

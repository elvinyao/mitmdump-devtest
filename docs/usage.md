# 使用文档

## 1. 运行方式

所有执行命令使用 `bash .agent/run.sh ...`。runner 将本项目挂载到容器 `/workspace`，使用 Python 3.12，自动引导固定版本 uv；虚拟环境放在 `.venv-docker`，uv 下载缓存使用 Docker volume。依赖由 `uv.lock` 锁定。

```bash
bash .agent/run.sh uv sync --locked
bash .agent/run.sh uv run fault-engine --help
bash .agent/run.sh uv run fault-engine validate examples/scenarios.yaml
```

`validate` 不启动网络服务，也不要求管理 token。字段错误返回非零退出码并指出配置路径。

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

配置不热加载；修改后重启。未知字段、重复 YAML key、无效引用和不合法的动作组合会在启动前拒绝。

## 3. 匹配和计数

- 规则按配置顺序匹配，**第一条命中生效**，不叠加其他规则；未命中透传且不计数。
- `match` 的字段为 AND 关系。省略 match 表示匹配服务的全部普通 HTTP 请求。
- `methods` 是区分大小写的 HTTP method 列表；省略即全部方法。CONNECT 隧道不支持，非法 method 拒绝。
- `path` 精确匹配原始路径，不含 query，不额外 URL 解码。与 `path_regex` 互斥；正则使用 fullmatch，匹配整个路径。
- header 名称大小写不敏感，值精确匹配；匹配看到的是客户端原始 Host。重复普通 header 按 mitmproxy 的合并值匹配。
- query 名称/值经过 URL 解码，重复 key 中只要有一个值满足配置即可。
- `scope: global` 是默认值，所有匹配该规则的请求共享计数。其他值代表请求头名，例如 `X-Test-Run-ID`。
- 计数键为 `(服务, 规则, scope值)`。同一逻辑调用的重试使用相同 scope；并发测试或不同逻辑调用使用不同 scope。
- scope 头缺失、为空或超过 256 字符返回 **400 + `X-Fault-Engine-Error: scenario`**，不消耗次数。它与刻意模拟的业务错误不同。
- 所有配置中的 scope 控制头默认在转发前删除，包括未匹配请求。禁止使用 Authorization/Cookie/Host 等保留头作为 scope。

序号从 1 开始，先分配再执行延迟。并发请求按进入引擎的顺序分配，不保证按响应完成顺序递增。请求取消仍消耗已分配序号。

`start_at: 5` 与一个 `repeat: 2` 的 503 步骤表示：1–4 次透传，5–6 次 503，随后执行结束策略。`repeat` 是该步骤占据的请求数，不是在代理内部重试。

| after_sequence | 序列结束后 |
| --- | --- |
| passthrough（默认） | 正常访问 upstream |
| repeat_last | 永久重复最后一个动作 |
| cycle | 从 sequence 第一个动作循环，不重新执行 start_at 的前置透传 |

**passthrough 不保证成功**，真实后端仍可能返回错误。要保证第三次固定 200，使用 `respond(status=200)`。

计数保存在单进程内存中，重启清空，不在多个副本之间共享。TTL 为自最后一次命中起的闲置时间；过期时移除并记录 `state_expired`，下次该 scope 从 1 开始。容量满时不会淘汰活跃测试，而是返回带 scenario 标记的 400 并记录 `state_capacity`。请把 TTL 设置得大于一次完整重试测试的持续时间。

## 4. 故障动作

每个动作可带 `repeat`（正整数，默认 1）。

| action | 参数 | 行为 |
| --- | --- | --- |
| passthrough | 无 | 访问真实后端 |
| respond | status、可选 headers/body/json_body/body_base64 | 不访问后端，生成最终 HTTP 响应 |
| delay_before | seconds | 转发前异步等待，然后正常转发 |
| delay_after | seconds | 收到完整后端响应后，再延迟交付客户端 |
| timeout | seconds | 不访问后端，在有限时间内不响应，时间到后终止请求 |
| disconnect | 无 | 终止当前 flow，不承诺特定 TCP 标志或 errno |
| reset | 无 | 对该客户端 TCP 连接发送真实 RST |

`seconds` 必须大于 0 且不超过 3600。测试客户端读超时时，让 seconds 明显大于客户端 timeout。delay_before 到期后如果客户端已断开，不承诺仍会写入后端；验证“后端已成功而客户端超时”应使用 delay_after。

`respond.status` 接受 200–599；1xx 是中间响应，不能作为该动作的最终响应。文本使用 UTF-8，json_body 自动生成 JSON Content-Type，body_base64 用于二进制。三种 body 字段互斥；`json_body: null` 表示 JSON null。headers 值须为字符串，例如 `Retry-After: '1'`。Content-Length 等分帧头由程序管理，不能手动配置。204/205/304 不允许非空 body，HEAD 不发送 body。

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
# 同一 scope：429、429、200
bash .agent/run.sh sh -c 'for i in 1 2 3; do curl -sS -o /dev/null -w "%{http_code}\n" -H "X-Test-Run-ID: demo" http://host.docker.internal:18080/retry; done'

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
| GET /rules | 规则 ID、服务、scope、起始序号、动作摘要，不暴露 mock body |
| GET /state | `{"counters":[{"service":"orders","rule":"retry-twice","scope":"demo","count":3}]}` |
| POST /reset | JSON 可选 service/rule/scope，返回实际删除的计数条目数 |

```bash
bash .agent/run.sh curl -sS -H 'Authorization: Bearer local-demo-token' http://host.docker.internal:19090/state
bash .agent/run.sh curl -sS -X POST -H 'Authorization: Bearer local-demo-token' -H 'Content-Type: application/json' -d '{"service":"orders","rule":"retry-twice","scope":"demo"}' http://host.docker.internal:19090/reset
```

`{}` 重置全部。filter 不匹配返回 `{"reset":0}`。错误 JSON、未知字段返回 400，未经认证返回 401，超过 4 KiB 的管理请求体返回 413。重置只影响之后的分配，在途请求保留已选动作。

## 6. 排查

事件日志包含时间、request_id、服务、规则、scope（最多 64 字符）、序号、动作、阶段和结果。默认不记录请求体、Authorization 或 Cookie。scope 不应放入秘密数据。

真实上游连接或 TLS 校验失败通常由 mitmproxy 返回 502；这与 respond 生成的 502 不同，可用选中动作日志和后端调用次数区分。场景配置约定错误携带 `X-Fault-Engine-Error: scenario`。

当前缓冲请求/响应体，上限默认为 10 MiB；不保证流式/SSE 或无限响应。HTTP/2 被禁用，以保持连接级故障可复现。当前客户端入口只验证 HTTP/1.1 明文访问；HTTPS 是指代理到后端的链路。

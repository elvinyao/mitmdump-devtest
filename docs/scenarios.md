# 场景手册

先按 [README](../README.md) 启动完整演示，再从另一终端执行下面的 runner 命令。所有规则来自 [scenarios.yaml](../examples/scenarios.yaml)；除 inventory `/cycle` 使用宿主机端口 18081，其余使用 18080。

表中的“后端”表示真实透传，演示后端返回 200；接入真实服务后不保证成功。“测试 ID”表示必须携带 `X-Test-Run-ID`，同一逻辑调用的重试保持相同 ID，不同测试使用不同 ID。未标注的规则使用 `global` 计数。

## 选择场景

### 重试、恢复与定向匹配

| 规则 ID | 请求 | 预期 / 适用场景 |
| --- | --- | --- |
| retry-twice | `/retry`，测试 ID | 429、429、后端；验证次数与 Retry-After |
| nth-error | `/nth`，测试 ID | 前 4 次后端，第 5–6 次 503，之后后端 |
| fixed-success | `/mock` | 400、400、固定 200，之后持续 200 |
| inventory-cycle | inventory `/cycle`，GET/POST/PUT | 500、后端交替，使用全局计数 |
| retry-budget | `/always-unavailable` | 持续 503，检查重试预算耗尽 |
| mixed-retry | `/mixed-retry`，测试 ID | 3 秒不响应、503、固定 200，之后持续 200 |
| targeted-write-retry | POST/PUT `/payments/123?mode=fault`，`X-Client: mobile`，测试 ID | 同时满足方法、正则路径、header 和 query 才执行 429、429、后端 |
| recovery-cycle | `/recovery-cycle`，测试 ID | 前 2 次后端，随后按 503×2、后端×2 循环；预热只执行一次 |
| maintenance-health-exception | GET `/maintenance/health` | 始终透传；必须排在维护窗口规则之前 |
| maintenance-window | `/maintenance/` 下其他请求 | 持续 503，Retry-After 为 5；前面的豁免规则优先 |

### 超时、断连与写入确认丢失

| 规则 ID | 请求 | 预期 / 适用场景 |
| --- | --- | --- |
| before | `/slow-before` | 转发前延迟 3 秒 |
| after | `/slow-after` | 后端响应后延迟 3 秒；检查重复写入风险 |
| no-response | `/timeout` | 10 秒内不响应，不访问后端，时间到终止 |
| tcp-reset | `/reset` | 连接级 TCP RST |
| disconnect | `/disconnect` | 断开当前请求，不承诺 TCP RST |
| slow-mock | `/slow-mock` | 等待 3 秒后固定 200，不访问后端 |
| write-then-error | POST `/write-then-error`，测试 ID | 第一次收到后端响应后替换成 503，之后透传 |
| write-then-reset | POST `/write-then-reset`，测试 ID | 第一次收到后端响应后 RST，之后透传 |
| write-then-disconnect | POST `/write-then-disconnect`，测试 ID | 第一次收到后端响应后断连，之后透传 |
| gateway-timeout | `/gateway-timeout` | 立即返回 HTTP 504；用于与读超时区分 |

精确 RST 断言需要容器内直连；宿主机端口映射可能改变客户端所见错误，详见[故障区别](usage.md#超时504-和-errno-104)。HTTP 504 是有效 HTTP 响应；curl 默认不会因状态码 504 非零退出，使用 `-i` 查看状态，或按需使用 `--fail-with-body`。

### 状态码、解析与响应语义

| 规则 ID | 请求 | 预期 / 适用场景 |
| --- | --- | --- |
| authentication-required | `/unauthorized` | 持续 401 与 WWW-Authenticate，检查停止重试或刷新凭证 |
| write-conflict | POST/PUT/PATCH `/conflict` | 持续 409，检查版本冲突处理 |
| malformed-json | `/invalid-json` | HTTP 200，JSON 不完整 |
| empty-json-response | `/empty-json` | HTTP 200，声明 JSON 但 body 为空 |
| html-gateway-error | `/html-error` | HTTP 502 + HTML，检查非 JSON 错误体 |
| business-error | `/business-error` | HTTP 200 + `ok: false`，检查业务失败识别 |
| preserve-method-redirect | `/redirect` | 307 到 `/echo`；跟随重定向时保留 POST/body |
| cached-resource | GET/HEAD `/cached`，`If-None-Match: "demo-v1"` | 304 + ETag，无 body；其他条件透传 |
| delete-no-content | DELETE `/no-content` | 204，无 body；其他方法透传 |
| binary-download | GET/HEAD `/binary` | GET 返回字节 `00 01 ff 80`；HEAD 无 body，Content-Length 为 4 |

## 定向故障与单次测试重跑

只有四个匹配条件全部成立，下面的请求才进入定向限流规则：

```bash
bash .agent/run.sh sh -lc 'for i in 1 2 3; do curl -sS -o /dev/null -w "%{http_code}\n" -X POST -H "X-Client: mobile" -H "X-Test-Run-ID: checkout-001" -d "payment=1" "http://host.docker.internal:18080/payments/123?mode=fault"; done'
```

第一次运行得到 429、429、200。去掉 `X-Client` 或改成 `mode=normal` 会透传且不占用该规则计数；满足匹配但缺少测试 ID 则返回 scenario 400。更换为 `checkout-002` 会独立从第一次开始。

先查询同一个测试的状态，再精确重置，避免影响其他测试：

```bash
bash .agent/run.sh curl -sS --get -H 'Authorization: Bearer local-demo-token' --data-urlencode 'service=orders' --data-urlencode 'rule=targeted-write-retry' --data-urlencode 'scope=checkout-001' http://host.docker.internal:19090/state
bash .agent/run.sh curl -sS -X POST -H 'Authorization: Bearer local-demo-token' -H 'Content-Type: application/json' -d '{"service":"orders","rule":"targeted-write-retry","scope":"checkout-001"}' http://host.docker.internal:19090/reset
```

查询应显示 `count: 3`，重置应返回 `{"reset":1}`，再执行上面的三次请求即可复现。未命中或已过期时可能返回空状态和 `reset: 0`。

## 后端已执行后的写入重试

```bash
bash .agent/run.sh sh -lc 'for i in 1 2; do curl -sS -i -X POST -d "payment=1" -H "X-Test-Run-ID: write-001" http://host.docker.internal:18080/write-then-error; done'
```

首次使用 `write-001` 时，客户端依次看到 503、后端 200，而后端被调用两次；重跑前更换 ID，或按 `rule: write-then-error`、`scope: write-001` 精确 reset。演示后端只回显请求，不实现业务幂等性；真实客户端的验收还应检查业务记录和幂等键。累计 `upstream_calls` 包括其他请求，比较前后差值时保持该后端无其他流量。

## 循环恢复与规则顺序

```bash
bash .agent/run.sh sh -lc 'for i in 1 2 3 4 5 6 7 8; do curl -sS -o /dev/null -w "%{http_code}\n" -H "X-Test-Run-ID: recovery-001" http://host.docker.internal:18080/recovery-cycle; done'
bash .agent/run.sh curl -sS -i http://host.docker.internal:18080/maintenance/health
bash .agent/run.sh curl -sS -i http://host.docker.internal:18080/maintenance/orders
```

首次循环得到 200、200、503、503、200、200、503、503。健康检查透传，orders 路径返回维护错误；只有 GET 健康检查享有豁免。将宽泛规则移到前面会遮蔽豁免，配置验证不会代替业务上的规则优先级判断。

## 并发与长期运行

- 并发测试各自使用不同测试 ID；同 ID 的并发请求按进入引擎的顺序分配序号，不保证响应完成顺序。
- `global` 规则不能靠更换请求头重启；按规则 ID 调用 reset，或重启整个进程。
- TTL 是自最后一次命中起的闲置时间，设得比完整重试测试更长；过期或重启都会清空计数。
- 容量满时新 scope 得到 scenario 400，活跃 scope 不会被静默淘汰。先清理已结束的测试，或按需求调整 capacity。
- 用 `/rules` 检查匹配摘要和动作次数，用过滤后的 `/state` 检查实际序号；reset 不改变已经开始的请求。
- `/state` 有 `next_cursor` 时继续翻页，直到该字段消失；空页也可能有后续游标。
- 新连接首个请求出现 `503 + capacity`，或复用连接被关闭并记录 `capacity_exhausted`，表示资源限额触发而非场景故障；降低并发/缩短延迟，或调整 `limits`。被拒请求不占场景序号。

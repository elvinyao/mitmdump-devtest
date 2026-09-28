# 常用故障模式与改善路线

调研日期：2026-09-29。以下区分官方工具能力、本项目实现与后续建议。

| 模式 | 用途与官方来源 | 本项目状态 |
| --- | --- | --- |
| 按比例、定向故障 | 部分调用失败时观察重试；[Envoy fault filter](https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/fault_filter.html) 提供百分比与 header 选择 | 新增 probability/seed；已有 method/path/header/query 匹配 |
| 延迟分布 | 测试延迟阈值与偶发慢请求；[WireMock](https://wiremock.org/docs/simulating-faults/) 提供 uniform、lognormal 和分块慢发 | 新增均匀 jitter；低概率长延迟可组合，尚无 lognormal/分块慢发 |
| 有状态流程 | 创建后查询、轮询 pending→ready；[WireMock scenarios](https://wiremock.org/docs/stateful-behaviour/) 提供状态机 | 已有按次序列，尚无跨规则状态机 |
| 局部响应修改 | 保留真实数据，仅改 header/JSON；[Chaos Mesh HTTPChaos](https://chaos-mesh.org/docs/simulate-http-chaos-on-kubernetes/) 提供 replace/patch | 已有完整 respond_after 替换，尚无局部 patch |
| 传输阶段故障 | 大响应下载中断、低带宽；[Toxiproxy](https://github.com/Shopify/toxiproxy) 提供 bandwidth、slow_close、limit_data 等 | 已有连接级 reset，尚无限速或指定字节后截断 |

## 本轮实现选择

比例与抖动默认关闭，保持旧配置行为。为兼顾波动测试与 CI 重放，本项目采用无共享 RNG 的 SHA-256 采样：输入包括版本标签、seed、服务/规则/scope、序号和通道，摘要前 53 位映射到 [0,1)。这是本项目设计，不代表上述工具提供相同保证。

未抽中请求也分配序号，保留 first-match、容量、TTL 和 reset 契约。延迟在同步决策时固定，不修改只读执行计划。有限样本不保证精确比例；不能断言“20 次必须失败 5 次”。精确次数回归继续用 nth/repeat/cycle；采样用于批量压力与延迟阈值测试。

示例新增 25% 503、50–200 ms 抖动、5% 的 1–2 秒慢响应；详见 [场景手册](scenarios.md)。

## 后续优先级建议

1. **客户端验收助手**：有界、默认脱敏的事件记录与自动断言，检查重试次数、间隔、Idempotency-Key 保留和取消后无重试。现有工具制造故障与展示计数，尚不能自动判断业务策略是否正确，这是最直接的下一步价值。
2. **按时间恢复窗口**：服务不可用 5 秒后恢复，用于断路器开启、半开探测和关闭。按次数循环不能替代时间窗口，需可注入时钟、每 scope 起点与 reset 语义。
3. **响应局部 patch**：缺字段、错类型、改 Retry-After，同时保留真实响应其余内容。需要定义非 JSON/非法 JSON、压缩、HEAD 与 Content-Length 行为。
4. **流式慢发/中途截断**：首字节正常但 body 慢发或读到部分数据后断连。需要独立的流式执行设计、背压/取消和 socket 验收，完整响应后的 delay 不能代替它。
5. **跨请求状态机**：异步任务、token 刷新、支付确认流程。状态按 scope 隔离，转换需原子化，避免并行测试互相污染。

以上是结合当前代码的建议。本轮未实现这五项，也未引入外部服务。

## 底座建议

当前继续使用 mitmproxy：已经覆盖 HTTP 匹配、访问真实后端和后置故障。若重点转向任意 TCP、带宽或字节限制，可组合 Toxiproxy；若主要需要复杂纯 mock 状态机，可评估 WireMock，避免重建完整服务虚拟化平台。集群级 Kubernetes 实验可考虑 Chaos Mesh。这些属于后续架构选项，不要求当前部署更换底座。

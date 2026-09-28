# 配置、诊断与客户端验收设计

目标：使用 superpowers 计划、测试先行、独立审查和完成前验证，将现有故障代理变成可以直接生成配置、解释规则、观察请求并用于 CI 验收的工具。用户已要求自行制定并实施到验收，沿用本工作区及 Docker runner。

## 选择与依据

优先完成 CLI 使用闭环。替代方案是 Web UI（增加服务/前端维护成本）或继续增加故障动作（未解决操作和验收成本），本轮选择 CLI，不更换代理底座。

参考 [WireMock verifying](https://wiremock.org/docs/verifying/) 的 request journal、次数校验与诊断，以及 [Envoy admin](https://www.envoyproxy.io/docs/envoy/latest/operations/admin.html) 的独立管理接口。借鉴工作方式，不复制其完整协议，也不声称兼容。

## A. 本地配置与诊断

- `fault-engine init FILE --upstream URL --preset retry|timeout|reset|jitter` 生成可验证的完整 YAML；可设置 service、path、port、admin-port；默认 service=backend、path=/retry、port=8080、admin-port=9090、容器监听 0.0.0.0。不嵌入管理 token，使用 FAULT_ADMIN_TOKEN。文件存在时拒绝覆盖，校验失败不创建文件。
- retry 模板为同 X-Test-Run-ID 两次 503 后透传；timeout 为 10 秒不响应；reset 为真实 reset；jitter 为 delay_after 50–200 ms。结束策略除 retry 外 repeat_last。模板直接从程序生成，wheel 安装不依赖仓库 examples。
- `fault-engine explain FILE --service ID --method GET --path '/retry?x=1' --header 'Name: value' --ordinal 1` 离线解释每条候选的 method/path/header/query 匹配失败维度、第一条选中规则、scope 校验与指定序号的实际采样动作。匹配和动作选择复用 Engine 实现，不改变计数、不访问后端、不需要 token。输出 JSON，不回显 body、header/query 值。

## B. 请求记录与验收

- Journal 容量固定有界，配置 `limits.journal_capacity` 默认 1000，允许 0（关闭）至 100000。一请求一条，按到达顺序，结束幂等；只记录进入 `_request` 并完成场景选择的请求（包括未匹配透传）。未完成上传、容量拒绝、规则契约错误不计入此记录。
- 记录字段为 request_id、服务、规则、scope、方法、序号、实际动作、sampled、状态码或 transport outcome、开始时间和耗时、upstream_received。不存路径、query、header、body；scope 仅用非秘密测试 ID。
- 开始时分配递增 ID；只保留最新 N 条。清空不复用 ID。游标包含随机实例 ID 和位置，重启后旧游标失效。GET `/requests` 支持 service/rule/scope AND 过滤、after 游标和 limit（1–1000），返回 requests、checkpoint、可选 next_cursor，以及 complete 标记。完整性基于游标是否早于被删除/淘汰的范围，不因过滤而猜测完整。
- POST `/requests/reset` 接受空对象，清空请求记录，返回新 checkpoint；不重置序号。旧 checkpoint 不能把缺失历史当完整。
- POST `/verify`：service/rule/scope 可选、after 可选、count 必填非负整数、statuses 可选 HTTP 状态码数组（长度须等于 count）、min_interval_seconds 可选有限非负值。基于当前完整窗口检查精确次数、到达顺序的状态序列、相邻到达间隔下限；间隔使用 monotonic 时钟，不等同于“前一个响应结束后退避”。
- journal 关闭、窗口丢失或选中请求尚在途时返回 matched=false、complete=false，禁止不完整证据通过。参数无效 400，验证不符合预期为 200 + matched=false。未知/重复查询参数拒绝，全部接口沿用 bearer 鉴权与管理 body 上限。
- 完成 response hook 仅表示代理已经准备发送响应，不声称客户端收到或后端业务提交。断连/reset/timeout/取消/真实上游错误均有明确 outcome，不误写成 HTTP 500；客户端 disconnect 回调及后续 error 不覆盖已终结记录。

## C. 管理命令与完整使用路径

- CLI 子命令 `requests`、`verify`、`reset`、`journal-clear`，支持 `--admin-url`（默认 http://127.0.0.1:9090）和 `--token-env`（默认 FAULT_ADMIN_TOKEN）；只从环境读 token，不通过 CLI 参数暴露。
- requests 按单页返回 JSON，显式传递 after/limit；verify 输出 JSON，匹配退出 0，断言失败或不完整退出 1，参数/HTTP/网络错误退出 2。请求具有有限超时，不跟随重定向、不使用环境代理转发管理 token。错误消息不输出 token 或未经校验的远端 body。
- reset 是序号重置，journal-clear 是记录清空，名称和帮助区分。CLI 提供 scope/service/rule 精确选择，清空请求记录作用全局且文档明确。
- 中文快速流程与真实 CLI 子进程验收：init→validate→explain→serve→执行 503/503/200 客户端重试→requests→verify 成功→故意错误断言退出 1→reset 和 journal-clear→再次运行。独立 wheel 覆盖新本地命令和管理能力。

## 验收与范围

旧有 328 项测试及 RST/HEAD/资源限额/采样契约继续通过；新增单元、真实 HTTP 与 CLI 回归；ruff/ty/lock/build/wheel 全部门槛通过。每项新增能力有文档、失败证据和通过结果。两阶段独立审查（需求后质量）完成，结果提交，禁止以覆盖率代替验收。

不增加 UI、实时热加载、跨规则状态机、流式截断；不自动决定某业务是否可以重试。后续需求仍保留在模式调研，不将本轮验收表述为这些能力已经实现。

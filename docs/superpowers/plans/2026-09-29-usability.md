# Fault Engine Usability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成生成配置、离线诊断、请求记录与客户端 CI 验收的 CLI 闭环。

**Architecture:** 本地工具复用 Config、Plan、Engine；Runtime 持有有界 Journal，Addon 记录选择与终结，Admin 负责鉴权与检查。独立 CLI 客户端使用 aiohttp 访问管理面，不新增依赖。

**Tech Stack:** Python 3.12、mitmproxy 12.2.3、aiohttp、Pydantic、pytest、uv、ruff、ty；所有执行经 `bash .agent/run.sh`。

设计依据：`docs/superpowers/specs/2026-09-29-usability-design.md`。当前干净工作区的已有实现是基线；不得重置用户工作。用户已授权计划实施直至验收，不在任务之间重复请求许可。

### Task 1: 配置模板与纯诊断

Files: create `src/fault_engine/local_commands.py`, `tests/test_local_commands.py`; modify `src/fault_engine/engine.py`, `src/fault_engine/cli.py`。

- [x] 先测试 init 生成四种严格合法配置、已有文件拒绝、无效 origin 不创建文件；测试 explain 与真实 decide 的选中规则/采样一致且不更新 snapshot。
- [x] Docker 执行 `uv run pytest tests/test_local_commands.py -q`，记录缺失命令/方法失败。
- [x] 使用 `Path.open('x')` 写 YAML；先 `Config.model_validate(data)`；生成器接口 `make_config(*, upstream, preset, service, path, port, admin_port) -> Config`。
- [x] Engine 提取纯匹配原因及指定序号决策；`explain(service, method, path, headers, query, ordinal=1)` 返回 JSON 字段 matched_rule/candidates/decision，候选只显示失败维度，不显示输入值。
- [x] CLI 本地注册与 dispatch；先读取 Config，再复用同一 matcher 与 action sampler；运行 focused tests/ruff/ty，交给独立需求审查后做质量审查。

### Task 2: 有界 Journal 与纯验证

Files: create `src/fault_engine/journal.py`, `tests/test_journal.py`; modify `src/fault_engine/config.py`, `src/fault_engine/plan.py`。

- [x] 先写一请求一条、重复 finish、淘汰/clear、旧实例游标、分页过滤、pending、关闭模式、精确次数/状态/单调时间间隔失败测试。
- [x] Docker 运行该文件确认缺失 Journal 失败。
- [x] `Journal(capacity, clock=time.monotonic)`；`start(request_id, **allowed_fields)` 分配 ID，`finish(request_id, outcome, status=None)` 幂等，`mark_upstream(request_id)` 记录观察；`page(..., after=None, limit=100)` 和 `verify(..., count, statuses=None, min_interval_seconds=None)` 返回脱敏 JSON；`clear()` 返回 checkpoint。
- [x] 游标采用 `uuid:id` 严格验证，不接受跨实例或超过当前 tip；保留 dropped_through，after 早于边界时 complete=false；容量 0 不存记录且 verify 不通过。
- [x] 添加 journal_capacity 到 Config→ExecutionPlan，所有规则快照契约保持；focused tests/ruff/ty 通过。

### Task 3: HTTP 集成与管理 API

Files: modify `src/fault_engine/addon.py`, `src/fault_engine/runtime.py`, `src/fault_engine/admin.py`; create `tests/test_journal_integration.py`。

- [x] 写真实网络测试：503/503/200 仅 3 条；不记录 auth/cookie/body/query/path；scope 隔离；reset/timeout/取消不生成重复终结；pending 或容量轮转后不完整验证不能通过。
- [x] 运行失败测试；Runtime 创建 Journal 并交给 Addon/Admin，在场景选择后开始记录，response/error/fault/cancel 完成记录；记录不影响现有连接凭据。
- [x] 添加鉴权的 GET requests、POST requests/reset、POST verify；Pydantic 严格校验，未知重复参数 400；保留 4 KiB body 上限；管理结果 matched=false 使用 HTTP 200。
- [x] 重跑 journal/integration/resource/lifecycle 测试并独立审查。

### Task 4: 管理 CLI 与 CI 路径

Files: create `src/fault_engine/admin_client.py`, `tests/test_admin_client.py`, `tests/test_usability_workflow.py`; modify `src/fault_engine/cli.py`。

- [x] 先用真实 CLI subprocess 测试 requests/verify/reset/journal-clear 和退出码 0/1/2，缺 token、错误 HTTP、重定向拒绝、超时及脱敏。
- [x] 客户端 aiohttp `ClientSession(trust_env=False, timeout=ClientTimeout(total=5))`，每请求 `allow_redirects=False`；只从 token_env 取 bearer；验证非空 ASCII token，错误返回静态摘要。
- [x] 实现完整 init→explain→运行重试→verify→reset/clear→重跑测试，真正访问后端且检查次数；客户端助手不自动推断业务重试政策。
- [x] 更新 wheel_smoke 使用安装包 init/explain 和 verify API；需求审查通过后质量审查。

### Task 5: 文档、验收和提交

Files: modify `README.md`, `docs/usage.md`, `docs/development.md`, `docs/verification.md`, `.agent/wheel_smoke.py`。

- [x] 中文文档覆盖两种 reset、游标/容量不完整、到达间隔与退避差异、exit codes、Docker 地址和 token_env；README 最短操作路径可复制。
- [x] 独立需求审查逐项核对设计 A/B/C 与回归证据；独立质量审查处理生命周期、误通过、泄露、CLI 错误路径。
- [x] `bash .agent/run.sh bash .agent/check.sh` 完整通过：锁文件、格式、lint、ty、全部 tests、build、独立 wheel。
- [x] 检查 git diff、更新验收表与本计划完成项，提交本地 commits；不 push。以当前证据逐项完成 goal audit 后调用 update_goal complete。

# 第二轮审查与场景扩展

用户授权：深入 review、改善、补充场景、自行 review 并 commit。沿用已经批准的 mitmproxy/纯引擎/TCP 前置层架构，所有执行仍经 Docker runner。

## 扩展契约

- `respond_after`：先访问真实 upstream，收到完整 HTTP 响应后替换为配置的响应；复用 respond 的 status/headers/body。upstream 建连失败时保留真实代理错误，不伪装成成功收到响应。
- `reset_after` / `disconnect_after`：后端完整响应到达后，对客户端执行已有 reset/disconnect 语义；用于测试写入已发生但客户端认为失败的重试与幂等性。
- respond / respond_after 增加可选 `delay_seconds`（0–3600，默认 0），在发出 mock 前异步等待。普通 respond 仍不访问 upstream。
- 不引入随机概率、网络丢包或流式截断，保证本轮新增动作仍然可确定地复现和验收。

## 执行清单

- [x] 并行独立检查 HTTP、配置/状态/管理、生命周期；发现先复现为回归测试。
- [x] 先测试扩展动作缺失，再实现；验证 upstream 调用次数、真实 errno、HEAD 和取消行为。
- [x] 修复审查发现，再按用户要求自审全部变更；补修排队启动/关闭所有权竞态及 reset_after 失败的 HEAD 分帧问题。
- [x] 增加可执行场景示例和中文说明，确保配置与文档可直接运行。
- [x] Docker 内运行锁文件检查、ruff、ty、全部测试、build 和隔离包验证：195 passed，覆盖率 95%，22 条规则验证通过。
- [x] 更新验收记录，检查最终 diff 并提交本轮变更；不 push。

## 验证路径

配置/状态复现放在 `tests/test_state_review.py`，HTTP 复现在 `tests/test_http_review.py`，生命周期复现在 `tests/test_lifecycle_review.py`；动作验收放在 `tests/test_extended_scenarios.py`。各文件单独运行可重现失败，最后执行 `.agent/check.sh`。

# 预算修复复核：自动重试边界（2026-11-07）

## 结论

上一轮 F1 的跨进程事务与 F2 的 unknown 占额机制已经接入。独立重跑两个新增专项测试均通过。没有证据要求重做这两部分。

但实际执行器仍按一次 `chat_auto()` 预留与结算；该函数内部允许多次传输。因此“42 次实际尝试以及失败未知用量均受发送前控制”尚不成立。在启动下一批参考发现前，应收口这一处调用边界。

## 已复现：一次预留发生两次尝试，未知用量消失

代码路径：

- `scripts/bc_t4_run_reference_discovery.py:225-243`：一次 reserve 后调用 chat_auto。
- `backend/core/llm_client.py:1569-1614`：chat_auto 路由到带自动重试的方法。
- `scripts/bc_t4_run_reference_discovery.py:263-294`：只按最终结果的 tokens 结算一次，没有逐次处理 attempts、先前未知消耗和 prior_known_tokens。

离线实验使用真实 LLMClient.chat_auto 与真实 UsageArchive，只将传输入口 chat 替换为模拟响应；不访问网络、不读取真实密钥、不修改生产台账。配置非流式、温度阶段最多两次尝试、退避阶段零次。

步骤：

1. 预算上限设为 1 次尝试、500 输入；预留输入 300。
2. 第一次模拟传输返回超时、tokens=None。
3. 客户端内部自动重试，第二次成功返回输入 180、输出 10。
4. 按执行器当前分支，以最终 tokens 结算唯一预留。

实测：

```json
{
  "network_calls": 0,
  "mock_transport_attempts": 2,
  "budget_max_attempts": 1,
  "client_reported_attempts": 2,
  "archive_attempts": 1,
  "archive_unknown_count": 0,
  "archive_input_occupied": 180,
  "returned_tokens": [180, 10]
}
```

因此第一次超时的未知用量并未经过新 unknown 分支，第二次尝试也没有重新申请名额。问题是执行器与重试客户端之间的边界，不是文件锁或 unknown 记录本身失效。实验说明存在可触发路径，不证明历史四次参考调用曾发生漏账。

## 最小修复建议

为本轮试跑提供明确的单次传输入口，关闭客户端包装层和 SDK 层的隐式重试。由外层运行器决定是否重试；每次重试先重新取得预算预留，再发送、独立结算。网络重试也要遵循既定总预算，不能等整个重试链结束才补记次数。

也可以在客户端每次真实传输前后接预算回调，但改动范围更大。无需为本轮试跑重构全项目调用架构。

注意：不能仅把某个 retries 配置设为零便宣称关闭全部重试，应验证实际传输次数。当前包装层有独立的温度阶段、退避阶段与特殊错误分支；SDK 是否重试也应由本地配置与拦截传输测试验证。

## 验收

- 总次数仅剩 1：第一次模拟超时后，不得出现第二次传输；台账保留 1 次 unknown 占用。
- 总次数允许 2、预算允许：第一次 unknown 与第二次 measured 分别记账；第一次未知占用不能被第二次成功覆盖。
- 次数允许但 token 预算不足：第二次必须在发送前被阻断。
- 缺失 usage 的成功响应也保持 unknown；不得因随后一次成功而丢失。
- 测试贯穿运行器使用的实际调用入口，并拦截最底层传输计数，避免只测台账方法。

本轮重跑命令：`pytest backend/tests/test_bc_production_readiness.py -q -k 'cross_process_reservation or unknown_usage_retains'`，2 项通过。未重跑全量测试，134+33 为交付方报告。未调用任何项目付费 API，未运行两臂生成。

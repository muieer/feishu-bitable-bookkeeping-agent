# 系统提示词接入合同

默认提示词为 `system.md`；`AGENT_SYSTEM_PROMPT_PATH` 可指定其他 UTF-8 文件。启动时读取，文件缺失或为空则停止启动 Agent。修改后须重启。代码不内置替代业务提示词。

## 完整消息与结构化输出

单笔和多笔统一使用 `MessagePlan.decisions`，一条完整消息先进行一次推理，最多返回 200 个决策。程序不按标点拆消息；同一业务对象本轮只返回一个最终决策。结构化解析失败时整条消息不执行写入。

| 字段 | 合同 |
| --- | --- |
| `decision_id` | 本消息内唯一标识；历史阶段保留原 ID |
| `source_text`、`source_occurrence` | 表达该意图的当前消息连续原文及出现序号；来源必须真实存在，独立新账单来源不能重叠或重复 |
| `action` | `ignore`、`create`、`update`、`clarify`、`preference`；同一消息可混合不同操作 |
| `expense` | 必填，可为 null；create/update 必须为对象，其中 date、amount、description、category 都必填，未知或未修改值为 null；追问保留已知事实 |
| `continues_pending` | 必填；补充已有待办为 true，新消费为 false |
| `target_id`、`target_number` | 必填，可为 null；分别关联允许操作的稳定账单 ID 或最新回复编号，不能引用其他用户或会话的记录 |
| `target_reference` | 自然语言关联稳定 ID 时的原文引用，须唯一匹配已有账单描述或原文 |
| `question`、`unresolved` | question 必填，可为 null；clarify 必须提供问题。有问题或未解决字段时不执行该笔写入 |
| `clarification_scope` | `expense` 保存账单事实；`target` 仅追问目标或意图，不创建待办 |
| `needs_history`、`history_keywords` | 已确认消费且分类需要历史时使用；关键词最多两个，程序去重查询 |
| `preference` | 必填，可为 null；preference 决策必须提供对象、适用条件、完整分类及长期意图证据，其他操作为 null |
| `cancel_pending` | 明确取消未提交待办时用 ignore 并指定目标；不能删除成功账单或重试待核对结果 |

金额的最终业务验证按账单独立执行：有限正数，最多两位小数及 14 位有效数字。分类路径需符合个人分类树和远端单选选项，模型不能创造分类。

## 每轮运行上下文

程序提供完整个人分类树、规则和最新明确偏好，以及以下数据：

| 上下文 | 用途 |
| --- | --- |
| `message_sent_at`、`timezone` | 本条消息在配置时区的时间 |
| `default_date`、`relative_date_base` | 新消费默认日期及当前消息相对日期基准 |
| `displayed_items` | 最新最终回复的固定编号映射 |
| `pending_items` | 所有待补充、待处理的账单，各自保留原文、日期基准、已知事实和问题 |
| `allowed_items` | 当前用户、当前会话内允许关联的账单 |
| `last_record` | 最近成功账单，无编号即时纠错目标 |
| `recent_turns` | 最近必要对话，仅在内存中保存 |

账单上下文包含 `bill_id`、`text`、`sent_at`、`expense`、`status`、`question`、`pending_action`、`pending_patch`、`record_id` 和远端已确认的 `confirmed`。未完成修改的草稿与已确认字段分开保存。

## 历史补全阶段

首轮无模型可调用的业务工具。程序仅对 `needs_history` 决策的不同关键词执行只读查询，过滤非法分类案例，再统一发起一次补全推理。模型只能返回 `unresolved_decisions` 中的 ID；只允许补全分类或追问，不能改变交易事实、路由、来源或修改意图。程序保留首轮已确定决策。历史阶段失败或违反合同时，需要历史的项目独立追问，其他确定项目继续执行。

普通消息仅需一次模型请求；输出校验允许一次重试，历史补全也允许一次重试。SDK 的 HTTP 自动重试另计，受发送前 HTTP 请求额度约束。

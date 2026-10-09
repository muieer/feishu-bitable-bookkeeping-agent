# 系统提示词接入说明

将正式提示词放在同目录的 `system.md`，或通过 `AGENT_SYSTEM_PROMPT_PATH` 指定其他 UTF-8 文件。当前 `system.md` 留空；应用不会自动填入或使用替代业务提示词。每次启动 Agent 时读取文件，修改后重启。

## 运行时提供的信息

程序每轮提供完整分类树、个人分类规则、已保存的明确长期偏好，以及以下上下文：

| 信息 | 用途 |
| --- | --- |
| `message_sent_at`、`timezone` | 当前消息的发送时间和配置时区 |
| `default_date`、`relative_date_base` | 新消费的默认日期和相对日期基准 |
| `pending` | 待回答的追问、原始消费消息及已提取字段 |
| `pending_default_date`、`pending_relative_date_base` | 回答追问时沿用的原始日期基准 |
| `last_record` | 当前用户、当前会话唯一允许的纠错目标及最近成功字段 |
| `recent_turns` | 必要的最近对话；仅保留在内存中 |

提示词应描述实际消费、计划、价格讨论的区分规则；个人分类优先级和歧义处理；单次纠错与明确长期偏好的区别；多笔独立消费与单笔商品明细的区别。这里的表格和接口说明是接入合同，不是代写的正式提示词。

## 结构化输出合同

Pydantic AI 自动向模型提供 `Decision` 的 JSON Schema，模型通过结构化输出返回决策，无需发送自由文本结果或宣称写入成功。

- `action`：`ignore`、`create`、`update`、`preference`、`clarify`。
- `expense`：消费日期 `date`（`YYYY-MM-DD`）、正数金额 `amount`、简洁描述 `description`、分类 `category`。追问时允许只填已明确字段，未指定日期时可省略。
- `category`：`level1`、`level2`、`level3`。必须符合分类树，有三级子节点时不能省略三级分类。
- `record_id`：仅用于 `update`，只能指向 `last_record`；省略时程序使用最近成功账单。
- `preference`：消费对象 `subject`、适用条件 `condition`、完整合法 `category` 和用户明确表达长期偏好的原文 `evidence`。不能由单次纠错推断长期偏好。
- `question`、`unresolved`：缺失或歧义信息；有未解决问题时程序不写入。
- `needs_history`：实际消费分类需要历史案例时为 `true`。第一轮没有历史工具；确认记账意图后才允许下一轮检索。
- `multiple_expenses`：多笔独立消费时为 `true`，程序要求分别发送。
- `continues_pending`：正在回答已有追问时为 `true`，保留原消费字段及日期基准；新的消费为 `false`。

`ignore` 不携带操作、问题或历史检索请求，不产生业务工具调用和飞书回复。新增、纠错及偏好必须分别表达，混合操作需要追问澄清，不能在单个决策里隐含执行多项操作。

历史案例只作为辅助分类证据，明确规则优先。程序回复新增或纠错结果时直接使用成功写入且通过校验的数据，模型没有记录写入工具、任意命令权限或分类选项管理权限。

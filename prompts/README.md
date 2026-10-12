# 两阶段提示词合同

两阶段各使用独立 UTF-8 文件：`RECOGNITION_PROMPT_PATH` 默认 `prompts/recognition.md`，`DECISION_PROMPT_PATH` 默认 `prompts/decision.md`。文件缺失或为空时 Agent 不启动。修改后需要重启。程序不内置替代业务提示词。

## 第一阶段：识别与规范化

输入为完整消息和当前上下文中的结构化消费记录。模型只识别交易语义、关联已有编号、规范化文本；不推理分类、日期或最终行为。

```json
{"items":[{"type":"new","target_id":null,"raw":"咖啡12元"},{"type":"existing","target_id":"B1","raw":"B1金额改为20元"}]}
```

`items` 必填，最多 200 项，无消费为 `[]`。每项必须包含且只能包含 `type`、`target_id`、`raw`。`type` 只允许 `new` 或 `existing`；新消费目标必须 null，已有消费目标必须为合法格式的 B 编号。原文非空，不允许额外字段或自由文本。

程序再检查已有编号属于当前用户与会话，目标没有重复，原文可在规范化的完整消息中按顺序定位且不重叠。新消费由程序编号。识别失败时不进入决策或写入；无消费时回复「此次不包含消费行为。」且不进行表格操作。

## 第二阶段：推理与决策

输入包括 `items`（`bill_id`、`type`、`raw`）、`message_sent_at`、`timezone`、`default_date`、`related_existing`，以及完整 `categories` 和 `personal_rules`。没有全部历史对话、动态消费检索或偏好存储。每笔只使用自身信息和相关已有账单，不使用另一笔的日期、金额、用途或分类。

`MessagePlan.decisions` 必须与输入 B 编号一一对应，每个编号一个决策。`raw` 必须复制输入。新消费只能 `create` 或 `clarify`；已有消费只能 `update` 或 `clarify`。不能增加、删除、合并、拆分或重编号。

| 字段 | 约束 |
| --- | --- |
| `bill_id` | 必填，`B[1-9][0-9]*`，属于程序提供的编号集合 |
| `action` | 必填，判别联合类型：`create`、`update`、`clarify` |
| `raw` | 必填非空字符串，与输入一致 |
| `expense` | 必填；create/update 必须是四字段对象；clarify 必须 null |
| `reason` | 必填；create/update 必须 null；clarify 必须为非空具体原因 |

`expense` 必须包含 `date`、`amount`、`description`、`category` 四字段，每字段可为 null，但不能省略。`date` 严格使用 `YYYY-MM-DD`；金额使用 Decimal，布尔值不能作为金额。分类对象必须包含 `level1`、`level2`、`level3`，三级缺失需显式 null。所有对象禁止额外字段。

新增没有明确日期时由模型使用原消息当地发送日；程序执行前要求四项信息完整。更新仅填写明确要求修改的字段；程序保持其他字段并校验合并后的完整账单。金额为有限正数、最多两位小数，分类必须形成知识库中合法父子路径；有三级子项时不能省略三级。

结构化校验由 Pydantic 完成，业务错误按笔反馈为执行失败；模型的 `clarify` 独立分组，不改成待补充流程。分类知识中的旧 `ask` 条目仅作为信息缺口的描述，模型需转成不写入原因，不能启用追问或偏好学习。

## 运行约束

每阶段设置一次输出重试和两次模型请求上限。模型没有业务工具，无法直接写表格。两阶段共享单条消息的实际 HTTP 发送预算，包含 SDK 自动重试和重定向，上限 10 次。离线测试使用 FunctionModel 和 MockTransport，不调用真实 LLM；结构和流程测试不能证明真实模型的全部语义准确性。

# 飞书记账智能体

本项目在本地运行一个 Python 应用。用户向飞书机器人发送实际消费信息，Pydantic AI 调用火山方舟理解意图并依据个人分类知识生成结构化决策，程序校验后使用官方 `lark-cli` 写入现有多维表格，再回复结果。

现有查询 Demo 已改造为记账 MVP，包含单笔新增、即时纠错、明确的长期分类偏好更新、信息追问和结果反馈。**正式系统提示词尚待提供**：默认位置为 `prompts/system.md`，目前留空。文件缺失或为空时 Agent 停止启动；本地检查与只读表格诊断仍可运行。

## 能力与行为

| 消息或场景 | 行为 |
| --- | --- |
| 今天外卖午饭 25 元 | 字段和分类明确时直接记账 |
| 昨天咖啡 18 元 | 以消息发送时间解析日期，依据个人规则分类 |
| 给母亲买咖啡 30 元 | 明确的人情目的优先，按个人规则归入「人情往来 / 父母」 |
| 今天午饭 25 元 | 现有规则不允许仅凭「午饭」推断堂食或外卖，先追问 |
| 今天买了点东西 | 追问金额、消费内容和必要的分类依据，不写入 |
| 不对，刚才是 28 元 | 只修改当前用户、当前会话最近成功账单 |
| 以后豆浆都归类为食品 | 合法分类路径明确时，保存长期偏好；后续分类读取该偏好 |
| 明天打算喝咖啡 / 这个商品多少钱 / 查询统计 / 闲聊 | 输出忽略决策，不调用历史或写入工具，不回复 |
| 一条消息包含多笔独立消费 | 请用户分别发送，不合并、不只写其中一笔 |
| 图片、语音、机器人消息 | 忽略 |
| 写入超时、返回异常或没有唯一记录 ID | 提示结果待核对，不反馈记账成功，不自动重试 |

自然语言理解和上述分类判断由正式提示词、模型及个人知识共同决定。程序独立校验金额、日期、分类层级与执行目标，离线测试不能代替真实模型语义验收。多笔批量录入按技术架构的 MVP 范围暂不实现。

## 安装与配置

需要 Python 3.11 或以上版本、官方 `lark-cli` 和支持 Function Calling 的火山方舟模型。核心依赖沿用已验证版本：`lark-channel-sdk==1.4.0`、`pydantic-ai-slim[openai]==2.54.0`。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock.txt
```

已有 `.venv` 时直接激活。在项目根目录运行应用，配置及文件的相对路径均基于启动目录。

首次配置时复制 `.env.example`；已有 `.env` 时只补充需要覆盖的配置，不覆盖现有密钥。

```bash
cp .env.example .env
```

| 配置项 | 用途与默认值 |
| --- | --- |
| `LARK_APP_ID`、`LARK_APP_SECRET` | 飞书机器人凭证，必填 |
| `ARK_API_KEY`、`ARK_MODEL` | 方舟 API Key 和模型或推理接入点 ID，必填 |
| `ARK_BASE_URL` | 默认为 `https://ark.cn-beijing.volces.com/api/v3` |
| `BITABLE_BASE_TOKEN`、`BITABLE_TABLE_ID` | 现有 Base token 和表 ID，必填；表 ID 必须与知识文件一致 |
| `BITABLE_DATE_FIELD`、`BITABLE_AMOUNT_FIELD`、`BITABLE_DESCRIPTION_FIELD` | 默认「记账日期」「金额」「具体」 |
| `BITABLE_CATEGORY1_FIELD`、`BITABLE_CATEGORY2_FIELD`、`BITABLE_CATEGORY3_FIELD` | 默认「一级分类」「二级分类」「三级分类」 |
| `AGENT_SYSTEM_PROMPT_PATH` | 默认 `prompts/system.md`，UTF-8；也可配置绝对路径 |
| `CATEGORIES_PATH`、`RULES_PATH`、`SCHEMA_PATH` | 默认 `knowledge/categories.json`、`knowledge/rules.json`、`knowledge/schema.json` |
| `PREFERENCES_PATH` | 默认 `knowledge/preferences.json`，首次更新长期偏好时自动创建 |
| `OPERATION_JOURNAL_PATH` | 默认 `.runtime/operations.json`，写入去重日志 |
| `CONTEXT_TTL_SECONDS` | 默认 `1800`；追问、最近账单和最近对话的内存有效期 |
| `DISPLAY_TIMEZONE` | 默认 `Asia/Shanghai`；用于解析和写入日期 |
| `LARK_CLI_PATH`、`LARK_CLI_IDENTITY` | 默认 `lark-cli`、`user` |
| `CLI_TIMEOUT_SECONDS` | 默认 `30`；每次 CLI 调用的超时 |
| `AGENT_TIMEOUT_SECONDS` | 默认 `90`；模型理解阶段的超时，不中断已提交的写入 |
| `BITABLE_VIEW_ID` | 仅用于 `--query` 只读诊断；记账和历史分类检索针对整张目标表 |

默认分类知识沿用本地 `knowledge` 文件。`schema.json` 已从项目附件复制到该目录。当前 `knowledge/` 沿用原有 Git 忽略设置，复制项目到其他机器时需一并提供这些文件。不要用通用分类标准覆盖个人分类树或原始规则。

## 放入系统提示词

将正式系统提示词保存为 UTF-8 文件：

```text
/Users/yanghuhu/ai_project/feishu-bitable-bookkeeping-agent/prompts/system.md
```

应用在启动 Agent 时读取文件，作为 Pydantic AI 的 `system_prompt`。修改提示词后重启应用。代码没有内置替代业务提示词。提示词需要遵守的输出与上下文接口见 [提示词接入说明](prompts/README.md)；分类树、规则、长期偏好、消息时间和短期上下文由程序每轮提供，无需复制进提示词。

## 飞书与方舟准备

飞书自建应用需开启机器人能力、接收与发送消息权限，采用 WebSocket 长连接并订阅 `im.message.receive_v1`，发布版本使配置生效。单聊直接发送，群聊按 SDK 配置 @ 机器人。

CLI 登录态与机器人凭证独立，应用不会自动登录或切换身份。CLI 使用的用户必须能够读取目标表，并具备新增、修改记录的权限。按 CLI 实际返回的 `missing_scopes` 补充授权。可在交互终端检查：

```bash
lark-cli --version
lark-cli auth status --json --verify
```

方舟使用 OpenAI 兼容的 Chat Completions 接口。模型须支持结构化结果所需的 Function Calling，以及可选历史工具调用。

## 启动与检查

```bash
# 本地配置、分类树、规则和附件结构；提示词空缺会明确提示，不连接远端。
bookkeeping-agent --check

# 只读核对真实字段及完整分类选项，不调用模型、不写入。
bookkeeping-agent --check-remote

# 保留原 Demo 的只读诊断，不向机器人开放查询功能。
bookkeeping-agent --query 10

# 正式提示词准备好后，启动飞书长连接。
bookkeeping-agent
```

终端会话会实际写入配置的表格。连续交互支持追问与纠错；单条模式的会话在退出后丢失。

```bash
bookkeeping-agent --interactive
bookkeeping-agent --ask '今天外卖午饭25元'
```

也可使用 `python -m bookkeeping_agent`。终端每次输入有新消息 ID，重新输入相同文字会作为新的消费处理。

## 分类与执行约束

- 明确的长期偏好优先于旧规则，明确规则优先于动态历史案例。不同历史记录有冲突时不能按次数投票。
- 所有分类必须形成 `categories.json` 中的完整合法路径。某个二级分类有三级分支时必须填写合法三级分类，没有分支时三级分类为空。
- 金额必须是有限正数，最多两位小数；使用 `Decimal` 校验和输出 JSON 数字，不经过 Python `float` 转换。
- 未指定日期时使用消息发送日期。回答追问时保留原始日期基准，新消费使用新消息日期。消费日期在未来时追问，不写入。
- 仅写入日期、金额、消费内容和三级分类。年份、月份、周数等公式字段，以及图片、支出频率不主动填写。
- Agent 只有按需检索历史案例的只读工具。意图理解的第一轮不提供该工具；模型不能调用写接口或拼接 Shell 命令。
- 写入前读取远端字段结构，补查被 CLI 截断的选项；结构或选项与本地分类知识不符时停止写入。
- 纠错仅接受当前用户、当前会话最近成功账单。先读取目标并检查是否被外部修改，再只提交变化字段；修改后读取核对，未观察到完整结果时报告待核对。
- 偏好更新保留用户明确表达的原文证据，合法路径通过校验后原子保存到独立文件，不覆盖原始规则，也不修改已有账单。

## 去重与异常处理

SDK 去重负责重复事件。业务层在每次写入前将消息 ID 保存到本地日志，确认成功后记录结果与账单 ID。进程重启时保留该日志，最近对话和纠错上下文则允许丢失。

同一会话从理解到执行按顺序处理，同一目标表的写操作串行。CLI 通过异步子进程调用；超时或取消会终止子进程并清理临时文件。模型超时只发生在执行前。

`started` 表示操作已保留但没有完整成功记录，按结果不确定处理。对该消息的再次投递不会重新执行。已成功消息再次投递时静默忽略；回复失败也不会撤销或重复账单。日志仅保留消息 ID、操作状态和必要记录 ID，不保存长期对话。

遇到结果不确定时，先查看表格核对。不要删除整个 `.runtime/operations.json` 或重新发送消费来试探结果。若原消息没有重投递，服务不会主动重复发送失败的回复。

## 测试与实现入口

```bash
python -m pytest -q
ruff check src tests
ruff format --check src tests
```

测试使用真实 Pydantic AI、真实 SDK 消息规范化和模拟 CLI 子进程，覆盖新增、追问、纠错、偏好、隔离、金额、分类、去重、重启、异常、取消以及 OpenAI 兼容协议。完整真实验收见 [端到端测试方法](docs/e2e-test.md)。

| 模块 | 职责 |
| --- | --- |
| `channel.py` | 接收消息元数据、忽略不支持的消息、发送执行结果 |
| `agent.py` | 单 Agent、外部系统提示词、结构化输出、受限历史检索 |
| `models.py` | 消息、消费、纠错、偏好及意图模型 |
| `knowledge.py` | 分类树、规则、表结构和长期偏好的读取与校验 |
| `service.py` | 短期上下文、会话顺序、业务校验、执行和确定性反馈 |
| `tools.py` | 固定 CLI 命令、超时清理、结果解析与修改核对 |
| `storage.py` | 原子 JSON 保存与写入去重日志 |
| `config.py`、`__main__.py` | 配置、装配、长连接和诊断入口 |

架构说明见 [系统架构与工作流程](docs/系统架构与工作流程.md)。当前只运行一个应用实例，不引入多 Agent、数据库、Redis、消息队列、向量库或长期对话服务。

## 故障排查

- **提示词为空或无法读取**：将正式提示词放入 `AGENT_SYSTEM_PROMPT_PATH`，确认 UTF-8 编码，再重启。
- **知识校验失败**：检查表 ID、分类树版本、规则路径和 `schema.json`，不得自动新建分类选项。
- **CLI 查询失败**：先运行 `--check-remote`，再检查 CLI 身份、登录态、表格权限与网络。
- **Keychain 不可用**：在能够访问 macOS 钥匙串的本机终端运行；应用不修改 CLI 凭证存储方式。
- **消息理解失败或超时**：检查方舟配置、Function Calling 支持和网络。此时尚未执行写入。
- **纠错提示被外部修改或结果待核对**：检查表格中的目标账单，不继续覆盖或重复创建。
- **机器人没有回复**：非记账、非文字及已处理消息会静默忽略；实际消费消息还应检查长连接、发布版本、权限和 `reply_failed` 日志。

应用日志不主动记录密钥、消费正文、模型响应或 CLI 原始错误。

## 参考

- [Lark Channel SDK](https://github.com/larksuite/channel-sdk-python) 与 [去重架构](https://github.com/larksuite/channel-sdk-python/blob/main/docs/dedup-architecture.md)
- [Pydantic AI 结构化输出](https://pydantic.dev/docs/ai/core-concepts/output/)
- CLI 参数以本机 `lark-cli base <命令> --help` 为准，当前核验版本为 `1.0.93`。

# 飞书记账 Agent：只读查询最小框架

本项目运行一个 Python 应用，通过飞书机器人接收「查询最近 10 条消费记录」等指令，由 Pydantic AI 调用火山方舟识别数量，再用官方 `lark-cli` 读取多维表格，最后回复查询结果。

当前阶段只实现最近 N 条消费记录查询。没有新增、修改、删除记录的代码，也没有消费分类、历史知识检索或长期会话记忆。

## 处理链路

```text
飞书文字消息
  → lark-channel-sdk（WebSocket、消息规范化、去重、同会话串行）
  → Pydantic AI Agent + 火山方舟（识别意图和 N）
  → recent_expenses(n)
  → lark-cli base +record-list（服务端按记账日期降序，limit=N）
  → Python 校验与排版
  → lark-channel-sdk 回复原消息
```

Python 应用只有一个进程。SDK 可使用内部线程维护连接；每次查询会启动短生命周期的 CLI 子进程，不阻塞异步消息循环，不需要独立服务。

## 当前查询规则

- 支持 N 为 1–100 的整数；自然语言未指定数量时默认 10 条。中文数字由模型识别。
- 按记账日期降序取前 N 条，先在服务端排序再限制数量。日期相同时不承诺固定次序。
- 目标表应是消费明细表。本阶段不判断收支类型；如果表内混有收入，也会被当作明细返回。
- 配置 `BITABLE_VIEW_ID` 时保留该视图的筛选范围，回复注明视图 ID；留空时查询整张表。两种模式都按记账日期降序，覆盖本次查询的视图排序，不修改视图配置。
- 整表模式自动筛选记账日期非空的行。视图模式不额外传筛选条件，以免覆盖视图筛选；若返回空日期，应用提示在视图中设置日期非空条件，不返回不完整的最近 N 条结果。
- 实际不足 N 条时返回已有记录；金额为空时显示「金额为空」，不当作 0。
- 回复展示日期、金额、具体内容，金额保留两位小数；币种沿用原表语义，不额外推断。
- 暂不支持按日期范围、分类、金额筛选，也不支持统计、写入和混合写入请求。模型未调用查询工具时返回固定的能力说明。
- 模型只接收查询完成摘要，消费明细保留在 Python 内并直接排版。模型生成的自由文本不会作为消费查询结果回复。
- 每条消息单独运行 Agent，没有上下文记忆。SDK 保留消息去重与同会话队列，关闭连续文字合并，避免多条查询被合成一次。

## 环境和安装

需要 Python 3.11 或更高版本、官方 `lark-cli`，以及支持 Function Calling 的火山方舟模型或推理接入点。本次开发环境为 Python 3.12，CLI 为 1.0.93。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock.txt
```

`requirements.lock.txt` 固定本次验证使用的依赖，并以可编辑方式安装项目及测试工具。核心依赖也固定在 `pyproject.toml`，包括 `lark-channel-sdk==1.4.0` 和 `pydantic-ai-slim[openai]==2.54.0`。应用中不需要 Node.js 通信层。

已有本地 `.venv` 时，直接激活即可。

## 配置

在项目根目录执行：

```bash
cp .env.example .env
```

编辑 `.env`，填写下表配置。应用从启动时的工作目录读取 `.env`，环境变量优先；因此运行命令前应进入项目根目录。`.env` 已加入忽略规则。

| 配置项 | 用途 | 默认值 |
| --- | --- | --- |
| `LARK_APP_ID` | 飞书机器人应用 ID | 必填 |
| `LARK_APP_SECRET` | 飞书机器人应用密钥 | 必填 |
| `ARK_API_KEY` | 火山方舟 API Key | 必填 |
| `ARK_MODEL` | 已开通且支持 Function Calling 的模型 ID 或推理接入点 ID | 必填 |
| `ARK_BASE_URL` | 方舟 OpenAI 兼容接口地址 | `https://ark.cn-beijing.volces.com/api/v3` |
| `BITABLE_BASE_TOKEN` | 目标 Base token | 必填 |
| `BITABLE_TABLE_ID` | 目标数据表 ID | 必填 |
| `BITABLE_VIEW_ID` | 可选视图 ID；填写时按视图筛选范围查询，留空查询整表 | 空 |
| `BITABLE_DATE_FIELD` | 日期字段名称，应为日期类型 | `记账日期` |
| `BITABLE_AMOUNT_FIELD` | 金额字段名称，应为数字类型 | `金额` |
| `BITABLE_DESCRIPTION_FIELD` | 描述字段名称，应为文本类型 | `具体` |
| `LARK_CLI_PATH` | CLI 可执行文件名或绝对路径 | `lark-cli` |
| `LARK_CLI_IDENTITY` | CLI 操作者身份，`user` 或 `bot` | `user` |
| `CLI_TIMEOUT_SECONDS` | 单次 CLI 查询超时 | `30` |
| `AGENT_TIMEOUT_SECONDS` | 单条消息从模型理解到查询完成的总超时 | `90` |
| `DISPLAY_TIMEZONE` | 回复中日期的时区 | `Asia/Shanghai` |

例如链接 `https://example.feishu.cn/base/bascnXXX?table=tblXXX&view=vewXXX` 中，`bascnXXX` 为 Base token，`tblXXX` 为数据表 ID，`vewXXX` 为视图 ID。Wiki 或分享链接应先通过 CLI 的 `base +url-resolve --url '<链接>' --as user` 解析。字段配置使用实际名称，不是 `field_id`；表格字段改名后同步修改配置。

### 飞书机器人

1. 在飞书开发者后台准备自建应用，开启机器人能力。
2. 开通机器人发送消息和接收消息所需权限；SDK 快速入门列出了 `im:message`、`im:message:send_as_bot`。根据使用方式开通单聊接收或群聊 @ 消息接收权限。
3. 将事件接收方式设为长连接，订阅 `im.message.receive_v1`。
4. 发布版本，使权限和事件配置生效，并将应用可用范围设置为实际使用者。
5. 启动本地应用后，在机器人单聊中测试；在群聊中需要 @ 机器人。

如开发者后台要求先建立连接再保存事件，先完成其余配置并启动应用，再回后台保存事件订阅。

### CLI 登录与表格权限

CLI 登录态与机器人配置相互独立。`.env` 中的 `LARK_APP_ID`、`LARK_APP_SECRET` 不会替换 CLI 已保存的配置，应用不会自动发起登录或切换身份。

先检查 CLI：

```bash
lark-cli --version
lark-cli auth status --json --verify
lark-cli base +record-list --help
```

尚未配置 CLI 时，在交互终端运行 `lark-cli config init`，根据提示完成配置。默认采用用户身份；该用户必须能读取目标表。需要补充授权时，按 CLI 返回的 `missing_scopes` 申请对应读取权限：

```bash
lark-cli auth login --scope '<CLI 提示缺少的读取 scope>'
```

不要为本项目申请写入权限。如果现有 CLI 登录态已拥有写权限，应用仍只执行 `base +record-list`。如明确改用 `bot`，需要单独配置 CLI 应用权限及表格可见范围，并将 `LARK_CLI_IDENTITY` 设置为 `bot`。

### 火山方舟

配置方舟 API Key 和实际可用的模型 ID/推理接入点 ID。应用使用 Pydantic AI 的 `OpenAIChatModel`，请求 Chat Completions 接口并通过 Function Calling 调用查询工具，不使用 OpenAI 的模型服务。方舟模型需要支持此调用方式。

默认地址对应方舟模型推理 API；其他方舟产品的接入地址应按所用服务的官方说明配置。

## 启动与分层排查

```bash
# 1. 检查配置是否齐全及 CLI 是否存在；不会连接任何远端服务。
bookkeeping-agent --check

# 2. 跳过模型和机器人，仅查询多维表格。
bookkeeping-agent --query 10

# 3. 通过真实模型识别指令、调用真实 CLI，在终端显示结果。
bookkeeping-agent --ask '查询最近 10 条消费记录'

# 4. 启动机器人长连接，按 Ctrl+C 退出。
bookkeeping-agent
```

所有模式共用一套配置，目前均要求必填项齐全。也可用 `python -m bookkeeping_agent` 替代入口命令。

## 模块边界

| 文件 | 职责 |
| --- | --- |
| `src/bookkeeping_agent/channel.py` | 创建 Channel、处理消息和发送结果；复用 SDK 的去重及队列 |
| `src/bookkeeping_agent/agent.py` | 创建 Pydantic AI Agent、注册唯一查询工具、组织确定性回复 |
| `src/bookkeeping_agent/tools.py` | 校验 N、构造固定 CLI 参数、异步执行、解析并校验结果 |
| `src/bookkeeping_agent/config.py` | `.env` / 环境变量加载与配置校验 |
| `src/bookkeeping_agent/__main__.py` | 装配组件、启动及三个诊断入口 |
| `tests/` | 工具边界、模型调用协议和 SDK 消息链路的离线测试 |

CLI 参数通过 `asyncio.create_subprocess_exec` 逐项传入，不使用 Shell。模型只能传 N，不能传命令、表格标识、字段、排序或过滤条件。每次查询使用独立临时目录，成功、失败、取消和超时后均清理记录及 manifest；超时和取消会结束 CLI 子进程。

## 测试

```bash
python -m pytest -q
ruff check src tests
ruff format --check src tests
```

离线测试覆盖：真实 Channel SDK 的消息规范化和去重 → 真实 Pydantic AI 工具调用 → 模拟 CLI 子进程 → 回复参数。另有 OpenAI 兼容 HTTP 协议测试，检查方舟地址、工具参数、工具返回消息和二轮模型请求。

测试不连接真实飞书、方舟或表格。离线测试通过代表本地组件和接口封装可运行，不代表真实模型已正确理解中文数量，也不代表凭证、权限或 WebSocket 已通过现场联调。完整操作见 [端到端测试方法](docs/e2e-test.md)。

## 常见问题

- **配置缺失或无效**：根据提示字段补齐 `.env`，确认从项目根目录启动。错误只显示配置项名称，不显示密钥。
- **CLI 查询失败**：先运行 `--query 10` 定位；再用 `lark-cli auth status --json --verify` 检查身份与登录态，并核对表格、字段和读取权限。
- **CLI 提示 Keychain 不可用**：在能访问系统钥匙串的本机交互终端运行；本项目不会修改 CLI 的凭证存储方式。
- **模型调用失败或超时**：确认 API Key、模型 ID、接入地址、Function Calling 支持及网络连接。用 `--ask` 将消息通信因素排除。
- **机器人没有回复**：确认应用发布、可用范围、长连接、事件订阅、接收和发送权限；群内需 @。避免同时启动多个连接同一机器人的实例。
- **日志有 `reply_failed`**：查询可能已完成，但飞书未确认发送成功。检查消息权限及连接；不会把发送失败记为成功。
- **返回数据格式异常**：确认日期、金额、描述字段名称和类型正确。CLI NDJSON 日期应带时区；不会把解析失败当作空表。

应用日志记录消息 ID、错误类型和发送状态，不主动记录消息正文、消费明细、密钥或 CLI 原始错误。发送长文本的分段与重试交给 SDK。

## 官方参考

- [Lark Channel SDK](https://github.com/larksuite/channel-sdk-python) 与 [快速入门](https://github.com/larksuite/channel-sdk-python/blob/main/docs/quickstart.md)
- [Pydantic AI：OpenAI 兼容模型](https://pydantic.dev/docs/ai/models/openai/)
- [火山方舟：Base URL 与鉴权](https://docs.volcengine.com/docs/ark/base-url-and-authentication?lang=en)
- CLI 的版本与参数以本机 `lark-cli base +record-list --help` 为准；本项目使用 `--sort-json`、`--filter-json`、`--field-id` 和 NDJSON 导出协议。

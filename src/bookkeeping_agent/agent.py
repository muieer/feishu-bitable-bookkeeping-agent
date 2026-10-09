from dataclasses import dataclass
from zoneinfo import ZoneInfo

from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits

from .config import Settings
from .tools import BitableQueryTool, ExpenseRecord, RecordCount

HELP_TEXT = (
    "当前仅支持查询最近 1–100 条消费记录，例如：查询最近 10 条消费记录。"
    "未指定数量时默认查询 10 条；暂不支持记账、修改、删除、分类、筛选或统计。"
)


@dataclass
class QueryContext:
    tool: BitableQueryTool
    records: list[ExpenseRecord] | None = None
    requested: int = 10


def build_agent(settings: Settings, model: Model | None = None) -> Agent:
    if model is None:
        model = OpenAIChatModel(
            settings.ark_model,
            provider=OpenAIProvider(
                base_url=settings.ark_base_url, api_key=settings.ark_api_key.get_secret_value()
            ),
        )
    agent = Agent(
        model,
        deps_type=QueryContext,
        instructions=(
            "你是只读消费记录查询助手。仅在用户请求查看最近N条消费记录时，调用"
            "recent_expenses工具一次。将中文或阿拉伯数字转成整数N；未指定数量默认10。"
            "N必须为1到100的整数，超范围或不明确时不要调用工具，不得自动截断。"
            "仅支持配置指定的数据表或视图范围内按记账日期降序取前N条，不支持临时指定筛选条件"
            "或统计；用户请求这些条件时不得擅自忽略条件调用工具。"
            "新增、修改、删除、分类、历史知识检索、闲聊或混合写入请求均不调用工具。"
            "不能执行命令，不能更改目标表或字段。工具完成后简短结束，不能编造消费明细。"
        ),
        retries=1,
    )

    @agent.tool
    async def recent_expenses(ctx: RunContext[QueryContext], n: RecordCount = 10) -> str:
        """按记账日期降序查询最近 n 条消费记录，n 为 1–100 的整数。"""
        ctx.deps.records = await ctx.deps.tool.recent_expenses(n)
        ctx.deps.requested = n
        # Original records stay in Python. The model receives only completion metadata.
        return f"查询成功，返回{len(ctx.deps.records)}条，应用会直接展示原始查询结果。"

    return agent


def format_records(
    records: list[ExpenseRecord], requested: int, timezone: str, view_id: str = ""
) -> str:
    if not records:
        if view_id:
            return f"指定视图（{view_id}）中未找到消费记录。"
        return "未找到有记账日期的消费记录。"
    lines = [f"最近 {requested} 条消费记录（实际返回 {len(records)} 条，按记账日期降序）："]
    if view_id:
        lines.append(f"查询范围：指定视图（{view_id}）。")
    for index, record in enumerate(records, 1):
        date = record.date.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d")
        amount = f"{record.amount:.2f}" if record.amount is not None else "金额为空"
        description = " ".join((record.description or "无描述").split())
        lines.append(f"{index}. {date} | {amount} | {description}")
    lines.append("仅查询已填写记账日期的记录；本次未修改任何数据。")
    return "\n".join(lines)


class QueryService:
    def __init__(self, settings: Settings, tool: BitableQueryTool, model: Model | None = None):
        self.settings = settings
        self.tool = tool
        self.agent = build_agent(settings, model)

    async def reply(self, text: str) -> str:
        context = QueryContext(self.tool)
        await self.agent.run(
            text,
            deps=context,
            usage_limits=UsageLimits(
                request_limit=3,
                tool_calls_limit=1,
            ),
        )
        if context.records is None:
            return HELP_TEXT
        return format_records(
            context.records,
            context.requested,
            self.settings.display_timezone,
            self.settings.bitable_view_id,
        )

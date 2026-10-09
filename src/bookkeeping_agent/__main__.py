import argparse
import asyncio
import logging
import shutil
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from .channel import build_channel, register_handlers
from .config import Settings
from .knowledge import Knowledge, KnowledgeError, read_system_prompt
from .models import IncomingMessage
from .service import BookkeepingService
from .tools import BitableTool


async def run(settings: Settings, args: argparse.Namespace) -> None:
    tool = BitableTool(settings)
    knowledge = Knowledge(settings)
    if args.check or args.check_remote:
        print("本地配置、分类树、个人规则和表格结构校验通过，已找到 lark-cli。")
        try:
            read_system_prompt(settings.agent_system_prompt_path)
            print(f"系统提示词已准备：{settings.agent_system_prompt_path.resolve()}")
        except KnowledgeError as exc:
            print(f"Agent 尚不能启动：{exc}")
        if args.check_remote:
            knowledge.validate_schema(await tool.field_schema())
            print("远端表格字段类型及分类选项校验通过；未写入数据。")
        return
    if args.query is not None:
        records = await tool.recent_expenses(args.query)
        for record in records:
            print(
                f"{record.date.astimezone(ZoneInfo(settings.display_timezone)).date()} | "
                f"{record.amount} | {record.description}"
            )
        if not records:
            print("未找到消费记录。")
        return
    service = BookkeepingService(settings, tool)
    if args.ask is not None or args.interactive:
        print("终端记账会实际写入配置的多维表格。")
        while True:
            text = args.ask if args.ask is not None else await asyncio.to_thread(input, "消费 > ")
            if not text.strip():
                if args.interactive:
                    continue
                return
            message = IncomingMessage(
                message_id="terminal_" + uuid.uuid4().hex,
                user_id="terminal",
                chat_id="terminal",
                sent_at=datetime.now(ZoneInfo(settings.display_timezone)),
                text=text,
            )
            response = await service.reply(message)
            if response:
                print(response)
            if args.ask is not None:
                return
    channel = build_channel(settings)
    register_handlers(channel, service)
    try:
        await channel.connect()
    finally:
        await channel.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="飞书记账智能体：自然语言记账与即时纠错")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="检查本地配置和知识文件，不调用模型")
    group.add_argument("--check-remote", action="store_true", help="同时只读核对远端字段与分类")
    group.add_argument("--query", type=int, help="只读诊断：跳过模型查询 N 条最近消费")
    group.add_argument("--ask", help="在终端处理一条消息，会实际记账")
    group.add_argument("--interactive", action="store_true", help="终端连续会话，支持追问和纠错")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("bookkeeping_agent").setLevel(logging.INFO)
    try:
        settings = Settings()
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, error["loc"])) for error in exc.errors())
        parser.exit(2, f"配置缺失或无效：{fields}。请参照 .env.example 配置。\n")
    if not shutil.which(settings.lark_cli_path):
        parser.exit(2, "找不到 lark-cli，请安装或设置 LARK_CLI_PATH。\n")
    try:
        asyncio.run(run(settings, args))
    except (KeyboardInterrupt, EOFError):
        pass
    except KnowledgeError as exc:
        parser.exit(2, f"知识或提示词配置无效：{exc}\n")
    except Exception as exc:  # noqa: BLE001 -- Avoid printing raw credential/provider errors.
        parser.exit(1, f"运行失败（{type(exc).__name__}）。请参照 README 排查配置和权限。\n")


if __name__ == "__main__":
    main()

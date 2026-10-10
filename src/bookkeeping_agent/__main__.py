import argparse
import asyncio
import logging
import shutil
import uuid
from contextlib import suppress
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from .channel import build_channel, register_handlers
from .config import Settings
from .knowledge import Knowledge, KnowledgeError, read_system_prompt
from .logging_setup import configure_logging
from .models import IncomingMessage
from .service import BookkeepingService
from .tools import BitableTool
from .tracing import LOG_FAILED, LOG_FAILED_SUFFIX, TraceStore, emit


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
    try:
        await run_service(settings, args, service)
    finally:
        await service.aclose()


async def run_service(settings: Settings, args: argparse.Namespace, service: BookkeepingService):
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
            trace = service.traces.new(message.message_id)
            with service.traces.scope(trace):
                emit("message.terminal_received", message=message)
                response = await service.reply(message)
                emit("terminal.output", response=response)
                if trace.failed and response:
                    response = (
                        (
                            response
                            if LOG_FAILED_SUFFIX in response
                            else response + LOG_FAILED_SUFFIX
                        )
                        if trace.mutation_submitted
                        else LOG_FAILED
                    )
                if response:
                    print(response)
                emit("message.end", outcome="terminal_output" if response else "silent")
            if args.ask is not None:
                return
    channel = build_channel(settings)
    register_handlers(channel, service)
    logger = logging.getLogger("bookkeeping_agent")

    async def observe_ready():
        await channel.wait_ready()
        logger.info("channel_connected")

    ready_task = asyncio.create_task(observe_ready())
    try:
        logger.info("channel_connecting")
        await channel.connect()
    finally:
        ready_task.cancel()
        with suppress(asyncio.CancelledError):
            await ready_task
        await channel.disconnect()
        logger.info("channel_disconnected")


def main() -> None:
    parser = argparse.ArgumentParser(description="飞书记账智能体：自然语言记账与即时纠错")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="检查本地配置和知识文件，不调用模型")
    group.add_argument("--check-remote", action="store_true", help="同时只读核对远端字段与分类")
    group.add_argument("--query", type=int, help="只读诊断：跳过模型查询 N 条最近消费")
    group.add_argument("--ask", help="在终端处理一条消息，会实际记账")
    group.add_argument("--interactive", action="store_true", help="终端连续会话，支持追问和纠错")
    args = parser.parse_args()
    try:
        settings = Settings()
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, error["loc"])) for error in exc.errors())
        parser.exit(2, f"配置缺失或无效：{fields}。请参照 .env.example 配置。\n")
    try:
        configure_logging(settings.log_file_path)
        TraceStore(settings)
    except OSError:
        parser.exit(
            2, "无法创建或打开运行日志，请检查 LOG_FILE_PATH、MESSAGE_LOG_DIR 和目录写入权限。\n"
        )
    logger = logging.getLogger("bookkeeping_agent")
    logger.info(
        "application_started log_file=%s message_log_dir=%s",
        settings.log_file_path.resolve(),
        settings.message_log_dir.resolve(),
    )
    if not shutil.which(settings.lark_cli_path):
        logger.error("startup_failed kind=MissingCLI")
        parser.exit(2, "找不到 lark-cli，请安装或设置 LARK_CLI_PATH。\n")
    try:
        asyncio.run(run(settings, args))
    except (KeyboardInterrupt, EOFError):
        pass
    except KnowledgeError as exc:
        logger.error("startup_failed kind=%s", type(exc).__name__)
        parser.exit(2, f"知识或提示词配置无效：{exc}\n")
    except Exception as exc:  # noqa: BLE001 -- Avoid printing raw credential/provider errors.
        logger.error("application_failed kind=%s", type(exc).__name__)
        parser.exit(1, f"运行失败（{type(exc).__name__}）。请参照 README 排查配置和权限。\n")


if __name__ == "__main__":
    main()

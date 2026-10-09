import argparse
import asyncio
import logging
import shutil

from pydantic import ValidationError

from .agent import QueryService, format_records
from .channel import build_channel, register_handlers
from .config import Settings
from .tools import BitableQueryTool


async def run(settings: Settings, args: argparse.Namespace) -> None:
    tool = BitableQueryTool(settings)
    if args.check:
        print("配置校验通过，已找到 lark-cli；尚未验证远端凭证和读取权限。")
        return
    if args.query is not None:
        print(
            format_records(
                await tool.recent_expenses(args.query),
                args.query,
                settings.display_timezone,
                settings.bitable_view_id,
            )
        )
        return
    service = QueryService(settings, tool)
    if args.ask:
        print(await asyncio.wait_for(service.reply(args.ask), settings.agent_timeout_seconds))
        return
    channel = build_channel(settings)
    register_handlers(channel, service, settings.agent_timeout_seconds)
    try:
        await channel.connect()
    finally:
        await channel.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="飞书记账 Agent：只读查询最近消费记录")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="仅验证本地配置和 CLI 路径")
    group.add_argument("--query", type=int, help="跳过模型，直接只读查询 N 条记录")
    group.add_argument("--ask", help="运行 Agent 查询，在终端显示结果，不发送飞书消息")
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
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001 -- Avoid printing raw credential/provider errors.
        parser.exit(1, f"运行失败（{type(exc).__name__}）。请参照 README 排查配置和权限。\n")


if __name__ == "__main__":
    main()

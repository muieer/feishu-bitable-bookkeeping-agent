import os
import subprocess
import sys
from pathlib import Path

import pytest


def cli_env(settings):
    env = os.environ.copy()
    env.update({key.upper(): str(value) for key, value in settings.model_dump(mode="json").items()})
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env["LARK_CLI_PATH"] = sys.executable
    return env


@pytest.mark.parametrize("custom_path", [False, True])
def test_cli_logs_to_console_and_file_and_preserves_existing_content(
    settings, tmp_path, custom_path
):
    env = cli_env(settings)
    path = tmp_path / ("custom/nested/运行.log" if custom_path else "logs/service.log")
    if custom_path:
        env["LOG_FILE_PATH"] = str(path)
    else:
        env.pop("LOG_FILE_PATH", None)
    script = """
import logging
from bookkeeping_agent.__main__ import main
main()
logging.getLogger('bookkeeping_agent.service').warning('agent_failed kind=UnexpectedModelBehavior')
logging.getLogger('bookkeeping_agent.channel').info('reply_sent message_id=om_test')
logging.getLogger('third_party').info('hidden_info')
logging.getLogger('Lark').warning('connection_probe_failed')
"""
    first = subprocess.run(
        [sys.executable, "-c", script, "--check"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    assert first.returncode == 0, first.stderr
    content = path.read_text(encoding="utf-8")
    assert f"application_started log_file={path}" in content
    for event in ("agent_failed", "reply_sent", "connection_probe_failed"):
        assert event in content and event in first.stdout + first.stderr
    assert "hidden_info" not in content
    assert "本地配置、分类树" in first.stdout and "本地配置、分类树" not in content
    second = subprocess.run(
        [sys.executable, "-m", "bookkeeping_agent", "--check"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    assert second.returncode == 0, second.stderr
    appended = path.read_text(encoding="utf-8")
    assert appended.startswith(content)
    assert appended.count("application_started") == 2


def test_cli_reports_unusable_log_path(settings, tmp_path):
    env = cli_env(settings)
    env["LOG_FILE_PATH"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "bookkeeping_agent", "--check"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2
    assert "无法创建或打开运行日志" in result.stderr
    assert "本地配置、分类树" not in result.stdout


def test_cli_stops_before_startup_when_message_directory_is_unusable(settings, tmp_path):
    env = cli_env(settings)
    blocked = tmp_path / "blocked_directory"
    blocked.write_text("not a directory")
    env["MESSAGE_LOG_DIR"] = str(blocked)
    result = subprocess.run(
        [sys.executable, "-m", "bookkeeping_agent", "--check"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2 and "MESSAGE_LOG_DIR" in result.stderr
    assert "本地配置、分类树" not in result.stdout

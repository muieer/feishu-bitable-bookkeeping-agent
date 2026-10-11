"""Human-readable progress projected from actual business events."""

import logging
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)
_observer: ContextVar = ContextVar("bookkeeping_progress", default=None)


@contextmanager
def progress_scope(observer):
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


def observe(stage, details):
    observer = _observer.get()
    if observer is not None:
        try:
            observer(stage, details)
        except Exception as exc:  # noqa: BLE001 -- Presentation never controls a write.
            logger.warning("progress_observer_failed kind=%s", type(exc).__name__)


def category_label(category):
    return " / ".join(filter(None, (category.level1, category.level2, category.level3)))


def body_text(content, *, muted=False, text_size="normal"):
    text = {
        "tag": "plain_text",
        "content": " ".join(line.strip() for line in content.splitlines() if line.strip()),
        "text_size": text_size,
    }
    if muted:
        text["text_color"] = "grey"
    return text


def body_element(content, *, muted=False, text_size="normal"):
    return {"tag": "div", "text": body_text(content, muted=muted, text_size=text_size)}


@dataclass
class Step:
    key: str
    active: str
    completed: str
    status: str = "running"


@dataclass
class ProgressState:
    queued: bool = False
    clock: Callable[[], float] = time.monotonic
    received_at: float = field(init=False)
    execution_at: float | None = None
    ended_at: float | None = None
    wait_seconds: int = 0
    steps: list[Step] = field(default_factory=list)
    current: str = "正在理解这条消息。"
    detail: str = ""
    outcome: str = "failure"
    action: str | None = None
    expense: object = None
    preference: object = None
    confirmed: bool = False
    prepared: bool = False
    completed_response: str | None = None
    response: str | None = None
    title: str = "处理结果"
    status: str = "处理未完成"
    batch_total: int = 0
    batch_processed: int = 0
    batch_counts: dict = field(default_factory=dict)
    batch_stage_counts: dict = field(default_factory=dict)
    batch_interrupted: bool = False
    batch_attention: bool = False

    def __post_init__(self):
        self.received_at = self.clock()
        if self.queued:
            self.current = "正在等待上一条消息处理完成。"

    def start(self):
        if self.queued:
            self.wait_seconds = int(self.clock() - self.received_at)
        self.queued = False
        self.execution_at = self.clock()
        self.current = "正在理解这条消息。"

    def begin(self, key, active, completed, detail=""):
        previous = next((s for s in reversed(self.steps) if s.key == key), None)
        if previous is None or previous.status != "running":
            self.steps.append(Step(key, active, completed))
        # Limit presentation history, never diagnostic history.
        self.steps[:] = self.steps[-20:]
        self.current, self.detail = active, detail

    def complete(self, key):
        for step in reversed(self.steps):
            if step.key == key and step.status == "running":
                step.status = "completed"
                return

    def fail(self, key):
        for step in reversed(self.steps):
            if step.key == key and step.status == "running":
                step.status = "failed"
                return

    def on_event(self, stage, data):
        if self.ended_at is not None:
            return
        if stage == "batch.started":
            self.batch_total = data["total"]
        if self.batch_total:
            self.on_batch_event(stage, data)
            return
        if stage == "decision.context":
            self.begin(
                "understand",
                "正在理解消费内容、金额和日期，并根据分类规则和个人偏好确定分类。",
                "已理解消息内容",
            )
        elif stage == "history.query":
            self.begin("history", "正在查询相似消费的历史分类。", "已查询历史分类")
        elif stage == "history.valid_cases":
            self.complete("history")
            if data["cases"]:
                self.begin(
                    "history_match",
                    "已找到可用的历史分类，正在核对是否适用于这笔消费。",
                    "已核对历史分类的适用性",
                )
            else:
                self.current = "未找到可用的历史分类，正在确认需要补充的信息。"
        elif stage == "history.error":
            self.fail("history")
            self.current = "历史分类查询未完成，正在确认需要补充的信息。"
        elif stage == "decision.branch":
            self.action = data["decision"].action
            self.complete("understand")
            self.complete("history_match")
        elif stage == "expense.merged":
            self.begin(
                "validate", "正在核对金额、日期和分类是否完整有效。", "已校验金额、日期和分类"
            )
        elif stage == "expense.validated":
            self.expense = data["expense"]
            self.complete("validate")
        elif stage == "table.queue":
            self.current = "正在等待记账表格可用。"
            self.detail = ""
        elif stage == "table.lock_acquired":
            detail = ""
            if self.expense:
                detail = f"本笔消费：{self.expense.amount:.2f} 元 · {category_label(self.expense.category)}。"
            self.begin(
                "schema", "正在检查记账表格的字段和分类选项。", "已检查表格字段和分类选项", detail
            )
        elif stage == "table.schema_validated":
            self.complete("schema")
        elif stage == "table.correction_check":
            self.begin("correction", "正在核对最近一笔账单及需要修改的内容。", "已核对最近一笔账单")
        elif stage == "table.correction_preflight":
            self.complete("correction")
        elif stage == "cli.start" and data.get("write"):
            if self.action == "update":
                self.begin("write", "正在修改最近一笔账单。", "已提交账单修改")
            else:
                self.begin("write", "正在将这笔消费写入表格。", "已写入账单")
        elif stage == "table.update_verification_start":
            self.complete("write")
            self.begin("verify", "修改已提交，正在核对表格中的实际结果。", "已确认修改结果")
        elif stage == "table.prepared":
            self.prepared = True
            self.expense = data["expense"]
        elif stage == "table.confirmed":
            self.confirmed = True
            self.expense = data["expense"]
            self.complete("write")
            self.complete("verify")
        elif stage == "preference.write_planned":
            self.begin("preference", "正在保存这条分类偏好。", "已保存分类偏好")
        elif stage == "preference.saved":
            self.preference = data["preference"]
            self.complete("preference")
        elif stage == "business.completed":
            self.outcome = "success"
            self.completed_response = data["response"]
        elif stage == "business.clarify":
            self.outcome = "clarify"
        elif stage == "message.ignored":
            self.outcome = "ignore"
        elif stage in {"decision.timeout", "decision.error"}:
            self.fail("understand")
            self.title = "理解消息超时" if stage == "decision.timeout" else "消息理解失败"
        elif stage == "expense.validation_failed":
            self.fail("validate")
        elif stage == "table.preflight_failed":
            self.fail("correction" if any(s.key == "correction" for s in self.steps) else "schema")
        elif stage == "table.write_failed":
            self.fail("verify" if any(s.key == "verify" for s in self.steps) else "write")
        elif stage == "preference.save_failed":
            self.fail("preference")

    def on_batch_event(self, stage, data):
        stages = {
            "decision.context": ("understand", "理解消费信息与分类"),
            "history.query": ("history", "查询历史分类"),
            "expense.merged": ("validate", "校验金额、日期和分类"),
            "table.lock_acquired": ("schema", "检查表格字段和分类选项"),
            "table.correction_check": ("correction", "核对待修改账单"),
            "table.write_planned": ("write", "写入或修改账单"),
            "table.update_verification_start": ("verify", "核对修改结果"),
            "preference.write_planned": ("preference", "保存分类偏好"),
        }
        completions = {
            "decision.branch": "understand",
            "history.valid_cases": "history",
            "expense.validated": "validate",
            "table.schema_validated": "schema",
            "table.correction_preflight": "correction",
            "table.confirmed": "write",
            "preference.saved": "preference",
        }
        failures = {
            "decision.error": "understand",
            "history.error": "history",
            "expense.validation_failed": "validate",
            "table.preflight_failed": "schema",
            "table.write_failed": "write",
            "preference.save_failed": "preference",
        }
        if stage in stages:
            key, label = stages[stage]
            step = next((step for step in self.steps if step.key == key), None)
            if step is None:
                step = Step(key, label, label)
                self.steps.append(step)
            step.status = "running"
            self.current = f"正在{label}；已处理 {self.batch_processed}/{self.batch_total} 笔。"
        if stage in completions:
            key = completions[stage]
            self.batch_stage_counts[key] = self.batch_stage_counts.get(key, 0) + 1
            step = next((step for step in self.steps if step.key == key), None)
            if step:
                step.completed = f"{step.active}：已完成 {self.batch_stage_counts[key]} 笔"
                self.complete(key)
        if stage in failures:
            self.fail(failures[stage])
        if stage == "table.confirmed":
            self.confirmed = True
            verification = next(
                (step for step in self.steps if step.key == "verify" and step.status == "running"),
                None,
            )
            if verification:
                self.batch_stage_counts["verify"] = self.batch_stage_counts.get("verify", 0) + 1
                verification.completed = (
                    f"核对修改结果：已完成 {self.batch_stage_counts['verify']} 笔"
                )
            self.complete("verify")
        elif stage == "table.prepared":
            self.prepared = True
        elif stage in {"batch.item_started", "batch.item_completed"}:
            self.batch_processed = data["processed"]
            self.current = f"已处理 {self.batch_processed}/{self.batch_total} 笔账单。"
        elif stage in {"batch.completed", "batch.interrupted"}:
            self.batch_interrupted = stage == "batch.interrupted"
            self.batch_attention = data.get("attention", False)
            self.batch_counts = data["counts"]
            self.completed_response = data["response"]
            self.outcome = "batch"

    def finish(self, response, *, submitted=False, log_failed=False, interrupted=False):
        if self.ended_at is None:
            self.ended_at = self.clock()
        self.response = response
        exact_success = self.outcome == "success" and response == self.completed_response
        if self.outcome == "batch" and (interrupted or self.batch_interrupted):
            self.title = "批量处理已中断"
            self.status = "结果待核对" if self.batch_counts.get("uncertain") else "已中断"
        elif self.outcome == "batch" and not log_failed:
            self.title = "批量处理结果"
            self.status = (
                "结果待核对"
                if self.batch_counts.get("uncertain")
                else "部分完成 · 待补充"
                if (self.batch_counts.get("pending") or self.batch_attention)
                and self.batch_counts.get("success")
                else "待补充"
                if self.batch_counts.get("pending") or self.batch_attention
                else "处理未完成"
                if self.batch_counts.get("failed")
                else "已完成"
            )
        elif interrupted:
            self.status = (
                "已完成 · 有提示" if self.confirmed else "结果待核对" if submitted else "已中断"
            )
            self.title = (
                "表格写入已成功"
                if self.confirmed
                else "写入结果尚未确认"
                if submitted
                else "处理已中断"
            )
        elif exact_success:
            self.status = "已完成"
            self.title = (
                "已保存分类偏好"
                if self.preference
                else "准备记账"
                if self.prepared
                else "已修改"
                if self.action == "update"
                else "已记账"
            )
        elif log_failed:
            self.status = (
                "已完成 · 有提示"
                if self.confirmed or self.outcome == "success"
                else "结果待核对"
                if submitted
                else "处理未完成"
            )
            self.title = "处理结果"
        elif self.confirmed:
            self.status, self.title = "已完成 · 有提示", "表格写入已成功"
        elif self.outcome == "clarify":
            self.status, self.title = "待补充", "请补充或确认"
        elif self.outcome == "ignore":
            self.status, self.title = "已结束", "本次未记账"
        elif submitted:
            self.status, self.title = "结果待核对", "写入结果尚未确认"
        if self.status == "结果待核对" and "勿重复" not in self.response:
            self.response += "\n确认前请勿重复发送。"
        for step in self.steps:
            if step.status == "running":
                step.status = "unfinished"
        self.current = self.detail = ""

    def result_text(self):
        result = self.response or "本次消息已处理。"
        if self.batch_total:
            return result
        if self.outcome == "success" and self.response == self.completed_response:
            if self.preference:
                result = (
                    f"{self.title}：{self.preference.condition} → "
                    f"{category_label(self.preference.category)}。"
                )
            elif self.expense:
                result = (
                    f"{self.title}：{self.expense.amount:.2f} 元 · "
                    f"{self.expense.description} · {category_label(self.expense.category)} · "
                    f"{self.expense.date.isoformat()}"
                )
        elif self.outcome == "clarify" and self.status == "待补充":
            result += " 本次尚未记账。"
        return result

    def snapshot(self):
        now = self.ended_at if self.ended_at is not None else self.clock()
        if self.ended_at is not None:
            status = f"{self.status} · 耗时 {int(now - self.received_at)} 秒"
        elif self.execution_at is None:
            status = "已收到 · 等待中" if self.queued else "已收到"
            if self.queued:
                status += f" · 已等待 {int(now - self.received_at)} 秒"
        else:
            status = f"处理中 · 已执行 {int(now - self.execution_at)} 秒"
            if self.wait_seconds:
                status += f" · 等待 {self.wait_seconds} 秒"
        elements = [body_element(status, muted=True)]
        if self.ended_at is None:
            elements.append(body_element(self.current))
            if self.detail:
                elements.append(body_element(self.detail))
        if self.steps:
            history = []
            for step in self.steps:
                content = {
                    "completed": "✓ " + step.completed,
                    "running": "· " + step.active,
                    "failed": "! 未完成：" + step.active.removeprefix("正在").rstrip("。"),
                    "unfinished": "· 未完成：" + step.active.removeprefix("正在").rstrip("。"),
                }[step.status]
                history.append(body_element(content, muted=True, text_size="notation"))
            elements.append(
                {
                    "tag": "collapsible_panel",
                    "element_id": "process",
                    "expanded": False,
                    "header": {"title": body_text("执行过程", muted=True, text_size="notation")},
                    "elements": history,
                }
            )
        if self.ended_at is not None:
            if self.batch_total:
                elements.extend(body_element(line) for line in self.result_text().splitlines())
            else:
                elements.append(body_element(self.result_text()))
        return {
            "schema": "2.0",
            "config": {
                "update_multi": True,
                "width_mode": "default",
                "summary": {"content": self.title if self.ended_at is not None else self.current},
            },
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "vertical_spacing": "8px",
                "elements": elements,
            },
        }

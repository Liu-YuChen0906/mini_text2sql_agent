"""集中渲染 Mini 的终端界面；展示函数不修改查询状态。"""

import os
import shlex
import sys
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any

from execute_sql import QueryResult
from rich import box
from rich.console import Console, Group, RenderableType
from rich.padding import Padding
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

NODE_LABELS = {
    "agent": "主 Agent", "run_text2sql": "调用 Text2SQL",
    "extract": "理解问题", "clarify": "澄清问题", "link": "选择表和字段",
    "context": "构造上下文", "generate": "生成 SQL", "regenerate": "修复 SQL",
    "revise": "按反馈重写 SQL", "execute": "执行 SQL", "score": "评估 SQL", "review": "人工审核",
}
REVIEW_CHOICES = {
    "A": ("approve", "通过"), "B": ("edit_sql", "重写 SQL"),
    "C": ("edit_schema", "重新选表"), "D": ("reject", "拒绝"), "E": ("exit", "退出"),
}


def create_console(*, stderr: bool = False) -> Console:
    """按输出流创建 Console；重定向或 NO_COLOR 时不输出 ANSI 样式。"""
    stream = sys.stderr if stderr else sys.stdout
    terminal = bool(getattr(stream, "isatty", lambda: False)())
    console = Console(
        file=stream, color_system="auto" if terminal and "NO_COLOR" not in os.environ else None,
        markup=False, highlight=False, emoji=False, safe_box=True,
    )
    console.width = min(console.width, 100)
    return console


def _text(value: Any, console: Console, style: str = "") -> Text:
    """外部内容作为普通文本；编码不支持的字符显示为转义文本。"""
    encoding = getattr(console.file, "encoding", None) or "utf-8"
    value = str(value).encode(encoding, errors="backslashreplace").decode(encoding)
    return Text(value, style=style, overflow="fold")


def _icon(value: str, console: Console) -> str:
    """无法编码 emoji 时省略图标，保留状态文字。"""
    try:
        value.encode(getattr(console.file, "encoding", None) or "utf-8")
    except UnicodeEncodeError:
        return ""
    return value + " "


def show_notice(message: str, level: str = "info", *, console: Console | None = None) -> None:
    """展示提示和状态；调用方可注入 stderr Console。"""
    console = console or create_console()
    icon, style = {
        "info": ("", "cyan"), "progress": ("🧭", "bold cyan"),
        "success": ("✅", "green"), "paused": ("⏸", "yellow"),
        "warning": ("⚠️", "yellow"), "error": ("❌", "red"),
    }.get(level, ("", "cyan"))
    console.print(_text((_icon(icon, console) if icon else "") + message, console, style))


def ask_text(prompt: str, *, console: Console | None = None) -> str:
    """统一输入样式，保持 input 的异常和原始返回值。"""
    console = console or create_console()
    console.print(_text(prompt, console, "bold cyan"), end=" ")
    return input("")


def show_task(thread_id: str, question: str, resumed: bool = False, *, console: Console | None = None) -> None:
    """展示问题、完整任务 ID 和恢复状态。"""
    console = console or create_console()
    content = _text(f"问题：{question}\n任务 ID：{thread_id}", console)
    console.print(Panel(content, title="Mini OpenChatBI", border_style="cyan", expand=False))
    if resumed:
        show_notice(f"正在恢复任务：{thread_id}", console=console)


def show_stage(
    node: str, phase: str, detail: str = "", elapsed: float | None = None,
    *, attempt: int = 1, console: Console | None = None,
) -> None:
    """追加节点事件；次数指本次命令行运行中该节点的执行次数。"""
    console = console or create_console()
    label, icon, style = {
        "start": ("开始", "", "cyan"), "complete": ("完成", "✅", "green"),
        "paused": ("等待输入", "⏸", "yellow"), "warning": ("需关注", "⚠️", "yellow"),
        "error": ("失败", "❌", "red"),
    }[phase]
    name = NODE_LABELS.get(node, node)
    if attempt > 1:
        name += f"（本次运行第 {attempt} 次）"
    line = f"  {name} · {(_icon(icon, console) if icon else '')}{label}"
    if elapsed is not None:
        line += f" · {elapsed:.2f} 秒"
    console.print(_text(line, console, style))
    if detail:
        console.print(Padding(_text(detail, console, "dim"), (0, 0, 0, 4)))


def build_result_table(
    columns: list[str], rows: list[list[Any]], *, console: Console | None = None,
) -> RenderableType:
    """生成普通表格或窄屏逐行表格，保留全部单元格内容。"""
    console = console or create_console()
    if not rows:
        return _text("没有查到数据。", console, "dim")
    count = max(len(columns), max(len(row) for row in rows))
    headers = [str(columns[i]) if i < len(columns) else f"列 {i + 1}" for i in range(count)]

    def cell(value: Any) -> Text:
        return _text("NULL" if value is None else value, console, "dim" if value is None else "")

    if count * 15 + 1 > min(console.width, 100):
        records = []
        for index, row in enumerate(rows, 1):
            table = Table(title=f"第 {index} 行", box=box.SIMPLE, expand=True, show_header=False)
            table.add_column("字段", style="cyan", max_width=max(4, console.width // 3), overflow="fold")
            table.add_column("值", overflow="fold")
            for i, header in enumerate(headers):
                table.add_row(_text(header, console), cell(row[i] if i < len(row) else None))
            records.append(table)
        return Group(*records)

    table = Table(box=box.ROUNDED, header_style="bold cyan", padding=(0, 1))
    for i, header in enumerate(headers):
        values = [row[i] for row in rows if i < len(row) and row[i] is not None]
        numeric = bool(values) and all(isinstance(v, int | float | Decimal) and not isinstance(v, bool) for v in values)
        table.add_column(_text(header, console), justify="right" if numeric else "left", overflow="fold", min_width=12)
    for row in rows:
        table.add_row(*(cell(row[i] if i < len(row) else None) for i in range(count)))
    return table


def show_sql(sql: str, *, console: Console | None = None) -> None:
    """仅高亮与换行，不重新生成或改写 SQL。"""
    console = console or create_console()
    code = _text(sql, console).plain
    console.print(Panel(
        Syntax(code, "sql", theme="ansi_dark", background_color="default", word_wrap=True),
        title="SQL", border_style="cyan",
    ))


def _show_score(score: Any, reasons: list[str], console: Console) -> None:
    available = isinstance(score, int | float) and not isinstance(score, bool)
    value = f"{score:.2f} / 1.00" if available else "不可用"
    console.print(_text(f"模型评分：{value}", console, "green" if available and score >= 0.7 else "yellow"))
    if reasons:
        console.print(_text("评分依据：", console, "bold"))
        reason_list = Table.grid(padding=0, expand=True)
        reason_list.add_column(width=2)
        reason_list.add_column(overflow="fold")
        for reason in reasons:
            reason_list.add_row("- ", _text(reason, console))
        console.print(Padding(reason_list, (0, 0, 0, 2)))


def show_review(payload: dict, *, console: Console | None = None) -> None:
    """渲染审核信息和允许的选项，不收集或修改审核决定。"""
    console = console or create_console()
    console.print()
    show_notice("SQL 需要人工审核", "paused", console=console)
    console.print(_text(f"问题：{payload.get('question', '')}", console))
    if payload.get("sql"):
        show_sql(payload["sql"], console=console)
    _show_score(payload.get("confidence"), payload.get("reasons") or [], console)
    console.print(_text("结果预览（最多 5 行）", console, "bold"))
    console.print(build_result_table(payload.get("columns") or [], payload.get("result_preview") or [], console=console))
    allowed = set(payload.get("options", [choice[0] for choice in REVIEW_CHOICES.values()]))
    console.print()
    for letter, (decision, label) in REVIEW_CHOICES.items():
        if decision in allowed:
            console.print(_text(f"{letter}. {label}", console, "bold cyan"))


def _show_rows(result: QueryResult, console: Console) -> None:
    console.print(build_result_table(result["columns"], result["rows"], console=console))
    if result.get("truncated"):
        show_notice(f"仅显示前 {len(result['rows'])} 行，还有更多数据。", "warning", console=console)
    elif result["rows"]:
        console.print(_text(f"返回 {len(result['rows'])} 行", console, "dim"))


def show_result(state: dict, *, console: Console | None = None) -> None:
    """按终态显示结果；低分人工批准不伪装为模型高分。"""
    console = console or create_console()
    console.print()
    if state.get("status") == "cancelled":
        show_notice("会话已结束。", "info", console=console)
        return
    if state.get("status") != "success":
        errors = state.get("errors") or []
        reason = state.get("error") or (state.get("result") or {}).get("error")
        reason = reason or (errors[-1].get("error") if errors else None) or state.get("status", "未知错误")
        show_notice(f"查询未完成：{reason}", "error", console=console)
        if state.get("sql"):
            show_sql(state["sql"], console=console)
        return
    label = "查询完成 · 人工已通过" if state.get("human_decision") == "approve" else "查询完成"
    show_notice(label, "success", console=console)
    if state.get("sql"):
        show_sql(state["sql"], console=console)
    console.print(_text(_icon("📋", console) + "查询结果", console, "bold cyan"))
    _show_rows(state["result"], console)
    if "confidence" in state or state.get("confidence_reasons"):
        console.print()
        _show_score(state.get("confidence"), state.get("confidence_reasons") or [], console)


def render_result_table(result: QueryResult) -> str:
    """保留纯文本接口，复用 Rich 的中文宽度和长内容排版。"""
    buffer = StringIO()
    console = Console(file=buffer, width=100, color_system=None, markup=False, highlight=False)
    if result["status"] != "success":
        show_notice(f"查询失败：{result.get('error')}", "error", console=console)
    else:
        _show_rows(result, console)
    return buffer.getvalue().rstrip("\n")


def show_saved_task(thread_id: str, checkpoint: Path, *, console: Console | None = None) -> None:
    """给出可复制的绝对路径命令，兼容路径空格与自定义检查点。"""
    console = console or create_console()
    command = shlex.join([
        sys.executable, str(Path(__file__).with_name("main.py")), "--resume", thread_id,
        "--checkpoint", str(checkpoint.resolve()),
    ])
    show_notice("任务已保存，可稍后恢复：", "paused", console=console)
    console.print(_text(command, console), soft_wrap=True)

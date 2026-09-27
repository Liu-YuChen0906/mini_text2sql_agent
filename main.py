"""可暂停并恢复的 Mini 主 Agent 命令行入口。"""

import argparse
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from agent_graph import build_agent_graph, new_agent_turn
from presentation import (
    NODE_LABELS,
    REVIEW_CHOICES,
    ask_text,
    create_console,
    show_notice,
    show_result,
    show_review,
    show_saved_task,
    show_stage,
    show_task,
)
from resources import PROJECT_DIR, create_llm, load_resources
from rich.console import Console
from query_history import QueryHistory

EXIT_COMMANDS = {"exit", "quit"}


def _request_feedback(payload: dict, *, console: Console | None = None) -> str | dict[str, str]:
    """展示澄清或审核，收集一次反馈；保持原有 A～E 输入协议。"""
    console = console or create_console()
    if payload.get("kind") == "clarification":
        show_notice(f"需要澄清：{payload['question']}", "paused", console=console)
        return ask_text("你的回答：", console=console).strip()
    show_review(payload, console=console)
    allowed = set(payload.get("options", [choice[0] for choice in REVIEW_CHOICES.values()]))
    while True:
        letter = ask_text("请选择选项字母：", console=console).strip().upper()
        if letter in REVIEW_CHOICES and REVIEW_CHOICES[letter][0] in allowed:
            decision = REVIEW_CHOICES[letter][0]
            break
        show_notice("请输入菜单中显示的选项字母。", "warning", console=console)
    if decision in {"edit_sql", "edit_schema"}:
        while True:
            feedback = ask_text("请用自然语言说明需要修改的内容：", console=console).strip()
            if feedback:
                return {"decision": decision, "feedback": feedback}
            show_notice("修改说明不能为空。", "warning", console=console)
    return {"decision": decision}


def _stage_summary(node: str, update: dict) -> tuple[str, str]:
    """将节点局部更新转成进度摘要，不将整个状态打印到终端。"""
    status = update.get("status")
    result = update.get("result") or {}
    if status in {"error", "fatal", "timeout", "rejected"}:
        errors = update.get("errors") or []
        reason = update.get("error") or result.get("error") or (errors[-1].get("error") if errors else None)
        phase = "warning" if node == "review" and status == "rejected" and not update.get("error") else "error"
        return phase, reason or str(status)
    if status == "cancelled":
        return "warning", "用户选择结束会话。"
    if status == "needs_clarification":
        return "warning", "需要补充信息，将进入澄清节点。"
    if node == "extract":
        return "complete", f"改写：{update.get('rewrite_question', '')}"
    if node == "link":
        return "complete", "已选：" + "、".join(item["table"] for item in update.get("selected", []))
    if node == "context":
        return "complete", "表结构、业务规则与相关示例已整理。"
    if node in {"generate", "regenerate", "revise"}:
        suffix = f" · 已用修复次数 {update['retry_count']}" if "retry_count" in update else ""
        return "complete", "SQL 已生成" + suffix
    if node == "execute":
        count = len(result.get("rows", []))
        return "complete", f"仅显示前 {count} 行，还有更多数据。" if result.get("truncated") else f"返回 {count} 行"
    if node == "score":
        score = update.get("confidence")
        if score is None:
            return "warning", "模型评分不可用，将进入人工审核。"
        return ("complete" if score >= 0.7 else "warning"), f"模型评分：{score:.2f} / 1.00"
    if node == "review":
        decisions = dict(REVIEW_CHOICES.values())
        return "complete", "人工决定：" + decisions.get(update.get("human_decision"), "已收到反馈")
    return "complete", "已收到补充信息。" if node == "clarify" else ""


def _pending_payload(snapshot) -> dict | None:
    """用途：从主图快照中读取子图待处理的澄清或人工审核暂停。"""
    pending = [item for task in snapshot.tasks for item in task.interrupts]
    if not pending:
        return None
    return dict(pending[0].value)


def _run_agent_graph(graph, inputs, config: dict, *, console: Console):
    """用途：展示主图及 Text2SQL 子图的运行进度并读取最终快照。

    参数输入：graph 为主图；inputs 是新一轮状态或恢复命令；
        config 含任务 ID；console 为终端输出对象。
    输出：主图的检查点快照，供调用方判断暂停、结果与会话结束状态。
    """
    started: dict[str, tuple[float, int]] = {}
    attempts: dict[str, int] = {}
    show_notice("执行过程", "progress", console=console)
    for _namespace, event in graph.stream(inputs, config=config, stream_mode="tasks", subgraphs=True):
        node = event.get("name", "")
        if node not in NODE_LABELS:
            continue
        task_id = event["id"]
        if "input" in event:
            attempts[node] = attempts.get(node, 0) + 1
            started[task_id] = (time.perf_counter(), attempts[node])
            show_stage(node, "start", attempt=attempts[node], console=console)
            continue
        beginning, attempt = started.pop(task_id, (None, attempts.get(node, 1)))
        elapsed = None if beginning is None else time.perf_counter() - beginning
        if event.get("interrupts"):
            phase, detail = "paused", "查询已暂停，等待用户输入。"
        elif event.get("error") is not None:
            phase, detail = "error", str(event["error"])
        else:
            phase, detail = _stage_summary(node, event.get("result") or {})
        show_stage(node, phase, detail, elapsed, attempt=attempt, console=console)
    return graph.get_state(config)


def _show_agent_turn(values: dict, history: QueryHistory, session_id: str, console: Console) -> None:
    """用途：按主图本轮结果展示 SQL 表格或普通交流文字回复。"""
    outcome = values.get("query_outcome")
    if outcome is not None:
        if outcome.get("status") == "success":
            record = history.get(session_id, outcome["record_turn_id"])
            if record is None:
                show_notice("成功查询记录缺失，无法展示结果表。", "error", console=console)
                return
            result = {
                "status": "success", "sql": record["sql"],
                "result": {
                    "status": "success", "columns": record["columns"], "rows": record["rows"],
                    "truncated": record["truncated"], "error": None,
                },
                "confidence": outcome.get("confidence"),
                "confidence_reasons": outcome.get("confidence_reasons", []),
                "human_decision": outcome.get("human_decision", ""),
            }
            show_result(result, console=console)
        else:
            show_result(outcome, console=console)
    elif values.get("reply"):
        console.print(values["reply"])


def _drive_agent(args, question: str | None, console: Console) -> None:
    """用途：运行持续会话的主 Agent，并处理多轮输入与暂停恢复。

    参数输入：args 含恢复任务 ID 和检查点路径；question 为首轮消息；
        console 负责提示、进度及结果展示。
    流程：同一任务 ID 下重复调用主图；子图暂停时收集反馈；
        会话退出时写入结束状态，避免之后继续追问。
    """
    thread_id = args.resume or uuid4().hex
    config = {"configurable": {"thread_id": thread_id}}
    if not args.resume:
        show_task(thread_id, question or "", console=console)
    show_notice("正在准备主 Agent 模型…", console=console)
    llm = create_llm()
    with closing(sqlite3.connect(args.checkpoint, check_same_thread=False)) as connection:
        history = QueryHistory(args.checkpoint)
        graph = build_agent_graph(
            llm, SqliteSaver(connection), history,
            lambda: load_resources(llm=llm),
        )
        if args.resume:
            snapshot = graph.get_state(config)
            if not snapshot.values:
                show_notice(f"找不到任务 {thread_id}。", "error", console=console)
                return
            if "messages" not in snapshot.values:
                show_notice("这是旧版任务；新主 Agent 暂不迁移旧检查点。", "warning", console=console)
                return
            show_task(thread_id, snapshot.values.get("current_question", ""), resumed=True, console=console)
            if snapshot.values.get("session_closed"):
                show_notice("该会话已结束，不能继续追问。", "warning", console=console)
                return
        else:
            snapshot = _run_agent_graph(graph, new_agent_turn(question or "", 1), config, console=console)

        show_current_result = not args.resume or _pending_payload(snapshot) is not None
        while True:
            while (payload := _pending_payload(snapshot)) is not None:
                try:
                    feedback = _request_feedback(payload, console=console)
                except (EOFError, KeyboardInterrupt):
                    console.print()
                    show_saved_task(thread_id, args.checkpoint, console=console)
                    return
                snapshot = _run_agent_graph(graph, Command(resume=feedback), config, console=console)
            if show_current_result:
                _show_agent_turn(dict(snapshot.values), history, thread_id, console)
            if snapshot.values.get("session_closed"):
                return
            try:
                next_question = ask_text("请输入问题（exit 结束会话）：", console=console).strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                show_saved_task(thread_id, args.checkpoint, console=console)
                return
            if not next_question:
                show_notice("问题不能为空。", "warning", console=console)
                show_current_result = False
                continue
            if next_question.lower() in EXIT_COMMANDS:
                graph.update_state(config, {"session_closed": True})
                show_notice("会话已结束。", console=console)
                return
            show_task(thread_id, next_question, console=console)
            turn_id = snapshot.values.get("turn_id", 0) + 1
            snapshot = _run_agent_graph(graph, new_agent_turn(next_question, turn_id), config, console=console)
            show_current_result = True


def main() -> None:
    """读取命令行参数并启动交互；模型和数据库异常使用统一错误样式。"""
    parser = argparse.ArgumentParser(description="Mini OpenChatBI：主 Agent 与 Text2SQL 查询")
    parser.add_argument("--resume", metavar="THREAD_ID", help="恢复暂停中的查询或继续追问")
    parser.add_argument("--checkpoint", type=Path, default=PROJECT_DIR / "checkpoints.sqlite")
    args = parser.parse_args()
    console = create_console()
    question = None
    try:
        if not args.resume:
            question = ask_text("请输入问题（exit 结束会话）：", console=console).strip()
            if question.lower() in EXIT_COMMANDS:
                return
            if not question:
                show_notice("问题不能为空。", "warning", console=console)
                return
        _drive_agent(args, question, console)
    except (EOFError, KeyboardInterrupt):
        show_notice("运行已中断。可使用任务 ID 和 --resume 恢复会话。", "warning", console=console)
    except Exception as exc:
        show_notice(f"运行失败：{exc}", "error", console=console)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

"""可暂停并恢复的 Mini Text2SQL 命令行入口。"""

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from presentation import render_result_table
from resources import PROJECT_DIR, load_resources
from workflow import build_workflow


def _request_feedback(payload: dict) -> str | dict[str, str]:
    """用途：根据暂停类型，从终端收集一次澄清回答或 SQL 审核决定。

    参数输入：payload 是图的 interrupt 内容；kind 为 clarification 时包含问题，
        否则包含待审核 SQL、评分原因和结果预览。
    输出：澄清时返回回答字符串；审核时返回含 decision 的字典，edit 还包含 SQL。
    异常：标准输入中断或结束时，沿用 input 的 KeyboardInterrupt 或 EOFError。
    """
    if payload.get("kind") == "clarification":
        print(f"需要澄清：{payload['question']}")
        return input("你的回答：").strip()
    print("\nSQL 需要人工审核：")
    print(f"问题：{payload.get('question', '')}")
    print(f"SQL：{payload.get('sql', '')}")
    print(f"置信度：{payload.get('confidence')}")
    print(f"原因：{'；'.join(payload.get('reasons', []))}")
    print(f"结果预览：{payload.get('result_preview', [])}")
    while True:
        decision = input("选择 approve / edit / reject：").strip().lower()
        if decision in {"approve", "edit", "reject"}:
            break
        print("请输入 approve、edit 或 reject。")
    if decision == "edit":
        return {"decision": "edit", "sql": input("请输入修改后的完整 SQL：").strip()}
    return {"decision": decision}


def _show_result(state: dict) -> None:
    """用途：把图的最终状态打印为失败原因或 SQL 与查询表格。

    参数输入：state 是 graph.invoke 返回的状态字典，成功时含 sql 和 result。
    输出：None；只向标准输出写文本，不修改状态或重新执行查询。
    """
    if state.get("status") != "success":
        print(f"查询未完成：{state.get('error') or (state.get('result') or {}).get('error') or state.get('status')}")
        return
    print("\n生成的 SQL：")
    print(state.get("sql", ""))
    print("\n执行结果：")
    print(render_result_table(state["result"]))


def main() -> None:
    """用途：处理新问题或恢复暂停任务，并驱动一次命令行查询直至结束。

    参数输入：从命令行读取 --resume、--checkpoint，从标准输入读取问题及反馈。
    输出：None；打印任务 ID、暂停提示与最终结果；检查点保存在 SQLite 文件中。
    """
    parser = argparse.ArgumentParser(description="Mini Text2SQL：可暂停并恢复的查询")
    parser.add_argument("--resume", metavar="THREAD_ID", help="继续先前暂停的查询")
    parser.add_argument("--checkpoint", type=Path, default=PROJECT_DIR / "checkpoints.sqlite")
    args = parser.parse_args()

    question = None
    if not args.resume:
        question = input("请输入问题：").strip()
        if not question:
            print("问题不能为空。")
            return

    resources = load_resources()
    thread_id = args.resume or uuid4().hex
    config = {"configurable": {"thread_id": thread_id}}
    with closing(sqlite3.connect(args.checkpoint, check_same_thread=False)) as connection:
        graph = build_workflow(resources, SqliteSaver(connection))
        if args.resume:
            snapshot = graph.get_state(config)
            pending = [item for task in snapshot.tasks for item in task.interrupts]
            if not pending:
                print(f"任务 {thread_id} 没有等待中的澄清或审核。")
                return
            print(f"正在恢复任务：{thread_id}")
            payload = pending[0].value
            try:
                feedback = _request_feedback(payload)
            except (EOFError, KeyboardInterrupt):
                print(f"\n任务已保存。稍后用 --resume {thread_id} 继续。")
                return
            state = graph.invoke(Command(resume=feedback), config=config)
        else:
            print(f"任务 ID：{thread_id}")
            state = graph.invoke(
                {"question": question, "clarification_history": [], "errors": [], "retry_count": 0},
                config=config,
            )

        while "__interrupt__" in state:
            payload = state["__interrupt__"][0].value
            try:
                feedback = _request_feedback(payload)
            except (EOFError, KeyboardInterrupt):
                print(f"\n任务已保存。稍后用 --resume {thread_id} 继续。")
                return
            state = graph.invoke(Command(resume=feedback), config=config)
        _show_result(state)


if __name__ == "__main__":
    main()

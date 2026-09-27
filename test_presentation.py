"""终端排版和真实 LangGraph 流式暂停/恢复的离线验收。"""

import io
import re
import shlex
from types import SimpleNamespace

import main
import presentation as ui
import pytest
from langchain_core.messages import AIMessage
from langgraph.types import Command
from rich.cells import cell_len
from rich.console import Console
from schema_linking import QuestionInfo
from sql_quality import QualityScore
from test_text2sql_graph import _error, _success
from test_agent_graph import FakeRoutingLLM, _parent, _tool_call


def capture(width=80):
    buffer = io.StringIO()
    return Console(file=buffer, width=width, color_system=None, markup=False, highlight=False), buffer


def test_table_chinese_alignment_numeric_null_and_literal_markup():
    console, output = capture()
    table = ui.build_result_table(["客户", "数量", "说明"], [["甲", 12, "[red]普通文本[/red]"], ["客户乙", 3, None]], console=console)
    console.print(table)
    text = output.getvalue()
    assert "[red]普通文本[/red]" in text
    assert "NULL" in text and "客户乙" in text
    lines = [line for line in text.splitlines() if "│" in line]
    edges = [[cell_len(line[:i]) for i, char in enumerate(line) if char == "│"] for line in lines]
    assert all(edge == edges[0] for edge in edges)
    for line in lines[1:]:
        assert re.search(r"\s+(12|3) $", line.split("│")[2])


@pytest.mark.parametrize("width", [32, 48, 80, 100])
def test_long_content_and_wide_tables_fit_without_losing_cells(width):
    console, output = capture(width)
    columns = [f"字段_{i}" for i in range(8)]
    values = [f"value_{i}_" + "长内容" * 5 for i in range(8)]
    console.print(ui.build_result_table(columns, [values], console=console))
    text = output.getvalue()
    assert "第 1 行" in text
    for i in range(8):
        assert f"字段_{i}" in text and f"value_{i}_" in text
    assert text.count("长") == 40
    assert max(map(cell_len, text.splitlines())) <= width


def test_sql_highlight_preserves_long_sql_and_markup():
    console, output = capture(48)
    sql = "SELECT '[red]' AS label, customer_id, customer_name FROM Customers ORDER BY customer_id;"
    ui.show_sql(sql, console=console)
    text = output.getvalue()
    assert "[red]" in text and "customer_id" in text and ";" in text
    assert "\x1b[" not in text
    assert max(map(cell_len, text.splitlines())) <= 48
    rendered_code = "".join(line.split("│")[1] for line in text.splitlines() if "│" in line)
    assert "".join(sql.split()) == "".join(rendered_code.split())


def test_empty_truncated_and_error_states_are_explicit():
    console, output = capture()
    empty = {**_success(), "rows": []}
    ui.show_result({"status": "success", "result": empty}, console=console)
    ui.show_result({"status": "success", "result": {**_success(), "truncated": True}}, console=console)
    ui.show_result({"status": "error", "error": "no such column: amount", "sql": "SELECT amount"}, console=console)
    ui.show_result({"status": "cancelled"}, console=console)
    text = output.getvalue()
    assert "没有查到数据。" in text
    assert "仅显示前 1 行，还有更多数据。" in text
    assert "查询未完成：no such column: amount" in text
    assert "会话已结束。" in text


def test_review_and_approved_low_score_remain_honest():
    console, output = capture()
    ui.show_review({"kind": "sql_review", "question": "[bold]原始问题", "confidence": None,
                    "reasons": ["评分服务不可用"], "result_preview": [["甲", 2]],
                    "options": ["approve", "reject", "exit"]}, console=console)
    ui.show_result({"status": "success", "result": _success(), "confidence": 0.2,
                    "human_decision": "approve", "confidence_reasons": ["需要核对口径"]}, console=console)
    text = output.getvalue()
    assert "列 1" in text and "列 2" in text
    assert "[bold]原始问题" in text and "模型评分：不可用" in text
    assert "A. 通过" in text and "B. 重写 SQL" not in text
    assert "人工已通过" in text and "0.20 / 1.00" in text
    assert "需要核对口径" in text


def test_no_color_and_redirected_output(monkeypatch):
    class TTY(io.StringIO):
        def isatty(self):
            return True

    for buffer, no_color in [(io.StringIO(), False), (TTY(), True)]:
        monkeypatch.setattr(ui.sys, "stdout", buffer)
        if no_color:
            monkeypatch.setenv("NO_COLOR", "1")
        else:
            monkeypatch.delenv("NO_COLOR", raising=False)
        console = ui.create_console()
        assert console.width <= 100
        ui.show_notice("完成", "success", console=console)
        ui.show_sql("SELECT 1;", console=console)
        assert "\x1b[" not in buffer.getvalue()


def test_narrow_score_reason_keeps_bullet_with_content():
    console, output = capture(32)
    reason = "按客户分组；未下单客户保留为零；按订单数降序排列。"
    ui.show_result({"status": "success", "result": _success(), "confidence": 0.9,
                    "confidence_reasons": [reason]}, console=console)
    lines = output.getvalue().splitlines()
    reason_lines = lines[lines.index("评分依据：") + 1:]
    assert reason_lines[0].strip().startswith("- 按")
    assert reason == "".join(line.strip().removeprefix("- ") for line in reason_lines)
    assert max(map(cell_len, lines)) <= 32


@pytest.mark.parametrize("encoding", ["gbk", "ascii"])
def test_encoding_restricted_output_drops_emoji(encoding):
    binary = io.BytesIO()
    stream = io.TextIOWrapper(binary, encoding=encoding, write_through=True)
    console = Console(file=stream, width=80, color_system=None, markup=False, highlight=False)
    ui.show_notice("完成", "success", console=console)
    ui.show_result({"status": "success", "sql": "SELECT 1", "result": _success()}, console=console)
    text = binary.getvalue().decode(encoding)
    assert "✅" not in text and "📋" not in text
    assert "SELECT 1" in text
    if encoding == "gbk":
        assert "完成" in text and "查询结果" in text
    stream.detach()


def test_recovery_command_retains_spaces_and_custom_checkpoint(tmp_path):
    console, output = capture(40)
    checkpoint = tmp_path / "space dir" / "my checkpoint.sqlite"
    ui.show_saved_task("full-task-id", checkpoint, console=console)
    command = output.getvalue().splitlines()[-1]
    args = shlex.split(command)
    assert args[args.index("--resume") + 1] == "full-task-id"
    assert args[args.index("--checkpoint") + 1] == str(checkpoint.resolve())


def test_pending_payload_reads_nested_interrupt_without_mutation():
    payload = {"kind": "sql_review", "columns": ["id"], "result_preview": [[1]]}
    snapshot = SimpleNamespace(values={"messages": []}, tasks=[SimpleNamespace(
        interrupts=[SimpleNamespace(value=payload)])])
    assert main._pending_payload(snapshot)["columns"] == ["id"]
    assert main._pending_payload(snapshot) is not payload


def test_real_graph_stream_reports_nodes_and_retry_without_duplicate_execution(tmp_path, monkeypatch):
    graph, connection, _, calls = _parent(
        tmp_path, monkeypatch, FakeRoutingLLM([_tool_call(1)]),
        sqls=["SELECT bad", "SELECT 1 AS id"],
        results=[_error("no such column: bad"), _success()],
    )
    console, output = capture(100)
    try:
        from agent_graph import new_agent_turn
        snapshot = main._run_agent_graph(graph, new_agent_turn("How many orders?", 1),
                                         {"configurable": {"thread_id": "retry"}}, console=console)
        assert snapshot.values["query_outcome"]["status"] == "success"
        assert calls["executed"] == ["SELECT bad", "SELECT 1 AS id"]
        text = output.getvalue()
        assert text.index("理解问题") < text.index("选择表和字段") < text.index("构造上下文") < text.index("生成 SQL")
        assert "no such column: bad" in text and "本次运行第 2 次" in text
        assert "评估 SQL" in text and "查询完成" not in text
        assert "Table: Orders" not in text
    finally:
        connection.close()


@pytest.mark.parametrize("kind", ["clarification", "sql_review"])
def test_stream_resume_after_sqlite_reopen(tmp_path, monkeypatch, kind):
    setup = {"extracts": [(None, "哪个月份？")]} if kind == "clarification" else {"scores": [QualityScore(0.2, ["需确认"])]}
    graph, connection, _, calls = _parent(tmp_path, monkeypatch, FakeRoutingLLM([_tool_call(1)]), **setup)
    config = {"configurable": {"thread_id": kind}}
    console, output = capture(100)
    from agent_graph import new_agent_turn
    first = main._run_agent_graph(graph, new_agent_turn("How many orders?", 1), config, console=console)
    assert main._pending_payload(first)["kind"] == kind
    assert "等待输入" in output.getvalue() and "查询完成" not in output.getvalue()
    if kind == "sql_review":
        assert main._pending_payload(first)["columns"] == ["id"]
    connection.close()

    graph, connection, _, resumed_calls = _parent(tmp_path, monkeypatch, FakeRoutingLLM([]))
    answer = "上个月" if kind == "clarification" else {"decision": "approve"}
    try:
        snapshot = main._run_agent_graph(graph, Command(resume=answer), config, console=console)
        assert snapshot.values["query_outcome"]["status"] == "success"
        assert main._pending_payload(snapshot) is None
        if kind == "sql_review":
            assert calls["executed"] == ["SELECT 1 AS id"]
            assert resumed_calls["executed"] == []
            assert resumed_calls["extract"] == []
        else:
            assert "上个月" in resumed_calls["extract"][0]
            assert len(resumed_calls["executed"]) == 1
    finally:
        connection.close()


def test_main_saves_pending_task_on_eof(tmp_path, monkeypatch, capsys):
    graph, connection, _, calls = _parent(tmp_path, monkeypatch, FakeRoutingLLM([_tool_call(1)]),
                                           checkpoint_name="custom checkpoint.sqlite", extracts=[(None, "哪个月份？")])
    answers = iter(["订单数量"])
    def read(_):
        try:
            return next(answers)
        except StopIteration:
            raise EOFError from None
    monkeypatch.setattr("builtins.input", read)
    checkpoint = tmp_path / "custom checkpoint.sqlite"
    monkeypatch.setattr("sys.argv", ["main.py", "--checkpoint", str(checkpoint)])
    monkeypatch.setattr(main, "create_llm", lambda: object())
    monkeypatch.setattr(main, "load_resources", lambda **kwargs: object())
    monkeypatch.setattr(main, "build_agent_graph", lambda *args: graph)
    monkeypatch.setattr(main, "uuid4", lambda: SimpleNamespace(hex="saved-task"))
    try:
        main.main()
        text = capsys.readouterr().out
        assert "任务已保存" in text and "--resume saved-task" in text
        assert str(checkpoint) in text and calls["executed"] == []
    finally:
        connection.close()


def test_cli_greeting_does_not_load_chroma(tmp_path, monkeypatch):
    routing = FakeRoutingLLM([AIMessage(content="你好，我可以帮你查询数据。")])
    answers = iter(["你好", "exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(main, "create_llm", lambda: routing)
    monkeypatch.setattr(main, "load_resources", lambda **kwargs: pytest.fail("普通聊天不应加载 Chroma"))
    monkeypatch.setattr(main, "uuid4", lambda: SimpleNamespace(hex="greeting"))
    monkeypatch.setattr("sys.argv", ["main.py", "--checkpoint", str(tmp_path / "checkpoints.sqlite")])
    console, output = capture(100)
    monkeypatch.setattr(main, "create_console", lambda: console)
    main.main()
    assert "你好，我可以帮你查询数据。" in output.getvalue()
    assert "会话已结束" in output.getvalue()


def test_cli_multiple_result_tables_and_closed_session(tmp_path, monkeypatch):
    graph, connection, _, calls = _parent(
        tmp_path, monkeypatch, FakeRoutingLLM([_tool_call(1), _tool_call(2)]),
        extracts=[(QuestionInfo("count orders", [], [], []), None), (QuestionInfo("monthly order count", [], [], []), None)],
        sqls=["SELECT 1 AS first", "SELECT 2 AS second"],
        results=[_success(), _success()],
        scores=[QualityScore(0.9, []), QualityScore(0.9, [])],
    )
    answers = iter(["Count orders", "Monthly?", "exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(main, "create_llm", lambda: object())
    monkeypatch.setattr(main, "load_resources", lambda **kwargs: object())
    monkeypatch.setattr(main, "build_agent_graph", lambda *args: graph)
    monkeypatch.setattr(main, "uuid4", lambda: SimpleNamespace(hex="conversation"))
    monkeypatch.setattr("sys.argv", ["main.py", "--checkpoint", str(tmp_path / "checkpoints.sqlite")])
    console, output = capture(100)
    monkeypatch.setattr(main, "create_console", lambda: console)
    try:
        main.main()
        text = output.getvalue()
        assert text.count("查询完成") == 2
        assert "SELECT 1 AS first" in text and "SELECT 2 AS second" in text
        assert calls["executed"] == ["SELECT 1 AS first", "SELECT 2 AS second"]
        assert graph.get_state({"configurable": {"thread_id": "conversation"}}).values["session_closed"] is True
        monkeypatch.setattr("sys.argv", ["main.py", "--resume", "conversation", "--checkpoint", str(tmp_path / "checkpoints.sqlite")])
        main.main()
        assert "不能继续追问" in output.getvalue()
        assert len(calls["executed"]) == 2
    finally:
        connection.close()


def test_cli_resume_between_rounds(tmp_path, monkeypatch):
    graph, connection, _, calls = _parent(
        tmp_path, monkeypatch, FakeRoutingLLM([_tool_call(1), _tool_call(2)]),
        extracts=[(QuestionInfo("count orders", [], [], []), None), (QuestionInfo("monthly order count", [], [], []), None)],
        sqls=["SELECT 1 AS first", "SELECT 2 AS second"],
        results=[_success(), _success()],
        scores=[QualityScore(0.9, []), QualityScore(0.9, [])],
    )
    monkeypatch.setattr(main, "create_llm", lambda: object())
    monkeypatch.setattr(main, "load_resources", lambda **kwargs: object())
    monkeypatch.setattr(main, "build_agent_graph", lambda *args: graph)
    monkeypatch.setattr(main, "uuid4", lambda: SimpleNamespace(hex="saved-conversation"))
    checkpoint = tmp_path / "checkpoints.sqlite"
    monkeypatch.setattr("sys.argv", ["main.py", "--checkpoint", str(checkpoint)])
    answers = iter(["Count orders"])
    def read_first(_):
        try:
            return next(answers)
        except StopIteration:
            raise EOFError from None
    monkeypatch.setattr("builtins.input", read_first)
    console, output = capture(100)
    monkeypatch.setattr(main, "create_console", lambda: console)
    try:
        main.main()
        assert "任务已保存" in output.getvalue()
        assert calls["executed"] == ["SELECT 1 AS first"]
        monkeypatch.setattr("sys.argv", ["main.py", "--resume", "saved-conversation", "--checkpoint", str(checkpoint)])
        answers = iter(["Monthly?", "quit"])
        monkeypatch.setattr("builtins.input", lambda _: next(answers))
        main.main()
        assert calls["executed"] == ["SELECT 1 AS first", "SELECT 2 AS second"]
        assert "完整问题='count orders'" in calls["extract"][1]
    finally:
        connection.close()

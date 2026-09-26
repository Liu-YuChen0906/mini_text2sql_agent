"""LangGraph orchestration around Mini's existing Text2SQL components."""

import re
from typing import Any, TypedDict

from execute_sql import QueryResult, execute_readonly_sql
from generate_sql import generate_sql, regenerate_sql
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from resources import RuntimeResources
from schema_linking import LinkResult, QuestionInfo, SchemaLinker, SelectedTable, extract_question_for_graph
from sql_context import build_sql_context
from sql_quality import score_sql

MAX_SQL_RETRIES = 3
MAX_CLARIFICATIONS = 2
CONFIDENCE_THRESHOLD = 0.7
REPAIRABLE_ERROR = re.compile(
    r"syntax error|no such (?:table|column|function)|ambiguous column|misuse of aggregate|"
    r"wrong number of arguments|incomplete input",
    re.IGNORECASE,
)


class SQLState(TypedDict, total=False):
    """Only JSON-like run data is persisted; resources stay outside the state."""

    question: str
    clarification_history: list[str]
    clarification_question: str
    info: dict[str, Any]
    selected: list[dict[str, Any]]
    real: dict[str, list[str]]
    rewrite_question: str
    sql_context: str
    sql: str
    result: QueryResult
    errors: list[dict[str, str]]
    retry_count: int
    confidence: float | None
    confidence_reasons: list[str]
    human_decision: str
    status: str
    error: str


def _link_result(state: SQLState) -> LinkResult:
    return LinkResult(
        state["rewrite_question"],
        [SelectedTable(item["table"], list(item["columns"])) for item in state["selected"]],
        {table: set(columns) for table, columns in state["real"].items()},
    )


def build_workflow(resources: RuntimeResources, checkpointer: Any):
    """Compile a resumable Text2SQL graph using already initialized dependencies."""
    linker = SchemaLinker(resources.catalog, resources.indexes, resources.llm, resources.database_path)

    def extract_node(state: SQLState) -> dict[str, Any]:
        question = state["question"]
        for answer in state.get("clarification_history", []):
            question += f"\n用户补充：{answer}"
        try:
            info, clarification = extract_question_for_graph(resources.llm, question)
        except Exception as exc:
            return {"status": "error", "error": f"信息提取失败：{exc}"}
        if clarification:
            if len(state.get("clarification_history", [])) >= MAX_CLARIFICATIONS:
                return {"status": "error", "error": "问题经两次澄清后仍不明确。"}
            return {"status": "needs_clarification", "clarification_question": clarification}
        assert info is not None
        return {
            "status": "extracted",
            "clarification_question": "",
            "info": {
                "rewrite_question": info.rewrite_question,
                "keywords": info.keywords,
                "dimensions": info.dimensions,
                "metrics": info.metrics,
            },
            "rewrite_question": info.rewrite_question,
        }

    def route_extraction(state: SQLState) -> str:
        if state.get("status") == "needs_clarification":
            return "clarify"
        return "link" if state.get("status") == "extracted" else "end"

    def clarify_node(state: SQLState) -> dict[str, Any]:
        answer = interrupt({"kind": "clarification", "question": state["clarification_question"]})
        if not isinstance(answer, str) or not answer.strip():
            return {"status": "error", "error": "澄清回答不能为空。"}
        return {
            "clarification_history": [*state.get("clarification_history", []), answer.strip()],
            "status": "clarified",
        }

    def link_node(state: SQLState) -> dict[str, Any]:
        try:
            info = QuestionInfo(**state["info"])
            linked = linker.link_from_info(info)
        except Exception as exc:
            return {"status": "error", "error": f"Schema Linking 失败：{exc}"}
        return {
            "status": "linked",
            "selected": [{"table": item.table, "columns": item.columns} for item in linked.selected],
            "real": {table: sorted(columns) for table, columns in linked.real.items()},
        }

    def context_node(state: SQLState) -> dict[str, Any]:
        try:
            context = build_sql_context(_link_result(state), resources.catalog, resources.indexes.text2sql)
        except Exception as exc:
            return {"status": "error", "error": f"SQL 上下文构造失败：{exc}"}
        return {"status": "context_ready", "sql_context": context}

    def generate_node(state: SQLState) -> dict[str, Any]:
        try:
            sql = generate_sql(state["rewrite_question"], state["sql_context"], resources.llm)
        except Exception as exc:
            return {"status": "error", "error": f"SQL 生成失败：{exc}"}
        if not sql or sql.lower() == "null":
            return {"status": "error", "error": "模型没有生成可执行 SQL。"}
        return {"status": "sql_ready", "sql": sql}

    def regenerate_node(state: SQLState) -> dict[str, Any]:
        previous = state["errors"][-1]
        try:
            sql = regenerate_sql(
                state["rewrite_question"], state["sql_context"], previous["sql"], previous["error"], resources.llm
            )
        except Exception as exc:
            return {"status": "error", "error": f"SQL 修复失败：{exc}"}
        if not sql or sql.lower() == "null":
            return {"status": "error", "error": "模型未能修复 SQL。"}
        return {"status": "sql_ready", "sql": sql, "retry_count": state.get("retry_count", 0) + 1}

    def execute_node(state: SQLState) -> dict[str, Any]:
        try:
            result = execute_readonly_sql(state["sql"], resources.database_path)
        except Exception as exc:
            return {"status": "fatal", "error": f"SQL 执行失败：{exc}"}
        update: dict[str, Any] = {"result": result, "status": result["status"]}
        if result["status"] != "success":
            update["errors"] = [
                *state.get("errors", []),
                {"sql": state["sql"], "error": result.get("error") or result["status"]},
            ]
        return update

    def route_execution(state: SQLState) -> str:
        if state.get("status") == "success":
            return "score"
        if state.get("status") != "error":
            return "end"
        result = state.get("result") or {}
        if (
            result.get("status") == "error"
            and REPAIRABLE_ERROR.search(result.get("error") or "")
            and state.get("retry_count", 0) < MAX_SQL_RETRIES
        ):
            return "regenerate"
        return "end"

    def score_node(state: SQLState) -> dict[str, Any]:
        try:
            evaluated = score_sql(
                resources.llm, state["rewrite_question"], state["sql"], state["sql_context"], state["result"]
            )
            return {"confidence": evaluated.score, "confidence_reasons": evaluated.reasons}
        except Exception as exc:
            return {"confidence": None, "confidence_reasons": [f"评分不可用：{exc}"]}

    def route_score(state: SQLState) -> str:
        score = state.get("confidence")
        return "end" if score is not None and score >= CONFIDENCE_THRESHOLD else "review"

    def review_node(state: SQLState) -> dict[str, Any]:
        feedback = interrupt(
            {
                "kind": "sql_review",
                "question": state["rewrite_question"],
                "sql": state["sql"],
                "result_preview": state["result"]["rows"][:5],
                "confidence": state.get("confidence"),
                "reasons": state.get("confidence_reasons", []),
                "options": ["approve", "edit", "reject"],
            }
        )
        if not isinstance(feedback, dict):
            return {"status": "error", "error": "人工审核决定格式无效。"}
        decision = feedback.get("decision")
        if decision == "approve":
            return {"human_decision": "approve", "status": "success"}
        if decision == "edit":
            sql = feedback.get("sql")
            if not isinstance(sql, str) or not sql.strip():
                return {"status": "error", "error": "修改 SQL 不能为空。"}
            return {"human_decision": "edit", "sql": sql.strip(), "status": "sql_ready"}
        if decision == "reject":
            if state.get("retry_count", 0) >= MAX_SQL_RETRIES:
                return {"human_decision": "reject", "status": "rejected", "error": "人工拒绝，SQL 重试次数已用完。"}
            return {
                "human_decision": "reject",
                "status": "rejected",
                "errors": [*state.get("errors", []), {"sql": state["sql"], "error": "人工审核拒绝了该 SQL。"}],
            }
        return {"status": "error", "error": "人工审核决定必须是 approve、edit 或 reject。"}

    def route_review(state: SQLState) -> str:
        if state.get("human_decision") == "edit" and state.get("status") == "sql_ready":
            return "execute"
        if (
            state.get("human_decision") == "reject"
            and state.get("status") == "rejected"
            and state.get("retry_count", 0) < MAX_SQL_RETRIES
        ):
            return "regenerate"
        return "end"

    graph = StateGraph(SQLState)
    for name, node in (
        ("extract", extract_node), ("clarify", clarify_node), ("link", link_node),
        ("context", context_node), ("generate", generate_node), ("regenerate", regenerate_node),
        ("execute", execute_node), ("score", score_node), ("review", review_node),
    ):
        graph.add_node(name, node)
    graph.add_edge(START, "extract")
    graph.add_conditional_edges("extract", route_extraction, {"clarify": "clarify", "link": "link", "end": END})
    graph.add_conditional_edges("clarify", lambda s: "extract" if s.get("status") == "clarified" else "end", {"extract": "extract", "end": END})
    graph.add_conditional_edges("link", lambda s: "context" if s.get("status") == "linked" else "end", {"context": "context", "end": END})
    graph.add_conditional_edges("context", lambda s: "generate" if s.get("status") == "context_ready" else "end", {"generate": "generate", "end": END})
    graph.add_conditional_edges("generate", lambda s: "execute" if s.get("status") == "sql_ready" else "end", {"execute": "execute", "end": END})
    graph.add_conditional_edges("regenerate", lambda s: "execute" if s.get("status") == "sql_ready" else "end", {"execute": "execute", "end": END})
    graph.add_conditional_edges("execute", route_execution, {"score": "score", "regenerate": "regenerate", "end": END})
    graph.add_conditional_edges("score", route_score, {"review": "review", "end": END})
    graph.add_conditional_edges("review", route_review, {"execute": "execute", "regenerate": "regenerate", "end": END})
    return graph.compile(checkpointer=checkpointer)

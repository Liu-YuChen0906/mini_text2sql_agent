"""用 LangGraph 编排 Mini Text2SQL 的澄清、选表、执行修复与人工审核。"""

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
    """类用途：定义一次 Text2SQL 查询在 LangGraph 节点之间传递的状态字段。

    内容：保存问题、澄清记录、选表结果、SQL、执行结果、错误、重试次数和评分。
    状态只含可持久化的查询数据；Catalog、模型和数据库连接留在图外的资源对象中。
    """

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
    """用途：把检查点中的普通字典还原为 SQL 上下文函数需要的 LinkResult。

    参数输入：state 含 rewrite_question、selected 表字段列表及 real 字段列表。
    输出：LinkResult；将已选表转为 SelectedTable，并将真实字段列表转为集合。
    """
    return LinkResult(
        state["rewrite_question"],
        [SelectedTable(item["table"], list(item["columns"])) for item in state["selected"]],
        {table: set(columns) for table, columns in state["real"].items()},
    )


def build_workflow(resources: RuntimeResources, checkpointer: Any):
    """用途：用已初始化资源构建并编译可暂停、可恢复的 Text2SQL 状态图。

    参数输入：resources 包含 Catalog、索引、模型和 SQLite 路径；checkpointer
        用于保存图状态，供澄清和人工审核后按任务 ID 恢复。
    输出：已编译的 LangGraph 图；各节点返回局部状态更新，条件边负责选择下一步。
    """
    linker = SchemaLinker(resources.catalog, resources.indexes, resources.llm, resources.database_path)

    def extract_node(state: SQLState) -> dict[str, Any]:
        """用途：拼接原问题与补充回答，让模型决定澄清或提取查询信息。

        参数输入：state 含原问题及已有澄清记录。
        输出：需要澄清时写入问题；明确时写入 QuestionInfo 字典；失败时写入错误状态。
        """
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
        """用途：根据提取状态选择澄清、选表或结束。

        参数输入：state 的 status 为 needs_clarification、extracted 或错误状态。
        输出：str，取 clarify、link 或 end，供 extract 的条件边使用。
        """
        if state.get("status") == "needs_clarification":
            return "clarify"
        return "link" if state.get("status") == "extracted" else "end"

    def clarify_node(state: SQLState) -> dict[str, Any]:
        """用途：暂停图等待用户回答澄清问题，并保存有效回答。

        参数输入：state 含 clarification_question 和此前回答。
        输出：恢复后追加 clarification_history；空回答则返回错误状态。
        """
        answer = interrupt({"kind": "clarification", "question": state["clarification_question"]})
        if not isinstance(answer, str) or not answer.strip():
            return {"status": "error", "error": "澄清回答不能为空。"}
        return {
            "clarification_history": [*state.get("clarification_history", []), answer.strip()],
            "status": "clarified",
        }

    def link_node(state: SQLState) -> dict[str, Any]:
        """用途：复用已提取的 QuestionInfo，选择并校验表与字段。

        参数输入：state 的 info 是提取节点保存的问题信息字典。
        输出：已选表字段和真实字段的可持久化字典；失败时返回错误状态。
        """
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
        """用途：将选表结果、业务规则和相似 SQL 示例整理成生成上下文。

        参数输入：state 含改写问题、已选表和数据库真实字段。
        输出：sql_context 文本及 context_ready 状态；失败时返回错误状态。
        """
        try:
            context = build_sql_context(_link_result(state), resources.catalog, resources.indexes.text2sql)
        except Exception as exc:
            return {"status": "error", "error": f"SQL 上下文构造失败：{exc}"}
        return {"status": "context_ready", "sql_context": context}

    def generate_node(state: SQLState) -> dict[str, Any]:
        """用途：让模型根据改写问题和结构上下文生成初版 SQL。

        参数输入：state 含 rewrite_question 和 sql_context。
        输出：非空 SQL 与 sql_ready 状态；模型异常或空结果转为错误状态。
        """
        try:
            sql = generate_sql(state["rewrite_question"], state["sql_context"], resources.llm)
        except Exception as exc:
            return {"status": "error", "error": f"SQL 生成失败：{exc}"}
        if not sql or sql.lower() == "null":
            return {"status": "error", "error": "模型没有生成可执行 SQL。"}
        return {"status": "sql_ready", "sql": sql}

    def regenerate_node(state: SQLState) -> dict[str, Any]:
        """用途：把最近一次执行错误或人工拒绝反馈给模型以修复 SQL。

        参数输入：state 含问题、上下文和 errors 列表中的最近一项。
        输出：修复后的 SQL，并将 retry_count 加一；失败时返回错误状态。
        """
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
        """用途：通过只读执行器运行当前 SQL，并记录结果或错误。

        参数输入：state 的 sql 是当前待运行语句；数据库路径来自闭包资源。
        输出：result 与其 status；非成功结果还会追加 errors，异常转为 fatal。
        """
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
        """用途：按执行结果决定评分、有限次修复或结束。

        参数输入：state 含执行 status、result 错误信息及 retry_count。
        输出：str，取 score、regenerate 或 end；仅指定的 SQLite 错误可重试。
        """
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
        """用途：评估成功执行的 SQL 与问题是否匹配。

        参数输入：state 含问题、SQL、结构上下文和执行结果。
        输出：confidence 与原因；评分异常时分数为 None，并记录失败原因。
        """
        try:
            evaluated = score_sql(
                resources.llm, state["rewrite_question"], state["sql"], state["sql_context"], state["result"]
            )
            return {"confidence": evaluated.score, "confidence_reasons": evaluated.reasons}
        except Exception as exc:
            return {"confidence": None, "confidence_reasons": [f"评分不可用：{exc}"]}

    def route_score(state: SQLState) -> str:
        """用途：按评分阈值决定直接结束或进入人工审核。

        参数输入：state 的 confidence 为 0 到 1 的分数或 None。
        输出：str，分数至少 0.7 时为 end，否则为 review。
        """
        score = state.get("confidence")
        return "end" if score is not None and score >= CONFIDENCE_THRESHOLD else "review"

    def review_node(state: SQLState) -> dict[str, Any]:
        """用途：暂停等待人工批准、修改或拒绝低置信度 SQL。

        参数输入：state 含问题、SQL、评分原因和可展示的结果预览。
        输出：批准时保持成功；修改时保存新 SQL；拒绝时记录错误；无效输入返回错误。
        """
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
        """用途：把人工修改送回执行，把拒绝送去修复，其余决定结束。

        参数输入：state 含 human_decision、status 和 retry_count。
        输出：str，取 execute、regenerate 或 end；拒绝只在重试限额内重新生成。
        """
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

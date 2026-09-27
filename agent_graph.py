"""编排持续会话的主 Agent，并将 Text2SQL 作为唯一的数据查询子图。"""

from collections.abc import Callable
from functools import lru_cache
from typing import Annotated, Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, InjectedToolArg, tool
from langgraph.graph import END, START, MessagesState, StateGraph

from query_history import QueryHistory, QueryRecord, history_index, selected_context
from resources import RuntimeResources
from text2sql_graph import build_text2sql_graph, new_text2sql_turn


class AgentState(MessagesState):
    """类用途：定义主图在各轮之间持久化的会话状态。

    内容：消息历史、当前问题与轮次、模型路由结果、本轮回复或查询状态，
        以及整个会话是否已经结束。
    """
    current_question: str
    turn_id: int
    route: str
    reply: str
    query_outcome: dict[str, Any] | None
    session_closed: bool


def new_agent_turn(question: str, turn_id: int) -> dict[str, Any]:
    """用途：开始一轮主 Agent 对话，并保留同一任务 ID 下的既有消息。

    参数输入：question 为本轮用户消息；turn_id 为本轮编号。
    输出：追加用户消息，并清空上一轮的路由、回复及查询状态。
    """
    return {
        "messages": [HumanMessage(content=question)],
        "current_question": question,
        "turn_id": turn_id,
        "route": "",
        "reply": "",
        "query_outcome": None,
    }


def create_text2sql_tool(child_factory: Callable[[], Any], history: QueryHistory) -> BaseTool:
    """用途：创建真正执行子图的工具，隔离模型参数与主图可信状态。

    参数输入：child_factory 按需返回已编译的 Text2SQL 子图；history 管理成功记录。
    输出：模型只看到 history_turn_ids；问题、轮次和运行配置由主图注入。
    """

    @tool("run_text2sql")
    def run_text2sql(
        history_turn_ids: list[int],
        question: Annotated[str, InjectedToolArg],
        turn_id: Annotated[int, InjectedToolArg],
        config: RunnableConfig,
    ) -> dict[str, Any]:
        """查询业务数据库；需要旧结果时传入对应的历史轮次 ID。"""
        session_id = config["configurable"]["thread_id"]
        ids = [item for item in history_turn_ids if isinstance(item, int) and item > 0]
        selected = [record for old_id in dict.fromkeys(ids) if (record := history.get(session_id, old_id))]
        context = history_index(history.list(session_id))
        if selected:
            context += "\n\n用户引用的历史查询完整已返回结果：\n" + selected_context(selected)
        result = child_factory().invoke(new_text2sql_turn(question, context=context), config=config)
        if result.get("status") == "success":
            query_result = result["result"]
            record: QueryRecord = {
                "turn_id": turn_id,
                "question": question,
                "rewrite_question": result["rewrite_question"],
                "sql": result["sql"],
                "columns": query_result["columns"],
                "rows": query_result["rows"],
                "truncated": query_result["truncated"],
            }
            history.save(session_id, record)
        return result

    return run_text2sql


SYSTEM_PROMPT = (
    "你是 Mini OpenChatBI 的主 Agent。你目前只有 run_text2sql 一个数据工具。"
    "凡是需要查询、比较、计算或引用业务数据库事实的问题，必须调用 run_text2sql；"
    "不能凭聊天记忆编造数据答案。问候、能力说明和普通交流可以直接回复。"
    "调用工具时，history_turn_ids 填入需要查看完整已保存结果的历史轮次编号；"
    "不需要旧结果时传空数组。工具会查询当前用户问题并生成新的结果表。"
    "历史预览可能截断，不能把预览当成完整数据。"
)


def build_agent_graph(
    llm: Any, checkpointer: Any, history: QueryHistory,
    query_resources_factory: Callable[[], RuntimeResources],
):
    """用途：构建主 Agent 图，并接入可暂停的 Text2SQL 子图。

    参数输入：llm 为主 Agent 模型；checkpointer 保存主图状态；
        history 管理成功查询记录；query_resources_factory 仅在数据查询时加载检索资源。
    输出：已编译的主图；子图继承主图检查点以支持同一任务 ID 恢复。
    """
    @lru_cache(maxsize=1)
    def child_graph():
        """用途：首次数据查询时加载资源并构建子图，后续轮次复用。"""
        return build_text2sql_graph(query_resources_factory(), None)

    query_tool = create_text2sql_tool(child_graph, history)
    routing_model = llm.bind_tools([query_tool], parallel_tool_calls=False)

    def route_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        """用途：结合会话消息和历史摘要，判断本轮是否需要查询数据库。

        输出：普通交流返回文字回复；数据问题记录一次工具调用并转入查询节点。
        已结束的会话不会再次调用模型。
        """
        if state.get("session_closed"):
            return {"route": "end", "reply": "会话已结束。"}
        session_id = config["configurable"]["thread_id"]
        index = history_index(history.list(session_id))
        # 工具消息只含少量预览；完整的已返回结果行保存在结构化历史中，
        # 仅在模型引用指定轮次时读取。
        messages = [SystemMessage(content=f"{SYSTEM_PROMPT}\n\n成功查询历史：\n{index}")]
        messages.extend(state["messages"][-16:])
        response = routing_model.invoke(messages)
        if not isinstance(response, AIMessage):
            raise ValueError("主 Agent 没有返回 AIMessage。")
        if response.tool_calls:
            if len(response.tool_calls) != 1 or response.tool_calls[0]["name"] != "run_text2sql":
                raise ValueError("主 Agent 只能调用一次 run_text2sql。")
            return {"messages": [response], "route": "query", "reply": ""}
        reply = str(response.content).strip()
        if not reply:
            raise ValueError("主 Agent 没有返回可显示的回复。")
        return {"messages": [response], "route": "end", "reply": reply}

    def query_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        """用途：执行模型选中的 Text2SQL 工具调用并保存成功结果。

        参数输入：当前问题、工具调用中的历史轮次 ID 和主图运行配置。
        输出：不含完整结果行的本轮摘要及简短工具消息；工具内部保存成功记录。
        子图的澄清或审核暂停会通过主图向命令行传递。
        """
        call = state["messages"][-1].tool_calls[0]
        ids = call.get("args", {}).get("history_turn_ids", [])
        if not isinstance(ids, list) or not all(isinstance(item, int) and item > 0 for item in ids):
            ids = []
        result = query_tool.invoke(
            {"history_turn_ids": ids, "question": state["current_question"], "turn_id": state["turn_id"]},
            config=config,
        )
        outcome = {
            "status": result.get("status"),
            "turn_id": state["turn_id"],
            "retry_count": result.get("retry_count", 0),
            "confidence": result.get("confidence"),
            "confidence_reasons": result.get("confidence_reasons", []),
            "human_decision": result.get("human_decision", ""),
            "session_closed": bool(result.get("session_closed")),
        }
        if result.get("status") == "success":
            outcome["record_turn_id"] = state["turn_id"]
        else:
            outcome["sql"] = result.get("sql", "")
            outcome["error"] = result.get("error") or (result.get("result") or {}).get("error", "")
        tool_summary = (
            f"Text2SQL 状态：{result.get('status')}；完整问题：{result.get('rewrite_question', '')}；"
            f"SQL：{result.get('sql', '')}；结果预览：{(result.get('result') or {}).get('rows', [])[:3]}"
        )
        return {
            "messages": [ToolMessage(content=tool_summary, tool_call_id=call["id"], name="run_text2sql")],
            "query_outcome": outcome,
            "route": "end",
            "session_closed": bool(result.get("session_closed")),
        }

    graph = StateGraph(AgentState)
    graph.add_node("agent", route_node)
    graph.add_node("run_text2sql", query_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", lambda state: state["route"], {"query": "run_text2sql", "end": END})
    graph.add_edge("run_text2sql", END)
    return graph.compile(checkpointer=checkpointer)

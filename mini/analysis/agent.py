"""DeepAgents harness restricted to an explicit evidence-producing allowlist."""

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

PROMPT = """你是数据分析子 Agent。只按自包含任务分析，关键口径有疑义时用 query_dataset 的澄清。
先规划，使用已有数据集或查询实际数据，调用固定工具或编写并实际运行 Python，最后必须 submit_analysis_report。
database_schema 是服务器读取的实际表/字段元数据，不能当成业务数值。已有结构时直接查询本步骤业务数据，不要先查询全库行数或枚举表结构；query_dataset 内部会完成选表和字段检索。
已有 input_datasets 含 schema、coverage、units 和真实预览；不必为了探查接口而消耗 Python 调用。
SDK 约定：load_dataset(id) 直接返回 pandas.DataFrame；emit_result(dict) 提交合法 JSON 数值；save_figure(fig,name) 和 save_dataset(df,name,units={列名:单位}) 都返回 None；保存派生表时显式声明列单位。
产物是否成功应检查 run_python_analysis 工具返回的 artifacts（media_type=image/png），不在 Python 中猜测产物 ID 或把返回 None 当成失败。
SQL 只能通过 query_dataset 执行；数据只传 dataset ID，不能手抄业务表格。
所有工具失败都要核对；Python 只准 analysis_sdk 的 load_dataset, emit_result, save_dataset, save_figure。
禁用 shell、文件和再委派工具，草稿不得作为证据。不输出未执行的结果。
固定工具参数：describe_dataset(columns, group_by?, ddof=1)；compare_periods/contribution_analysis(metric,dimensions,unit,missing_groups=reject|zero,aggregation=sum, additive=true)。
两期 dataset_ids 顺序当前、基期；analyze_trend/detect_anomalies(time_column,metric,window,granularity=day,duplicate_dates=reject|sum,timezone,method=zscore,threshold=3)；correlation_analysis(columns,alignment_keys,method=pearson|spearman,missing=pairwise_drop)。
变化率/贡献度单位 ratio；百分数 change_percent 单位 %。Python result_units 用 /result/... 指针声明实际单位。
报告每条 fact 都必须绑定 values，含 name,value,unit,evidence_id,result_pointer；只能引用成功真实执行。
严格遵守 submit_analysis_report 的强类型 schema。不要使用 facts、claim、explanation 等自造字段；没有 verification_status 或 claim_id 输入。
result_pointer 从证据根开始，如 /result/current；必须逐字使用同一个 evidence_id 的实际 result 键，不能把 Python 自定义键用于固定工具证据。
先核对执行结果，再提交；验证错误应修正引用，不要重新执行已成功的 Python 或通过提交探测字段。
事实文字服务端生成。解释要有 fact_refs（同次提交第一个事实可引用 fact:0，索引为 claims 的零基索引），假设标待验证，建议不冒充事实。
输入截断或主动 LIMIT 时不能声明总体。空数据不等于零。SQL 口径和数据真实性不能自动证明。
报告必须列出统计口径、时区、时间范围和关键限制。submit 后根据错误修正，最后只返回 report ID 和状态。
"""


def build_data_analysis_agent(model, runtime, checkpointer=None):
    tools = runtime.tools()
    allowed = {t.name for t in tools} | {"write_todos"}

    class EvidenceOnly(AgentMiddleware):
        def wrap_model_call(self, request, handler):
            # StateBackend never exposes a host filesystem or a shell. Remove all default bypass tools.
            filtered = [
                t
                for t in request.tools
                if (t.get("name") if isinstance(t, dict) else t.name) in allowed
            ]
            return handler(
                request.override(
                    tools=filtered,
                    model_settings={
                        **request.model_settings,
                        "parallel_tool_calls": False,
                    },
                )
            )

        def wrap_tool_call(self, request, handler):
            if request.tool_call["name"] not in allowed:
                return ToolMessage(
                    content="tool_disabled: use evidence tools only",
                    tool_call_id=request.tool_call["id"],
                )
            if request.tool_call["name"] == "write_todos":
                runtime.consume()
            return handler(request)

    return create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=PROMPT,
        subagents=[],
        backend=StateBackend(),
        middleware=[EvidenceOnly()],
        checkpointer=checkpointer,
        name="data_analysis",
    )

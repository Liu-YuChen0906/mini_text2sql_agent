"""提取问题意图、检索候选表与字段，并校验模型的选表结果。"""

import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from catalog import FileCatalog
from langchain_core.messages import HumanMessage, SystemMessage
from resources import IndexStores


@dataclass(frozen=True)
class QuestionInfo:
    """类用途：保存模型提取出的改写问题和检索线索。

    支持功能：通过明确的属性提供 rewrite_question（str）、keywords、
    dimensions、metrics（三者均为 list[str]）；不负责校验数据库字段。
    """

    rewrite_question: str
    keywords: list[str]
    dimensions: list[str]
    metrics: list[str]


@dataclass(frozen=True)
class SelectedTable:
    """类用途：表示模型选中且通过校验的一张表。

    支持功能：保存表名 table（str）及按原顺序去重的字段名 columns（list[str]）。
    """

    table: str
    columns: list[str]


@dataclass(frozen=True)
class LinkResult:
    """类用途：以带名称的属性保存一次 Schema Linking 的结果。

    支持功能：提供改写问题 rewrite_question（str）、选中表 selected
    （list[SelectedTable]）和数据库实有字段 real（dict[str, set[str]]）。
    """

    rewrite_question: str
    selected: list[SelectedTable]
    real: dict[str, set[str]]


def _model_json(llm: Any, system_prompt: str, user_prompt: str) -> dict[str, Any]:
    """用途：调用模型，并将返回的文本解析为 JSON 对象。

    参数输入：
        llm（Any）：实现 invoke(messages) 的聊天模型对象；调用结果需有 content 属性。
        system_prompt（str）：约束模型任务和输出格式的系统提示文本。
        user_prompt（str）：本次要处理的问题或附带纠错信息的用户提示文本。
    输出：
        dict[str, Any]：模型文本解析后的 JSON 对象；允许外层有 ```json 代码围栏。
    异常：非文本内容或 JSON 顶层不是对象时抛 ValueError；JSON 语法错误时
        json.loads 会抛 JSONDecodeError（它也是 ValueError 的子类）。
    """
    content = llm.invoke([SystemMessage(system_prompt), HumanMessage(user_prompt)]).content
    if not isinstance(content, str):
        raise ValueError("模型没有返回文本 JSON。")
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("模型必须返回 JSON 对象。")
    return result


def extract_question(llm: Any, question: str) -> QuestionInfo:
    """用途：从自然语言问题中提取后续选表所需的信息。

    参数输入：
        llm（Any）：实现 invoke(messages) 的聊天模型，用于提取查询意图。
        question（str）：用户原始自然语言问题，例如“按州统计客户数”。
    输出：
        QuestionInfo：rewrite_question 是非空 str；keywords、dimensions 和
            metrics 是 list[str]，分别保存检索词、维度和指标。模型的额外键不使用。
    异常：必需键缺失或类型不符时抛 ValueError；此阶段不验证字段是否真的存在。
    """
    result = _model_json(
        llm,
        "分析用户的数据查询问题。只返回 JSON 对象，包含 rewrite_question、keywords、dimensions、metrics。"
        "rewrite_question 是完整、忠实的问题；其余三项是字符串数组。"
        "提取业务实体、字段、指标及常见英文对应词，不要臆造数据库字段名。",
        question,
    )
    return _question_info_from_json(result)


def _question_info_from_json(result: dict[str, Any]) -> QuestionInfo:
    """用途：校验模型返回的问题改写和三组检索线索，并转换为 QuestionInfo。

    参数输入：result 是已解析的模型 JSON 对象，需含 rewrite_question、keywords、
        dimensions、metrics；本函数供普通提取和图提取共同使用。
    输出：QuestionInfo，保留模型给出的改写文本和三个字符串数组。
    异常：改写为空、字段缺失或数组元素不是字符串时抛 ValueError。
    """
    if not isinstance(result.get("rewrite_question"), str) or not result["rewrite_question"].strip():
        raise ValueError("信息提取结果缺少 rewrite_question。")
    for key in ("keywords", "dimensions", "metrics"):
        values = result.get(key)
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError(f"信息提取结果中的 {key} 必须是字符串数组。")
    return QuestionInfo(result["rewrite_question"], result["keywords"], result["dimensions"], result["metrics"])


def extract_question_for_graph(llm: Any, question: str) -> tuple[QuestionInfo | None, str | None]:
    """用途：让模型判断问题是否缺少关键信息，再提取查询意图或澄清问题。

    参数输入：llm 是聊天模型；question 是原问题及已有澄清回答拼接的文本。
    输出：问题明确时返回 (QuestionInfo, None)；需要澄清时返回 (None, 问题文本)。
    异常：模型 JSON 缺少布尔判断、澄清问题为空或提取字段无效时抛 ValueError。
    """
    result = _model_json(
        llm,
        "分析用户的数据查询问题。只返回 JSON 对象，包含 needs_clarification（布尔值）、"
        "clarification_question（字符串）、rewrite_question（字符串）、keywords、dimensions、metrics（三项字符串数组）。"
        "仅当缺失的信息会改变应查询的指标、筛选条件或数据范围时才要求澄清；"
        "否则 needs_clarification 为 false，并忠实改写问题，不要臆造字段。",
        question,
    )
    needs_clarification = result.get("needs_clarification")
    if not isinstance(needs_clarification, bool):
        raise ValueError("信息提取结果缺少布尔型 needs_clarification。")
    if needs_clarification:
        clarification = result.get("clarification_question")
        if not isinstance(clarification, str) or not clarification.strip():
            raise ValueError("需要澄清时必须提供 clarification_question。")
        return None, clarification.strip()
    return _question_info_from_json(result), None


def database_columns(database_path: str | Path) -> dict[str, set[str]]:
    """用途：从 SQLite 只读连接取得真实表名和字段名，供后续校验。

    参数输入：
        database_path（str | Path）：现有 SQLite 数据库文件路径，以只读 URI 打开。
    输出：
        dict[str, set[str]]：键是 sqlite_master 中的业务表名，值是通过
            PRAGMA table_info 读出的真实字段名集合；过滤 sqlite_ 开头的系统表。
            此结果用于核对 Catalog 和模型选择，不把 CREATE TABLE 文本传给模型。
    """
    path = Path(database_path).resolve()
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        result = {}
        for table in tables:
            quoted_table = table.replace('"', '""')
            result[table] = {row[1] for row in connection.execute(f'PRAGMA table_info("{quoted_table}")')}
        return result


def _related_columns(info: QuestionInfo, catalog: FileCatalog, column_store: Any) -> set[str]:
    """用途：结合向量检索和目录文本匹配，找出与问题相关的字段。

    参数输入：
        info（QuestionInfo）：extract_question 的结果；从 keywords、dimensions、
            metrics 三个 list[str] 拼接检索词，三者为空时用 rewrite_question 检索。
        catalog（FileCatalog）：提供已知字段名、显示名、别名、标签和描述。
        column_store（Any）：实现 similarity_search_with_score(query, k=12) 的
            字段向量集合；结果元素是 (文档, 距离)，文档 metadata 含 column_name。
    输出：
        set[str]：相关字段的 column_name 集合。向量结果只取距离小于 0.5 且
            字段名在 Catalog 中的项；同时加入文本包含或相似度至少 0.8 的字段。
            向量检索异常时打印提示并继续文本匹配；两者都没命中则返回空集合。
    """
    terms: list[str] = []
    for term_group in (info.keywords, info.dimensions, info.metrics):
        for value in term_group:
            term = value.strip()
            if term:
                terms.append(term)
    query = " ".join(terms) or info.rewrite_question
    all_columns = catalog.get_column_list()
    for table in catalog.get_table_list():
        all_columns.extend(catalog.get_column_list(table))
    known = {column["column_name"] for column in all_columns}
    matches = set()
    try:
        matches = {
            doc.metadata["column_name"]
            for doc, distance in column_store.similarity_search_with_score(query, k=12)
            if distance < 0.5 and doc.metadata.get("column_name") in known
        }
    except Exception as exc:
        print(f"字段向量检索不可用，改用 Catalog 文本匹配：{type(exc).__name__}", file=sys.stderr)
    for column in all_columns:
        names = [column.get(key, "") for key in ("column_name", "display_name", "alias", "tag", "description")]
        for term in terms:
            word = term.casefold().replace("_", " ")
            if any(
                word in name.casefold().replace("_", " ")
                or SequenceMatcher(None, word, name.casefold().replace("_", " ")).ratio() >= 0.8
                for name in names
                if name
            ):
                matches.add(column["column_name"])
                break
    return matches


def _candidate_tables(
    catalog: FileCatalog, matched_columns: set[str], real: dict[str, set[str]]
) -> dict[str, list[dict[str, str]]]:
    """用途：从目录中筛出包含相关字段且与真实数据库一致的候选表。

    参数输入：
        catalog（FileCatalog）：提供已登记的表及每张表的字段说明。
        matched_columns（set[str]）：_related_columns 找到的字段名；非空时每张
            候选表至少要包含一个命中字段，空集合时不按问题相关性过滤。
        real（dict[str, set[str]]）：database_columns 返回的真实表到字段名集合。
    输出：
        dict[str, list[dict[str, str]]]：键是候选表名，值是该表在 Catalog 中有
            说明且在数据库中实际存在的字段字典列表；没有候选表时返回空字典。
    """
    candidates = {}
    for table in catalog.get_table_list():
        columns = [
            column for column in catalog.get_column_list(table) if column["column_name"] in real.get(table, set())
        ]
        if columns and (not matched_columns or any(column["column_name"] in matched_columns for column in columns)):
            candidates[table] = columns
    return candidates


def _selection_examples(info: QuestionInfo, catalog: FileCatalog, store: Any, candidates: dict) -> list[str]:
    """用途：检索与当前问题相似且只涉及候选表的选表示例。

    参数输入：
        info（QuestionInfo）：extract_question 的结果；优先用 keywords 组成
            检索文本，没有关键词时使用 rewrite_question。
        catalog（FileCatalog）：提供“示例问题 → 应选表列表”的对应关系。
        store（Any）：实现 max_marginal_relevance_search(query, k=5,
            fetch_k=20) 的选表示例向量集合，返回的文档含 page_content。
        candidates（dict）：候选表名到字段说明列表的映射；只用其表名过滤示例。
    输出：
        list[str]：形如 "Question: ...\nSelected tables: ..." 的文本列表；
            仅保留示例中全部表都属于候选表的结果。检索失败时返回空列表。
    """
    examples = dict(catalog.get_table_selection_examples())
    query = " ".join(info.keywords) or info.rewrite_question
    try:
        found = store.max_marginal_relevance_search(query, k=5, fetch_k=20)
    except Exception as exc:
        print(f"选表示例检索不可用：{type(exc).__name__}", file=sys.stderr)
        return []
    return [
        f"Question: {doc.page_content}\nSelected tables: {', '.join(examples[doc.page_content])}"
        for doc in found
        if doc.page_content in examples and set(examples[doc.page_content]).issubset(candidates)
    ]


def _validate_selection(selection: Any, candidates: dict[str, list[dict[str, str]]]) -> list[SelectedTable]:
    """用途：核对模型选出的表和字段是否都属于候选范围。

    参数输入：
        selection（Any）：预期是 list[dict]；每项格式为
            {"table": str, "columns": list[str]}，来自模型 JSON 的 tables 键。
        candidates（dict[str, list[dict[str, str]]]）：允许选择的表名及其字段说明；
            每个字段字典用 column_name 标识允许的真实字段。
    输出：
        list[SelectedTable]：保留原有表顺序，每项含 table（str）与去重后的
            columns（list[str]）。必须至少选一张表，且每张表至少选一个有效字段。
    异常：表不在候选集中、表重复、字段缺失或字段类型错误时抛 ValueError。
    """
    if not isinstance(selection, list) or not selection:
        raise ValueError("必须选出至少一张表。")
    validated = []
    seen = set()
    for item in selection:
        if not isinstance(item, dict) or not isinstance(item.get("table"), str) or item["table"] not in candidates:
            raise ValueError("模型选了候选表之外的表。")
        table = item["table"]
        columns = item.get("columns")
        allowed = {column["column_name"] for column in candidates[table]}
        if (
            table in seen
            or not isinstance(columns, list)
            or not columns
            or any(not isinstance(column, str) or column not in allowed for column in columns)
        ):
            raise ValueError(f"{table} 的字段不存在、为空或表被重复选择。")
        seen.add(table)
        validated.append(SelectedTable(table, list(dict.fromkeys(columns))))
    return validated


def _selection_prompt(catalog: FileCatalog, candidates: dict[str, list[dict[str, str]]], examples: list[str]) -> str:
    """用途：把候选表、字段说明和选表示例填入原有系统提示词。

    参数输入：
        catalog（FileCatalog）：提供候选表的描述和选表规则。
        candidates（dict[str, list[dict[str, str]]]）：候选表与有效字段说明。
        examples（list[str]）：已过滤的相似选表示例文本。
    输出：str，供模型选择表与字段的完整系统提示词；保留原有文本与顺序。
    """
    table_text = []
    for table, columns in candidates.items():
        metadata = catalog.get_table_information(table)
        fields = ", ".join(
            f"{column['column_name']} ({column.get('display_name', '')}; {column.get('description', '')})"
            for column in columns
        )
        table_text.append(
            f"Table: {table}\nDescription: {metadata.get('description', '')}\n"
            f"Selection rule: {metadata.get('selection_rule', '')}\nColumns: {fields}"
        )
    return (
        "根据问题从候选表选择所需的表和字段。只能使用列出的表和字段。"
        '返回 JSON：{"tables":[{"table":"表名","columns":["字段名"]}]}。'
        "每张表至少列出一个相关字段；需要连接时包含连接字段。\n\n"
        + "\n\n".join(table_text)
        + "\n\nSimilar examples:\n"
        + "\n\n".join(examples)
    )


class SchemaLinker:
    """类用途：调度从自然语言问题到真实表与字段的选择流程。

    支持功能：复用 Catalog、索引、模型及 SQLite 路径；按问题提取、真实字段
    读取、候选检索、模型选表和结果校验的顺序执行。具体判断由独立函数完成。
    """

    def __init__(self, catalog: FileCatalog, indexes: IndexStores, llm: Any, database_path: str | Path) -> None:
        """用途：保存多次选表调用可以复用的依赖。

        参数输入：
            catalog（FileCatalog）：表、字段和选表示例的业务目录。
            indexes（IndexStores）：提供字段与选表示例向量集合。
            llm（Any）：实现 invoke(messages) 的聊天模型。
            database_path（str | Path）：供真实字段校验的 SQLite 文件路径。
        输出：None；只初始化属性，不发起模型请求或数据库查询。
        """
        self.catalog = catalog
        self.indexes = indexes
        self.llm = llm
        self.database_path = database_path

    def link(self, question: str) -> LinkResult:
        """用途：完成一次问题提取、候选检索、模型选表和校验。

        参数输入：question（str）是用户原始自然语言问题。
        输出：LinkResult，包含改写问题、已校验的表与字段、数据库真实字段。
        异常：无候选表时抛 ValueError；选表结果无效时反馈错误并重试一次，
            第二次仍无效则抛 ValueError。问题提取错误不在此处重试。
        """
        info = extract_question(self.llm, question)
        return self.link_from_info(info)

    def link_from_info(self, info: QuestionInfo) -> LinkResult:
        """用途：消费已提取的问题信息，检索并校验所选表和字段，不重复调用提取模型。

        参数输入：info 是图的 extract 节点产生的 QuestionInfo。
        输出：LinkResult，包含改写问题、已选表字段及数据库真实字段集合。
        异常：无候选表或模型两次选表都无效时抛 ValueError。
        """
        real = database_columns(self.database_path)
        matched = _related_columns(info, self.catalog, self.indexes.columns)
        candidates = _candidate_tables(self.catalog, matched, real)
        if not candidates:
            raise ValueError("Catalog 中没有与数据库一致的候选表。")
        examples = _selection_examples(info, self.catalog, self.indexes.table_selection_example, candidates)
        system_prompt = _selection_prompt(self.catalog, candidates, examples)
        error = ""
        for _ in range(2):
            try:
                result = _model_json(self.llm, system_prompt, f"Question: {info.rewrite_question}\n{error}")
                selected = _validate_selection(result.get("tables"), candidates)
                return LinkResult(info.rewrite_question, selected, real)
            except ValueError as exc:
                error = f"上次输出无效：{exc} 请只从上述候选表和字段中重新选择。"
        raise ValueError(f"Schema linking 失败：{error}")

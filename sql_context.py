"""从已选表和检索到的 SQL 示例生成模型可读的结构上下文。"""

import re
import sys
from typing import Any

from catalog import FileCatalog
from schema_linking import LinkResult


def _table_sections(link: LinkResult, catalog: FileCatalog) -> list[str]:
    """用途：为每张已选表生成字段和业务规则说明段落。

    参数输入：
        link（LinkResult）：含 selected（表与相关字段）及 real（数据库真实字段）。
        catalog（FileCatalog）：提供表描述、字段解释、派生指标及 SQL 规则。
    输出：
        list[str]：按选表顺序排列的段落；每段列出相关字段和该表全部有效字段。
            保留原有字段过滤方式及提示文本格式。
    """
    sections = []
    for item in link.selected:
        table = item.table
        metadata = catalog.get_table_information(table)
        columns = [column for column in catalog.get_column_list(table) if column["column_name"] in link.real[table]]
        column_text = "\n".join(
            f"- {column['column_name']} ({column.get('type', '')}): "
            f"{column.get('display_name', '')}; {column.get('description', '')}"
            for column in columns
        )
        sections.append(
            f"Table: {table}\nDescription: {metadata.get('description', '')}\n"
            f"Relevant columns: {', '.join(item.columns)}\nAvailable columns:\n{column_text}\n"
            f"Derived metrics: {metadata.get('derived_metric', '')}\nSQL rule: {metadata.get('sql_rule', '')}"
        )
    return sections


def _related_sql_examples(question: str, selected_tables: set[str], catalog: FileCatalog, store: Any) -> list[str]:
    """用途：检索并筛选可用于当前选表结果的 SQL 问答示例。

    参数输入：
        question（str）：改写后的自然语言问题，用于相似示例检索。
        selected_tables（set[str]）：本次选择的表名集合。
        catalog（FileCatalog）：提供示例问题、SQL 与 YAML 所属表标签。
        store（Any）：实现 max_marginal_relevance_search 的 SQL 示例索引。
    输出：
        list[str]：符合原有表标签和 FROM/JOIN 正则筛选规则的 Q/A 文本；
            检索失败时打印提示并返回空列表。保留 k=5、fetch_k=20 参数。
    """
    examples = {sample_question: (sql, tables) for sample_question, sql, tables in catalog.get_sql_examples()}
    related = []
    try:
        found = store.max_marginal_relevance_search(question, k=5, fetch_k=20)
    except Exception as exc:
        print(f"SQL 示例检索不可用：{type(exc).__name__}", file=sys.stderr)
        found = []
    for doc in found:
        if doc.page_content not in examples:
            continue
        sql, tagged_tables = examples[doc.page_content]
        referenced = set(re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", sql, flags=re.I))
        if set(tagged_tables).issubset(selected_tables) and referenced.issubset(selected_tables):
            related.append(f"Q: {doc.page_content}\nA: {sql}")
    return related


def build_sql_context(link: LinkResult, catalog: FileCatalog, store: Any) -> str:
    """用途：将选中表信息和相关 SQL 示例合并为生成 SQL 的上下文。

    参数输入：
        link（LinkResult）：Schema Linking 返回的改写问题、选表结果和真实字段。
        catalog（FileCatalog）：提供表字段及业务规则、SQL 示例数据。
        store（Any）：用于相似 SQL 示例检索的 text2sql 向量集合。
    输出：
        str：表说明段落、固定示例标题及筛选后的 Q/A 文本，以原有空行规则
            拼接；没有相关示例时仍保留标题。
    """
    sections = _table_sections(link, catalog)
    selected_tables = {item.table for item in link.selected}
    related = _related_sql_examples(link.rewrite_question, selected_tables, catalog, store)
    return "\n\n".join(sections) + "\n\nRelevant SQL examples:\n" + "\n\n".join(related)

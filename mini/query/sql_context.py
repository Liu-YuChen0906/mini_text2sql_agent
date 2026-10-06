"""从已选表和检索到的 SQL 示例生成模型可读的结构上下文。"""

from difflib import SequenceMatcher
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from mini.query.catalog import FileCatalog
from mini.query.schema_linking import LinkResult
from mini.runtime.notices import show_retrieval_warning


def _table_sections(link: LinkResult, catalog: FileCatalog) -> list[str]:
    """用途：为每张已选表生成字段和业务规则说明段落。

    参数输入：
        link（LinkResult）：含 selected（表与相关字段）及 real（数据库真实字段）。
        catalog（FileCatalog）：提供表描述、字段解释、派生指标及 SQL 规则。
    输出：
        list[str]：按选表顺序排列的段落；每段列出相关字段和该表全部有效字段。
            同时提供表粒度及关联路径。
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
            f"Derived metrics: {metadata.get('derived_metric', '')}\nSQL rule: {metadata.get('sql_rule', '')}\n"
            f"Grain: {metadata.get('grain', '')}\nRelationships: {metadata.get('relationships', '')}"
        )
    return sections


def _related_sql_examples(question: str, selected_tables: set[str], catalog: FileCatalog, store: Any, dialect: str) -> list[str]:
    """用途：检索并筛选可用于当前选表结果的 SQL 问答示例。

    参数输入：
        question（str）：改写后的自然语言问题，用于相似示例检索。
        selected_tables（set[str]）：本次选择的表名集合。
        catalog（FileCatalog）：提供示例问题、SQL 与 YAML 所属表标签。
        store（Any）：实现 max_marginal_relevance_search 的 SQL 示例索引。
    输出：
        list[str]：通过 SQL AST 校验物理表范围的 Q/A 文本；检索不足或失败时
            按目录问题文本相似度补齐，最多五条。
    """
    examples = {sample_question: (sql, tables) for sample_question, sql, tables in catalog.get_sql_examples(dialect)}
    related = []
    try:
        found = store.max_marginal_relevance_search(question, k=5, fetch_k=20)
    except Exception as exc:
        show_retrieval_warning(f"SQL 示例检索不可用：{type(exc).__name__}")
        found = []
    # Fill filtered retrieval gaps from the local catalog without an embedding call.
    questions = list(dict.fromkeys(doc.page_content for doc in found))
    ranked = sorted(examples, key=lambda sample: SequenceMatcher(None, question.casefold(), sample.casefold()).ratio(), reverse=True)
    questions.extend(sample for sample in ranked if sample not in questions)
    for sample in questions:
        if sample not in examples:
            continue
        sql, tagged_tables = examples[sample]
        try:
            tree = sqlglot.parse_one(sql, read=dialect)
            referenced = {
                ".".join(part.name for part in source.parts).casefold()
                for scope in traverse_scope(tree)
                for source in scope.sources.values() if isinstance(source, exp.Table)
            }
        except (sqlglot.errors.SqlglotError, ValueError):
            continue
        allowed = {table.casefold() for table in selected_tables}
        if {table.casefold() for table in tagged_tables}.issubset(allowed) and referenced.issubset(allowed):
            related.append(f"Q: {sample}\nA: {sql}")
            if len(related) >= 5:
                break
    return related


def build_sql_context(link: LinkResult, catalog: FileCatalog, store: Any, dialect: str = "sqlite") -> str:
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
    related = _related_sql_examples(link.rewrite_question, selected_tables, catalog, store, dialect)
    knowledge_path = catalog.data_path / "business_knowledge.md"
    knowledge = knowledge_path.read_text(encoding="utf-8") if knowledge_path.is_file() else ""
    if knowledge:
        sections.insert(0, "Business knowledge and glossary:\n" + knowledge)
    return "\n\n".join(sections) + "\n\nRelevant SQL examples:\n" + "\n\n".join(related)

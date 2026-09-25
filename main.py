"""从自然语言问题到 SQL 执行结果的 Mini 命令行入口。"""

from execute_sql import execute_readonly_sql
from generate_sql import generate_sql
from presentation import explain_result, render_result_table
from resources import load_resources
from schema_linking import SchemaLinker
from sql_context import build_sql_context


def main() -> None:
    """用途：按固定顺序运行一次自然语言数据查询并展示结果。

    参数输入：无函数参数；通过标准输入读取一条自然语言问题（str）。
    输出：
        None：不返回业务对象；依次打印 SQL、查询表格和可用时的中文解读。
            问题为空时直接提示；结果解读失败时保留已打印的查询表格并提示。
    流程：准备共享资源 → Schema Linking → SQL 上下文 → SQL 生成 →
        只读执行 → 表格展示 → 可选结果解读。
    """
    question = input("请输入问题：").strip()
    if not question:
        print("问题不能为空。")
        return

    resources = load_resources()
    linker = SchemaLinker(resources.catalog, resources.indexes, resources.llm, resources.database_path)
    linked = linker.link(question)
    context = build_sql_context(linked, resources.catalog, resources.indexes.text2sql)
    sql = generate_sql(linked.rewrite_question, context, resources.llm)
    print("\n生成的 SQL：")
    print(sql)

    result = execute_readonly_sql(sql, resources.database_path)
    print("\n执行结果：")
    print(render_result_table(result))
    if result["status"] == "success" and result["rows"]:
        try:
            explanation = explain_result(result, resources.llm)
            print("\n自然语言说明：")
            print(explanation)
        except Exception:
            print("\n自然语言说明暂时不可用，原始查询结果见上。")


if __name__ == "__main__":
    main()

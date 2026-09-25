"""将 SQLite 查询结果排版为终端文本。"""

from execute_sql import QueryResult


def _format_row(values: list[str], widths: list[int]) -> str:
    """用途：按照各列的显示宽度排版一行表格文本。

    参数输入：
        values（list[str]）：按列顺序排列、已经转成字符串的行数据。
        widths（list[int]）：与 values 对应的每列目标宽度。
    输出：str，各值右侧补空格后以 " | " 拼接的终端表格行。
    """
    cells = []
    for index, value in enumerate(values):
        cells.append(value.ljust(widths[index]))
    return " | ".join(cells)


def render_result_table(result: QueryResult) -> str:
    """用途：把执行器返回值转换成与原终端输出一致的文本。

    参数输入：
        result（QueryResult）：包含 status、columns、rows、truncated、error
            的 SQLite 查询结果字典；其中 rows 是 list[list[Any]]。
    输出：
        str：失败时为错误信息，成功但无行时为“没有查到数据。”；有数据时为
            表头、分隔线、数据行及必要的截断提示，内部换行与原输出一致。
    """
    if result["status"] != "success":
        return f"查询失败： {result['error']}"

    columns = result["columns"]
    rows = result["rows"]
    if not rows:
        return "没有查到数据。"

    display_rows = []
    for row in rows:
        display_rows.append(["NULL" if value is None else str(value) for value in row])

    widths = [len(column) for column in columns]
    for row in display_rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    lines = [_format_row(columns, widths), "-+-".join("-" * width for width in widths)]
    lines.extend(_format_row(row, widths) for row in display_rows)
    if result["truncated"]:
        lines.append(f"仅显示前 {len(rows)} 行，后面还有数据。")
    return "\n".join(lines)

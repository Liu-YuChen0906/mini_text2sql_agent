"""离线展示三种终端布局：python demo_output.py [--width 48]。"""

import argparse

from presentation import create_console, show_notice, show_result, show_review, show_stage, show_task


def main() -> None:
    """使用固定示例展示排版，不连接数据库或调用模型服务。"""
    parser = argparse.ArgumentParser(description="Mini 命令行输出离线演示")
    parser.add_argument("--width", type=int, default=None, help="模拟终端宽度，至少 24 字符")
    args = parser.parse_args()
    if args.width is not None and args.width < 24:
        parser.error("宽度至少为 24")
    console = create_console()
    if args.width is not None:
        console.width = min(args.width, 100)
    question = "列出每位客户的姓名和订单数量，按订单数从高到低排列"
    sql = (
        "SELECT c.customer_name, COUNT(o.order_id) AS order_count\n"
        "FROM Customers AS c\n"
        "LEFT JOIN Orders AS o ON c.customer_id = o.customer_id\n"
        "GROUP BY c.customer_id, c.customer_name\n"
        "ORDER BY order_count DESC;"
    )
    result = {
        "status": "success", "columns": ["客户姓名", "订单数量"],
        "rows": [["张三", 12], ["李四", 8], ["王五", 0]], "truncated": False, "error": None,
    }
    show_notice("离线样式演示 · 以下数据、评分和耗时均为固定示例", console=console)
    show_task("demo-task-001", question, console=console)
    show_notice("执行过程", "progress", console=console)
    for node, detail, elapsed in [
        ("extract", "改写：按客户统计订单数量", 1.2),
        ("link", "已选：Customers、Orders", 0.8),
        ("context", "表结构、业务规则与相关示例已整理。", 0.3),
        ("generate", "SQL 已生成", 1.5),
        ("execute", "返回 3 行", 0.01),
        ("score", "模型评分：0.90 / 1.00", 0.7),
    ]:
        show_stage(node, "start", console=console)
        show_stage(node, "complete", detail, elapsed, console=console)
    show_result({"status": "success", "sql": sql, "result": result, "confidence": 0.9,
                 "confidence_reasons": ["按客户分组；未下单客户保留为零；按订单数降序排列。"]}, console=console)

    console.print()
    console.rule("人工审核示例")
    show_stage("review", "paused", "查询已暂停，等待用户输入。", console=console)
    show_review({
        "kind": "sql_review", "question": question,
        "sql": "SELECT customer_id, COUNT(*) AS order_count FROM Orders GROUP BY customer_id;",
        "columns": ["customer_id", "order_count"], "result_preview": [[1, 12], [2, 8]],
        "confidence": 0.4, "reasons": ["缺少客户姓名，且没有保留未下单客户。"],
        "options": ["approve", "edit_sql", "edit_schema", "reject", "exit"],
    }, console=console)

    console.print()
    console.rule("查询失败示例")
    show_result({"status": "error", "sql": "SELECT customer_name, amount FROM Customers;",
                 "error": "SQL 执行失败：no such column: amount"}, console=console)


if __name__ == "__main__":
    main()

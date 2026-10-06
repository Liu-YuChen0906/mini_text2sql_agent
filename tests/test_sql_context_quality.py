"""真实样例数据和检索筛选的回归验证。"""
from pathlib import Path
from types import SimpleNamespace

from mini.query.sql_context import _related_sql_examples
from mini.query.sqlite_adapter import SQLiteAdapter


def test_cte_alias_is_not_treated_as_a_physical_table():
    catalog = SimpleNamespace(get_sql_examples=lambda _: [
        ("计数", "WITH x AS (SELECT order_id FROM Orders) SELECT COUNT(*) FROM x", ["Orders"]),
        ("客户计数", "SELECT COUNT(*) FROM Customers", ["Orders"]),
    ])
    store = SimpleNamespace(max_marginal_relevance_search=lambda *args, **kwargs: [
        SimpleNamespace(page_content="计数"), SimpleNamespace(page_content="客户计数"),
    ])
    examples = _related_sql_examples("计数", {"Orders"}, catalog, store, "sqlite")
    assert len(examples) == 1 and "WITH x" in examples[0]


def test_actual_status_values_and_date_range_are_available():
    adapter = SQLiteAdapter(Path(__file__).resolve().parents[1] / "data/tracking_orders.sqlite")
    context = adapter.sample_context({"Orders": {"order_status", "date_order_placed", "order_details"}})
    assert "Delivered" in context and "Canceled" in context
    assert "Packing" not in context and "order_details" not in context
    assert "最早/最晚日期" in context


def test_all_sqlite_examples_execute_and_pending_filter_matches_real_values():
    from mini.query.catalog import FileCatalog
    root = Path(__file__).resolve().parents[1]
    catalog = FileCatalog(root / "catalog_data")
    adapter = SQLiteAdapter(root / "data/tracking_orders.sqlite")
    examples = catalog.get_sql_examples()
    assert len(examples) >= 8
    for question, sql, _ in examples:
        result = adapter.execute_readonly(sql).result
        assert result["status"] == "success", (question, result.get("error"))
        if "pending orders" in question:
            assert result["rows"] and all(row[2] == "Pending" for row in result["rows"])


def test_parse_error_in_read_query_can_be_repaired_without_executing_it():
    adapter = SQLiteAdapter(Path(__file__).resolve().parents[1] / "data/tracking_orders.sqlite")
    outcome = adapter.execute_readonly("SELECT * FROM Orders WHERE (")
    assert outcome.result["status"] == "error" and outcome.repairable
    assert adapter.execute_readonly("DELETE FROM Orders").result["status"] == "rejected"

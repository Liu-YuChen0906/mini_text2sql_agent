"""业务数据库适配器、扩展注册和 PostgreSQL 集成测试。"""

import os
import sqlite3
from pathlib import Path

import pytest

from mini.query.catalog import FileCatalog
from mini.query.database import ExecutionOutcome, create_adapter, empty_result, register_adapter
from mini.query.postgres_adapter import PostgreSQLAdapter
from mini.query.sqlite_adapter import SQLiteAdapter


def test_sqlite_adapter_and_query_guard(tmp_path):
    """SQLite 适配器应读取真实列、截断行数并拒绝多语句与写入 CTE。"""
    path = tmp_path / "orders.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE Orders (order_id INTEGER)")
        connection.executemany("INSERT INTO Orders VALUES (?)", [(1,), (2,)])
    adapter = SQLiteAdapter(path)
    assert adapter.list_columns(["Orders"]) == {"Orders": {"order_id"}}
    result = adapter.execute_readonly("SELECT order_id FROM Orders ORDER BY order_id", row_limit=1).result
    assert result["rows"] == [[1]] and result["truncated"]
    for sql in (
        "DELETE FROM Orders",
        "WITH changed AS (DELETE FROM Orders) SELECT * FROM changed",
        "SELECT 1; DELETE FROM Orders",
        "SELECT 1;;",
        "SELECT 1 INTO new_table",
    ):
        assert adapter.execute_readonly(sql).result["status"] == "rejected"
    assert adapter.execute_readonly("SELECT '; DELETE' AS value -- ;\n").result["status"] == "success"
    assert adapter.execute_readonly("SELECT missing FROM Orders").repairable


def test_factory_defaults_and_registers_third_adapter(tmp_path):
    """新增适配器只需注册工厂，无需修改业务图或资源构造。"""
    default = tmp_path / "example.sqlite"
    assert isinstance(create_adapter({}, default), SQLiteAdapter)
    assert create_adapter({}, default).path == default

    class ThirdAdapter:
        """只提供统一接口的测试数据库。"""

        dialect = "third"

        def list_columns(self, catalog_tables):
            return {catalog_tables[0]: {"id"}}

        def execute_readonly(self, sql, row_limit=100, timeout_seconds=2.0):
            return ExecutionOutcome(empty_result("success"))

    register_adapter("test_third", lambda config, path: ThirdAdapter())
    adapter = create_adapter({"database": {"type": "test_third"}}, default)
    assert adapter.list_columns(["items"]) == {"items": {"id"}}
    assert adapter.execute_readonly("SELECT 1").result["status"] == "success"
    with pytest.raises(ValueError, match="已注册"):
        register_adapter("test_third", lambda config, path: ThirdAdapter())


def test_postgres_configuration_and_examples(tmp_path, monkeypatch):
    """连接串仅取自环境变量，PostgreSQL 示例使用对应 SQL 方言。"""
    monkeypatch.setenv("TEST_MINI_DATABASE_URL", "postgresql://example.invalid/orders")
    adapter = create_adapter({"database": {"type": "postgres", "url_env": "TEST_MINI_DATABASE_URL"}}, tmp_path)
    assert isinstance(adapter, PostgreSQLAdapter)
    assert adapter.dialect == "postgres"
    assert isinstance(create_adapter({"database": {"type": "postgresql", "url_env": "TEST_MINI_DATABASE_URL"}}, tmp_path), PostgreSQLAdapter)
    monkeypatch.delenv("TEST_MINI_DATABASE_URL")
    with pytest.raises(ValueError, match="未设置"):
        create_adapter({"database": {"type": "postgres", "url_env": "TEST_MINI_DATABASE_URL"}}, tmp_path)
    catalog = FileCatalog(Path(__file__).resolve().parents[1] / "catalog_data")
    examples = catalog.get_sql_examples("postgres")
    assert any("CURRENT_DATE - INTERVAL '30 days'" in sql for _, sql, _ in examples)
    assert catalog.get_sql_example_questions() == [question for question, _, _ in catalog.get_sql_examples()]
    assert catalog.get_sql_examples("third") == []


@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_DSN"), reason="需要临时 PostgreSQL；CI 提供服务")
def test_postgres_integration():
    """在独立测试库中验证表结构、只读执行、截断、超时和错误分类。"""
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql as pg_sql

    dsn = os.environ["TEST_POSTGRES_DSN"]
    catalog = FileCatalog(Path(__file__).resolve().parents[1] / "catalog_data")
    tables = catalog.get_table_list()
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            for table in tables:
                cursor.execute(pg_sql.SQL("DROP TABLE IF EXISTS {}").format(pg_sql.Identifier(table.lower())))
            for table in tables:
                columns = [row["column_name"] for row in catalog.table_columns if row["table_name"] == table]
                definitions = [pg_sql.SQL("{} {}").format(
                    pg_sql.Identifier(column),
                    pg_sql.SQL(
                        "INTEGER" if column.endswith(("_id", "_number")) else
                        "DATE" if "date" in column else "TEXT"
                    ),
                ) for column in columns]
                cursor.execute(pg_sql.SQL("CREATE TABLE {} ({})").format(
                    pg_sql.Identifier(table.lower()), pg_sql.SQL(", ").join(definitions),
                ))
            cursor.execute("INSERT INTO customers (customer_id, customer_name) VALUES (1, 'Alice'), (2, 'Bob')")
            cursor.execute("INSERT INTO orders (order_id, customer_id) VALUES (10, 1), (11, 2)")
    try:
        adapter = PostgreSQLAdapter(dsn)
        real = adapter.list_columns(tables)
        assert set(real) == set(tables)
        assert real["Customers"] == {"customer_id", "customer_name", "customer_details", "state"}
        assert "order_id" in real["Orders"]
        result = adapter.execute_readonly("SELECT order_id FROM Orders ORDER BY order_id", row_limit=1).result
        assert result["status"] == "success" and result["rows"] == [[10]] and result["truncated"]
        for question, example_sql, _ in catalog.get_sql_examples("postgres"):
            assert adapter.execute_readonly(example_sql).result["status"] == "success", question
        assert adapter.execute_readonly("DELETE FROM Orders").result["status"] == "rejected"
        assert adapter.execute_readonly("SELECT pg_sleep(1)", timeout_seconds=0.01).result["status"] == "timeout"
        failed = adapter.execute_readonly("SELECT absent FROM Orders")
        assert failed.result["status"] == "error" and failed.repairable
    finally:
        with psycopg.connect(dsn) as connection:
            with connection.cursor() as cursor:
                for table in reversed(tables):
                    cursor.execute(pg_sql.SQL("DROP TABLE {}").format(pg_sql.Identifier(table.lower())))

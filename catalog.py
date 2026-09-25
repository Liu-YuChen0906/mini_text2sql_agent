"""读取文件式业务目录，提供表、字段说明和示例数据的查询接口。"""

import csv
from pathlib import Path
from typing import Any

import yaml


class FileCatalog:
    """类用途：管理 Mini 项目的文件式数据目录。

    数据来源：table_columns.csv 定义表与字段的对应关系，common_columns.csv
    和可选的 table_spec_columns.csv 提供字段说明，两个 YAML/CSV 示例文件提供
    选表和 SQL 范例。这里保存的是业务元数据，并不直接查询 SQLite 中的数据行。
    支持功能：按表获取字段及其说明、列出表、获取表规则、解析选表示例和 SQL 示例。
    """

    def __init__(self, data_path: str | Path):
        """用途：加载数据目录中的 CSV 和 YAML 文件。

        参数输入：
            data_path（str | Path）：存放五份必需目录文件的文件夹路径。
                可以传路径字符串或 pathlib.Path；table_spec_columns.csv 可缺省。
        输出：
            None：构造函数不返回业务值；把各文件内容加载到实例属性中。
        异常：缺少必需文件时抛出 FileNotFoundError。
        """
        self.data_path = Path(data_path)
        required = (
            "table_columns.csv",
            "common_columns.csv",
            "table_info.yaml",
            "table_selection_example.csv",
            "sql_example.yaml",
        )
        missing = [name for name in required if not (self.data_path / name).is_file()]
        if missing:
            raise FileNotFoundError(f"Catalog 缺少文件：{', '.join(missing)}")

        self.table_columns = self._read_csv("table_columns.csv")
        self.common_columns = self._read_csv("common_columns.csv")
        self.table_spec_columns = self._read_csv("table_spec_columns.csv")
        self.table_info = self._read_yaml("table_info.yaml")
        self.selection_examples = self._read_csv("table_selection_example.csv")
        self.sql_examples = self._read_yaml("sql_example.yaml")

    def _read_csv(self, filename: str) -> list[dict[str, str]]:
        """用途：读取一份 CSV 文件，并以表头作为字典键。

        参数输入：
            filename（str）：相对于 self.data_path 的 CSV 文件名，例如
                "common_columns.csv"，不是绝对路径。
        输出：
            list[dict[str, str]]：每个字典表示一行，键是 CSV 表头、值是单元格文本；
                文件不存在时返回空列表。CSV 字段均按字符串读取，不做类型转换。
        """
        path = self.data_path / filename
        if not path.is_file():
            return []
        with path.open(encoding="utf-8", newline="") as file:
            return list(csv.DictReader(file))

    def _read_yaml(self, filename: str) -> dict[str, Any]:
        """用途：读取一份 YAML 文件。

        参数输入：
            filename（str）：相对于 self.data_path 的 YAML 文件名，例如
                "table_info.yaml"。
        输出：
            dict[str, Any]：解析后的映射；值的具体类型由 YAML 内容决定，
                文件内容为空时返回空字典。文件不存在或 YAML 无效时会抛异常。
        """
        with (self.data_path / filename).open(encoding="utf-8") as file:
            return yaml.safe_load(file) or {}

    def get_column_list(self, table: str | None = None) -> list[dict[str, str]]:
        """用途：取得通用字段，或取得某张表的字段及其说明。

        参数输入：
            table（str | None）：要查询的完整表名，例如 "Orders" 或
                "db.Orders"；None 表示返回 common_columns.csv 中的全部通用字段。
        输出：
            list[dict[str, str]]：字段说明字典列表，常见键有 column_name、
                display_name、type、alias、description。指定表时只返回
                table_columns.csv 登记且存在说明的字段；专属说明优先于通用说明。
                表不存在或字段没有说明时，对应结果为空或不包含该字段。
        """
        common = {row["column_name"]: row for row in self.common_columns}
        if table is None:
            return list(common.values())

        specific = {
            (f"{row['db_name']}.{row['table_name']}".strip("."), row["column_name"]): row
            for row in self.table_spec_columns
        }
        result = []
        for row in self.table_columns:
            full_table = f"{row['db_name']}.{row['table_name']}".strip(".")
            if full_table == table:
                column = specific.get((table, row["column_name"])) or common.get(row["column_name"])
                if column:
                    result.append(column)
        return result

    def get_table_list(self) -> list[str]:
        """用途：列出数据目录中登记的表。

        参数输入：无；读取实例中的 table_columns 字段映射数据。
        输出：
            list[str]：不重复的表名，顺序按 table_columns.csv 首次出现的位置；
                有 db_name 时格式为 "数据库名.表名"，否则只有表名。
        """
        return list(dict.fromkeys(f"{row['db_name']}.{row['table_name']}".strip(".") for row in self.table_columns))

    def get_table_information(self, table: str) -> dict[str, Any]:
        """用途：取得某张表的业务描述和使用规则。

        参数输入：
            table（str）：完整表名，例如 "Orders" 或 "db.Orders"；无前缀时
                使用 table_info.yaml 中的空字符串数据库键。
        输出：
            dict[str, Any]：该表的元数据，可能含 description、selection_rule、
                sql_rule、derived_metric 等业务说明；未登记时返回空字典。
        """
        database, _, table_name = table.rpartition(".")
        return self.table_info.get(database, {}).get(table_name, {})

    def get_table_selection_examples(self) -> list[tuple[str, list[str]]]:
        """用途：解析问题到选中表的示例。

        参数输入：无；读取实例中的 selection_examples CSV 行数据。
        输出：
            list[tuple[str, list[str]]]：每项为 (示例问题, 应选表名列表)，例如
                ("Show me all customers", ["Customers"])；缺少问题或表列表的行被跳过。
        异常：selected_tables 单元格不是合法 JSON 时会抛出 JSONDecodeError。
        """
        import json

        return [
            (row["question"], json.loads(row["selected_tables"]))
            for row in self.selection_examples
            if row.get("question") and row.get("selected_tables")
        ]

    def get_sql_examples(self) -> list[tuple[str, str, list[str]]]:
        """用途：解析 YAML 中以 Q/A 格式保存的 SQL 示例。

        参数输入：无；读取实例中的 sql_examples YAML 映射。
        输出：
            list[tuple[str, str, list[str]]]：每项为 (示例问题, SQL 文本,
                YAML 所属表名列表)。一个文本块可以有多组以 Q:、A: 开头的问答；
                只有问题和答案都非空的组合才进入结果。第三项只记录 YAML 的表标签，
                不自动分析 SQL 中的所有 JOIN 表。
        """
        examples = []
        for database, tables in self.sql_examples.items():
            for table, block in tables.items():
                question = answer = ""
                current = None
                for line in block.strip().splitlines():
                    if line.startswith("Q:"):
                        if question and answer:
                            examples.append((question.strip(), answer.strip(), [f"{database}.{table}".strip(".")]))
                        question, answer, current = line[2:], "", "question"
                    elif line.startswith("A:"):
                        answer, current = line[2:], "answer"
                    elif current == "question":
                        question += "\n" + line
                    elif current == "answer":
                        answer += "\n" + line
                if question and answer:
                    examples.append((question.strip(), answer.strip(), [f"{database}.{table}".strip(".")]))
        return examples

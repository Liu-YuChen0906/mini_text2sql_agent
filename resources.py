"""集中准备 Mini 查询流程使用的配置、模型、目录和向量索引。"""

import sqlite3
from dataclasses import dataclass
from importlib import metadata, resources
from pathlib import Path

import yaml
from catalog import FileCatalog
from langchain_chroma import Chroma
from langchain_deepseek import ChatDeepSeek
from langchain_openai import OpenAIEmbeddings

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.yaml"
DATABASE_PATH = PROJECT_DIR / "data" / "tracking_orders.sqlite"


@dataclass(frozen=True)
class IndexStores:
    """类用途：用固定名称保存三个 Chroma 检索集合。

    支持功能：通过 columns、table_selection_example、text2sql 属性分别取得
    字段、选表示例和 SQL 示例索引，避免调用方依赖字符串键名。
    """

    columns: Chroma
    table_selection_example: Chroma
    text2sql: Chroma


@dataclass(frozen=True)
class RuntimeResources:
    """类用途：保存一次查询流程共同使用的已初始化依赖。

    支持功能：向主流程提供文件目录、三个向量集合、聊天模型和数据库路径；
    本类只承载资源，不执行检索、生成 SQL 或数据库查询。
    """

    catalog: FileCatalog
    indexes: IndexStores
    llm: ChatDeepSeek
    database_path: Path


def _check_chroma_migrations(persist_directory: str | Path) -> None:
    """用途：在启动 Chroma 前识别持久化索引与已安装版本的迁移不兼容。

    参数输入：
        persist_directory（str | Path）：Chroma 持久化目录；只读取其中的
            chroma.sqlite3，不修改索引内容。
    输出：None；索引不存在或已应用迁移数不超过本地版本时正常返回。
    异常：索引记录的迁移数超过已安装 Chroma 包所含迁移数时抛 RuntimeError，
        避免底层 Rust 客户端因版本倒退而触发难读的 panic。
    """
    database_path = Path(persist_directory) / "chroma.sqlite3"
    if not database_path.is_file():
        return

    with sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        applied = connection.execute("SELECT dir, COUNT(*) FROM migrations GROUP BY dir").fetchall()
    migration_root = resources.files("chromadb").joinpath("migrations")
    for directory, count in applied:
        source_directory = migration_root.joinpath(directory)
        available = (
            sum(item.name.endswith(".sql") for item in source_directory.iterdir()) if source_directory.is_dir() else 0
        )
        if count > available:
            version = metadata.version("chromadb")
            raise RuntimeError(
                f"Chroma 索引需要比当前 chromadb {version} 更新的版本："
                f"{directory} 已应用 {count} 个迁移，本地只有 {available} 个。"
                "请使用兼容版本，或在保留旧索引后重建索引。"
            )


def initialize_chroma(
    catalog: FileCatalog,
    embedding: OpenAIEmbeddings,
    persist_directory: str | Path,
    *,
    build_missing: bool = False,
) -> IndexStores:
    """用途：打开并校验三个 Chroma 集合，按原有规则决定是否重建。

    参数输入：
        catalog（FileCatalog）：提供字段文本和两类示例问题。
        embedding（OpenAIEmbeddings）：检索与建索引使用的嵌入模型。
        persist_directory（str | Path）：三个集合共用的持久化目录。
        build_missing（bool）：默认 False；集合缺失或文档文本不一致时是否重建。
    输出：
        IndexStores：包含 columns、table_selection_example、text2sql 三个集合。
            字段集合保留字段元数据，另外两个集合只索引问题文本。
    异常：默认情况下，集合缺失或文档文本不一致时抛 RuntimeError。
    """
    _check_chroma_migrations(persist_directory)
    columns = catalog.get_column_list()
    sources = {
        "columns": (
            [f"{column['column_name']}: {column['display_name']}" for column in columns],
            columns,
        ),
        "table_selection_example": (
            [question for question, _ in catalog.get_table_selection_examples()] or [""],
            None,
        ),
        "text2sql": ([question for question, _, _ in catalog.get_sql_examples()], None),
    }
    stores = {}
    for name, (texts, metadatas) in sources.items():
        store = Chroma(
            collection_name=name,
            embedding_function=embedding,
            persist_directory=str(persist_directory),
            collection_metadata={"hnsw:space": "cosine"},
        )
        existing = store.get(include=["documents"])["documents"]
        if sorted(existing or []) != sorted(texts):
            if not build_missing:
                raise RuntimeError(f"Chroma 集合 {name} 缺失或与 Catalog 不一致，需要重新建立索引。")
            if existing:
                store.reset_collection()
            if texts:
                store = Chroma.from_texts(
                    texts,
                    embedding,
                    metadatas=metadatas,
                    collection_name=name,
                    collection_metadata={"hnsw:space": "cosine"},
                    persist_directory=str(persist_directory),
                )
        stores[name] = store
    return IndexStores(**stores)


def load_catalog_and_chroma(config_path: str | Path) -> tuple[FileCatalog, IndexStores]:
    """用途：根据 YAML 配置准备文件目录和三个已有向量集合。

    参数输入：
        config_path（str | Path）：Mini 配置文件路径；相对数据目录和索引目录
            均相对于此文件所在目录解析。
    输出：
        tuple[FileCatalog, IndexStores]：依次为目录对象和带名称属性的索引集合。
    异常：存储或嵌入模型类型不支持时抛 ValueError；缺少索引时沿用原有
        RuntimeError，不自动重建。
    """
    config_path = Path(config_path)
    with config_path.open(encoding="utf-8") as file:
        config = yaml.safe_load(file) or {}

    catalog_config = config.get("catalog_store") or {}
    if catalog_config.get("store_type") != "file_system":
        raise ValueError("Mini 的 catalog_store.store_type 必须是 file_system。")
    data_path = Path(catalog_config["data_path"])
    if not data_path.is_absolute():
        data_path = config_path.parent / data_path
    catalog = FileCatalog(data_path)

    embedding_config = config.get("embedding_model") or {}
    if embedding_config.get("class") != "langchain_openai.OpenAIEmbeddings":
        raise ValueError("embedding_model.class 必须是 langchain_openai.OpenAIEmbeddings。")
    embedding = OpenAIEmbeddings(**embedding_config.get("params", {}))

    vector_path = Path(config.get("vector_db_path", "chroma_db"))
    if not vector_path.is_absolute():
        vector_path = config_path.parent / vector_path
    return catalog, initialize_chroma(catalog, embedding, vector_path)


def load_llm_config(config_path: str | Path = CONFIG_PATH) -> dict:
    """用途：读取并校验 Mini 项目的 DeepSeek 模型参数。

    参数输入：
        config_path（str | Path）：YAML 配置文件路径，默认是 Mini 的 config.yaml。
    输出：
        dict：default_llm.params 字典，供 ChatDeepSeek 构造函数使用；其他参数
            保持 YAML 解析后的类型，不做额外转换。
    异常：文件不存在抛 FileNotFoundError；配置结构、模型类或 API Key 无效
        时抛 ValueError。
    """
    config_file = Path(config_path)
    if not config_file.is_file():
        raise FileNotFoundError(f"找不到 Mini 配置文件：{config_file}")

    with config_file.open(encoding="utf-8") as file:
        config = yaml.safe_load(file)

    if not isinstance(config, dict):
        raise ValueError("Mini 配置文件需要包含 default_llm 配置。")

    llm_config = config.get("default_llm")
    if not isinstance(llm_config, dict):
        raise ValueError("Mini 配置文件缺少 default_llm。")
    if llm_config.get("class") != "langchain_deepseek.ChatDeepSeek":
        raise ValueError("Mini 当前只支持 langchain_deepseek.ChatDeepSeek。")

    params = llm_config.get("params")
    if not isinstance(params, dict):
        raise ValueError("default_llm 缺少 params。")
    api_key = params.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip() or api_key == "YOUR_API_KEY_HERE":
        raise ValueError("请在 mini_openchatbi/config.yaml 中填写 DeepSeek API Key。")

    return params


def create_llm(config_path: str | Path = CONFIG_PATH) -> ChatDeepSeek:
    """用途：根据 Mini 配置创建一个聊天模型对象。

    参数输入：
        config_path（str | Path）：包含 default_llm.params 的 YAML 配置路径。
    输出：
        ChatDeepSeek：可用于 invoke 和 LangChain 链的模型对象；构造时不发送请求。
    """
    return ChatDeepSeek(**load_llm_config(config_path))


def load_resources(
    config_path: str | Path = CONFIG_PATH, database_path: str | Path = DATABASE_PATH
) -> RuntimeResources:
    """用途：一次性组装主流程需要的目录、索引、模型与数据库路径。

    参数输入：
        config_path（str | Path）：Mini 的 YAML 配置路径；默认使用项目配置。
        database_path（str | Path）：SQLite 文件路径；默认使用示例数据库。
    输出：
        RuntimeResources：四种依赖的带名称容器；模型只创建一次，由后续
            选表、SQL 生成和结果解读共享。目录和索引先于模型加载。
    """
    catalog, indexes = load_catalog_and_chroma(config_path)
    return RuntimeResources(catalog, indexes, create_llm(config_path), Path(database_path))

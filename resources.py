"""集中准备 Mini 查询流程使用的配置、模型、目录和向量索引。"""

import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from tempfile import TemporaryDirectory

import chromadb
import yaml
from catalog import FileCatalog
from chromadb.config import Settings
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


@lru_cache(maxsize=1)
def _runtime_chroma_migrations() -> dict[tuple[str, int], str]:
    """用途：由当前 Chroma 运行库创建空库，读取它实际支持的迁移与哈希。

    Chroma 的部分迁移内置于 Rust 运行库，仅统计 Python 包中的 SQL 文件会误判。
    临时库不包含项目 Catalog 或业务数据，也不会调用嵌入服务。
    """
    with TemporaryDirectory() as directory:
        chromadb.PersistentClient(path=directory, settings=Settings(anonymized_telemetry=False))
        database_path = Path(directory) / "chroma.sqlite3"
        with sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            return {(name, version): digest for name, version, digest in connection.execute(
                "SELECT dir, version, hash FROM migrations"
            )}


def _check_chroma_migrations(persist_directory: str | Path) -> None:
    """用途：在启动 Chroma 前识别持久化索引与已安装版本的迁移不兼容。

    参数输入：
        persist_directory（str | Path）：Chroma 持久化目录；只读取其中的
            chroma.sqlite3，不修改索引内容。
    输出：None；索引不存在或全部迁移与当前运行库一致时正常返回。
    异常：索引含当前运行库没有的迁移或同版本迁移哈希不一致时抛 RuntimeError，
        避免底层客户端因版本倒退而触发难读的错误。
    """
    database_path = Path(persist_directory) / "chroma.sqlite3"
    if not database_path.is_file():
        return

    with sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        applied = connection.execute("SELECT dir, version, hash FROM migrations").fetchall()
    supported = _runtime_chroma_migrations()
    version = metadata.version("chromadb")
    for directory, migration_version, digest in applied:
        key = (directory, migration_version)
        if key not in supported:
            raise RuntimeError(
                f"Chroma 索引需要比当前 chromadb {version} 更新的版本："
                f"{directory} 存在不受支持的第 {migration_version} 次迁移。"
                "请使用兼容版本，或在保留旧索引后重建索引。"
            )
        if digest != supported[key]:
            raise RuntimeError(
                f"Chroma 索引与当前 chromadb {version} 的迁移内容不一致："
                f"{directory} 第 {migration_version} 次迁移的哈希不同。"
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


def _read_config(config_path: str | Path) -> dict:
    """用途：读取 Mini 的 YAML 配置，供各资源加载函数复用。

    参数输入：config_path 是配置文件路径，可以是字符串或 Path。
    输出：dict，为解析后的完整配置。
    异常：文件不存在时抛 FileNotFoundError；内容不是映射时抛 ValueError。
    """
    config_file = Path(config_path)
    if not config_file.is_file():
        raise FileNotFoundError(f"找不到 Mini 配置文件：{config_file}")
    with config_file.open(encoding="utf-8") as file:
        config = yaml.safe_load(file)
    if not isinstance(config, dict):
        raise ValueError("Mini 配置文件需要包含 default_llm 配置。")
    return config


def load_catalog(config: dict, config_path: str | Path) -> FileCatalog:
    """用途：根据已解析配置加载文件式业务目录。

    参数输入：config 含 catalog_store；config_path 用于解析相对数据目录。
    输出：FileCatalog，提供表、字段和示例的查询接口。
    异常：存储类型不支持时抛 ValueError；目录文件缺失时抛 FileNotFoundError。
    """
    catalog_config = config.get("catalog_store") or {}
    if catalog_config.get("store_type") != "file_system":
        raise ValueError("Mini 的 catalog_store.store_type 必须是 file_system。")
    data_path = Path(catalog_config["data_path"])
    if not data_path.is_absolute():
        data_path = Path(config_path).parent / data_path
    return FileCatalog(data_path)


def load_chroma(config: dict, catalog: FileCatalog, config_path: str | Path) -> IndexStores:
    """用途：配置嵌入模型，打开并校验 Catalog 对应的三个 Chroma 索引。

    参数输入：config 含 embedding_model 和 vector_db_path；catalog 提供索引文本；
        config_path 用于解析相对索引目录。
    输出：IndexStores，包含字段、选表示例和 SQL 示例三个集合。
    异常：嵌入模型类型不支持时抛 ValueError；索引缺失或与 Catalog 不一致时
        沿用 initialize_chroma 的 RuntimeError，不自动重建。
    """
    embedding_config = config.get("embedding_model") or {}
    if embedding_config.get("class") != "langchain_openai.OpenAIEmbeddings":
        raise ValueError("embedding_model.class 必须是 langchain_openai.OpenAIEmbeddings。")
    embedding = OpenAIEmbeddings(**embedding_config.get("params", {}))

    vector_path = Path(config.get("vector_db_path", "chroma_db"))
    if not vector_path.is_absolute():
        vector_path = Path(config_path).parent / vector_path
    return initialize_chroma(catalog, embedding, vector_path)


def _llm_params(config: dict) -> dict:
    """用途：从已解析配置中提取并校验 DeepSeek 模型参数。

    参数输入：config 是完整 YAML 配置的字典。
    输出：dict，可直接传给 ChatDeepSeek 构造函数的 params。
    异常：模型类、参数结构或 API Key 无效时抛 ValueError。
    """
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


def create_llm(config_path: str | Path = CONFIG_PATH, *, config: dict | None = None) -> ChatDeepSeek:
    """用途：根据 Mini 配置创建一个聊天模型对象。

    参数输入：
        config_path（str | Path）：包含 default_llm.params 的 YAML 配置路径。
        config（dict | None）：已解析的配置；传入时不重复读取配置文件。
    输出：
        ChatDeepSeek：可用于 invoke 和 LangChain 链的模型对象；构造时不发送请求。
    """
    params = _llm_params(config if config is not None else _read_config(config_path))
    return ChatDeepSeek(**params)


def load_resources(
    config_path: str | Path = CONFIG_PATH, database_path: str | Path = DATABASE_PATH,
    *, llm: ChatDeepSeek | None = None,
) -> RuntimeResources:
    """用途：一次性组装主流程需要的目录、索引、模型与数据库路径。

    参数输入：
        config_path（str | Path）：Mini 的 YAML 配置路径；默认使用项目配置。
        database_path（str | Path）：SQLite 文件路径；默认使用示例数据库。
        llm（ChatDeepSeek | None）：主 Agent 已创建的模型；传入时直接复用。
    输出：
        RuntimeResources：四种依赖的带名称容器；模型只创建一次，由后续
            选表、SQL 生成和结果解读共享。目录和索引先于模型加载。
    """
    config = _read_config(config_path)
    catalog = load_catalog(config, config_path)
    indexes = load_chroma(config, catalog, config_path)
    llm = llm or create_llm(config_path, config=config)
    return RuntimeResources(catalog, indexes, llm, Path(database_path))

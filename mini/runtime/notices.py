"""查询模块可复用的运行提示，避免业务代码依赖终端排版模块。"""

from rich.console import Console


def show_retrieval_warning(message: str) -> None:
    """向标准错误输出写入一条检索降级提示。

    参数输入：message 为可展示给本地运行者的中文说明。
    输出：None；提示写入 stderr，不改变查询状态。
    """
    Console(stderr=True, markup=False, highlight=False).print(message, style="yellow")

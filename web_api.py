"""Mini 网页 ASGI 兼容入口：uvicorn web_api:app。"""

from mini.web.api import app, create_app

__all__ = ["app", "create_app"]

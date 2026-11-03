"""FastAPI entry point for the local Mini OpenChatBI browser UI."""

from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from mini.runtime.resources import PROJECT_DIR
from mini.web.service import ServiceError, WebSessionService


class MessageInput(BaseModel):
    """网页新提问的正文和幂等请求 ID。"""

    content: str
    request_id: str


class ResumeInput(BaseModel):
    """恢复澄清或审核暂停点所需的交互 ID 与用户决定。"""

    request_id: str
    interaction_id: str
    answer: str | None = None
    decision: str | None = None
    feedback: str | None = None


def create_app(service_factory: Callable[[], WebSessionService] | None = None) -> FastAPI:
    """创建网页应用；可注入服务工厂供离线测试使用。

    参数输入：service_factory 创建持久会话服务，省略时使用真实服务。
    输出：含会话 API 和可选静态前端的 FastAPI 应用。
    """
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """在应用生命周期内持有服务，退出时关闭检查点连接。"""
        app.state.service = (service_factory or WebSessionService)()
        try:
            yield
        finally:
            app.state.service.close()

    app = FastAPI(title="Mini OpenChatBI", lifespan=lifespan)

    def service() -> WebSessionService:
        """读取当前应用生命周期创建的会话服务。"""
        return app.state.service

    async def call(fn, *args):
        """在线程池运行同步服务函数，并把业务错误转为 HTTP 错误。"""
        try:
            return await run_in_threadpool(fn, *args)
        except ServiceError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc

    @app.get("/api/health")
    def health():
        """返回服务存活状态。"""
        return {"status": "ok"}

    @app.post("/api/sessions", status_code=201)
    async def create_session():
        """新建一个独立的网页会话。"""
        return await call(service().create_session)

    @app.get("/api/sessions")
    async def list_sessions():
        """按最近更新时间列出网页会话。"""
        return await call(service().list_sessions)

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str):
        """读取指定会话的状态、待处理交互与页面消息。"""
        return await call(service().get_session, session_id)

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str):
        return await call(service().delete_session, session_id)

    @app.post("/api/sessions/{session_id}/messages")
    async def send_message(session_id: str, body: MessageInput):
        """提交一轮用户问题；相同请求 ID 可安全查询已有结果。"""
        return await call(service().send_message, session_id, body.content, body.request_id)
      
    @app.post("/api/sessions/{session_id}/resume")
    async def resume(session_id: str, body: ResumeInput):
        """提交澄清回答或人工审核决定，继续同一轮图执行。"""
        return await call(service().resume, session_id, body.request_id, body.interaction_id,
                          body.answer, body.decision, body.feedback)

    @app.post("/api/sessions/{session_id}/retry")
    async def retry(session_id: str):
        """显式恢复上次中断的网页请求。"""
        return await call(service().retry, session_id)

    @app.post("/api/sessions/{session_id}/analysis/{run_id}/cancel")
    async def cancel_analysis(session_id: str, run_id: str):
        return await call(service().cancel_analysis, session_id, run_id)

    @app.get("/api/sessions/{session_id}/analysis/{run_id}")
    async def analysis_report(session_id: str, run_id: str):
        return await call(service().analysis_record, session_id, "run", run_id)

    @app.get("/api/sessions/{session_id}/evidence/{evidence_id}")
    async def evidence(session_id: str, evidence_id: str):
        return await call(service().analysis_record, session_id, "evidence", evidence_id)

    @app.get("/api/sessions/{session_id}/datasets/{dataset_id}")
    async def dataset(session_id: str, dataset_id: str, offset: int = 0, limit: int = 50):
        if offset < 0 or not 1 <= limit <= 100:
            raise HTTPException(422, "分页参数无效")
        meta, snapshot = await call(service().analysis_record, session_id, "dataset", dataset_id)
        return {"metadata": meta, "columns": snapshot["columns"], "rows": snapshot["rows"][offset:offset+limit], "offset":offset}

    @app.get("/api/sessions/{session_id}/artifacts/{artifact_id}")
    async def artifact(session_id: str, artifact_id: str):
        meta, content = await call(service().analysis_record, session_id, "artifact", artifact_id)
        # Names are not interpolated into headers. Text/JSON never executes as HTML.
        media = meta["media_type"] if meta["media_type"] in {"application/json", "text/plain", "image/png"} else "application/octet-stream"
        return Response(content, media_type=media, headers={"Content-Disposition":f'attachment; filename="{artifact_id}"',
                        "X-Content-Type-Options":"nosniff", "Content-Security-Policy":"default-src 'none'", "Cache-Control":"no-store"})

    dist = PROJECT_DIR / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def frontend(path: str):
            """返回构建产物；前端路由回退到 index.html。"""
            target = (dist / path).resolve()
            if path and target.is_relative_to(dist.resolve()) and target.is_file():
                return FileResponse(target)
            return FileResponse(dist / "index.html")

    return app


app = create_app()

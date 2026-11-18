from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import BASE_DIR, settings
from .db import init_db
from .routers import api, web
from .services import scheduler
from .web import templates

STATIC_DIR = BASE_DIR / "app" / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    task = None
    if settings.scheduler_enabled:
        task = asyncio.create_task(scheduler.loop(), name="schedule-sync")
        task.add_done_callback(_scheduler_done)
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


def _scheduler_done(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        scheduler.logger.error("Фоновая синхронизация остановлена: %s", error)


app = FastAPI(
    title=settings.app_name,
    description=(
        "Неофициальный помощник старосты: расписание ПолесГУ, журнал посещаемости, "
        "распоряжения, заявления и справки."
    ),
    version="0.1.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(web.router)
app.include_router(api.router)


@app.get("/health", include_in_schema=False)
def health() -> dict:
    return {"status": "ok"}


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    wants_json = request.url.path.startswith("/api") or "application/json" in request.headers.get(
        "accept", ""
    )
    if wants_json:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    titles = {
        401: "Нужна авторизация",
        403: "Доступ запрещён",
        404: "Страница не найдена",
        422: "Неверный запрос",
    }
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "request": request,
            "code": exc.status_code,
            "title": titles.get(exc.status_code, "Ошибка"),
            "message": str(exc.detail),
        },
        status_code=exc.status_code,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=settings.dev_mode)

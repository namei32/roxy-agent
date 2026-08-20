from __future__ import annotations

from fastapi import FastAPI, WebSocket
import uvicorn

from agent.config_models import NotesBridgeConfig

from .broker import NotesBridgeBroker


def build_notes_bridge_app(broker: NotesBridgeBroker) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return broker.connection_status()

    @app.websocket("/ws")
    async def notes_bridge_socket(websocket: WebSocket) -> None:
        await broker.handle_websocket(websocket)

    return app


def build_notes_bridge_server(
    config: NotesBridgeConfig,
    broker: NotesBridgeBroker,
) -> uvicorn.Server:
    uvicorn_config = uvicorn.Config(
        build_notes_bridge_app(broker),
        host=config.host,
        port=config.port,
        log_level="info",
        access_log=False,
    )
    return uvicorn.Server(uvicorn_config)


__all__ = ["build_notes_bridge_app", "build_notes_bridge_server"]

"""隔离浏览器验收服务器；仅使用临时模型库和本地合成模型接口。"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import importlib.util
import tempfile
import os

os.environ["NO_PROXY"] = "localhost,127.0.0.1"
os.environ["no_proxy"] = "localhost,127.0.0.1"
from types import SimpleNamespace
from contextlib import asynccontextmanager
import uvicorn
from fastapi.responses import HTMLResponse, FileResponse
from fastapi import Request
from agent.config_models import Config
from agent.model_runtime.store import ModelRegistryStore
from agent.model_runtime.management import ModelManagementService
from agent.plugins.mobile_ui import PluginMobileUiProvider
from bootstrap.providers import build_model_registry
from bootstrap.chat_api import create_chat_app
from infra.channels.web_chat_channel import WebChatChannel

root = Path(tempfile.mkdtemp(prefix="model-manager-preview-"))
config = root / "config.toml"
config.write_text('[agent]\nsystem_prompt="test"\n')
store = ModelRegistryStore.for_workspace(root)
store.replace_from_llm_config(
    {
        "main": "original",
        "runtimes": {
            "original": {
                "model": "original-model",
                "provider": "openai",
                "context_window": 64000,
                "base_url": "http://127.0.0.1:18974/v1",
            }
        },
    }
)
registry = build_model_registry(Config.load(config, workspace=root))
service = ModelManagementService(root, registry)
parser = argparse.ArgumentParser()
parser.add_argument("--plugin-source", type=Path, required=True)
source = parser.parse_args().plugin_source.resolve()
spec = importlib.util.spec_from_file_location(
    "model_manager_preview", source / "plugin.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
plugin = mod.ModelManager()
plugin.context = SimpleNamespace(require_runtime_service=lambda name: service)
asset = SimpleNamespace(
    navigation_label="模型管理", navigation_description="管理模型", slots=()
)
generation = SimpleNamespace(
    plugin_id="model-manager@local",
    source_revision="preview",
    instance=plugin,
    contributions=SimpleNamespace(mobile_ui_asset=asset),
)
snapshot = SimpleNamespace(
    generations={"model-manager@local": generation},
    active_generations=lambda: (generation,),
)


class Store:
    @asynccontextmanager
    async def _lease(self):
        yield snapshot

    async def acquire(self):
        return self._lease()


provider = PluginMobileUiProvider(
    SimpleNamespace(current_snapshot=snapshot, snapshot_store=Store())
)
channel = WebChatChannel()
app = create_chat_app(
    workspace=root,
    channel=channel,
    plugin_ui_provider=provider,
    model_registry=registry,
)


@app.get("/preview")
async def preview():
    return HTMLResponse(
        """<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1"><meta charset="utf-8"><link rel="stylesheet" href="/panel.css"><style>:root{--roxy-color-text-primary:#242424;--roxy-color-text-secondary:#666;--roxy-color-bg-surface:#fff;--roxy-color-bg-surface-low:#f6f6f3;--roxy-color-bg-surface-high:#eee;--roxy-color-border-default:#ddd;--roxy-color-action-primary:#315a46;--roxy-color-on-action-primary:#fff;--roxy-color-status-error:#b33434}body{margin:0;font-family:system-ui} </style><main id="app"></main><script type="module">import panel from '/panel.js';async function call(kind,method,payload){const r=await fetch('/api/chat/plugin-ui/'+kind,{method:'POST',headers:{'content-type':'application/json','x-roxy-csrf':'1'},body:JSON.stringify({plugin_id:'model-manager@local',plugin_revision:'preview',method,payload,slot:kind==='action'?'dashboard.main':'drawer.panel'})});const body=await r.json();if(!r.ok)throw Error(body.detail);return body}panel.dashboard.mount(document.querySelector('#app'),{query:(m,p)=>call('query',m,p),action:(m,p)=>call('action',m,p)});</script>"""
    )


@app.get("/panel.js")
async def js():
    return FileResponse(source / "panel.js", media_type="text/javascript")


@app.get("/panel.css")
async def css():
    return FileResponse(source / "panel.css", media_type="text/css")


@app.get("/v1/models")
async def models():
    return {"data": [{"id": "phone-test-model"}]}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    app.state.last_model = body["model"]
    return {
        "id": "test-response",
        "object": "chat.completion",
        "created": 1,
        "model": body["model"],
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "OK"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@app.post("/exercise")
async def exercise(request: Request):
    body = await request.json()
    async with registry.execution_scope(body["model_id"]):
        response = await registry.provider("default").chat(
            messages=[{"role": "user", "content": "Reply OK."}],
            tools=[],
            model="ignored-by-binding",
            max_tokens=32,
        )
    return {"content": response.content, "actual_model": app.state.last_model}


@app.get("/proof")
async def proof():
    return {
        "last_model": getattr(app.state, "last_model", None),
        "revision": store.revision(),
    }


uvicorn.run(app, host="127.0.0.1", port=18974, log_level="warning")

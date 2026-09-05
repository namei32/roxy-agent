"""Serve the shipped mobile plugin against an owned SQLite fixture, never real data."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plugins.akasha.inspector import AkashaInspectorReader, InspectorPaths
from plugins.akasha.plugin import AkashaPlugin
from tests.test_akasha_graph import graph_fixture

HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Akasha 插件 · 隔离验证</title><link rel="stylesheet" href="/mobile_ui.css">
<style>body{margin:0;background:#f7f9fc;font-family:system-ui,sans-serif}aside{font-size:12px;padding:10px;text-align:center;background:#eef4fb;color:#63748a}main{max-width:620px;margin:auto}</style>
<aside>开发验证 · 隔离 SQLite 样例 · 此页未连接正式记忆</aside><main id="plugin"></main>
<script type="module">
import plugin from '/mobile_ui.js';
window.queryLog=[];
window.cleanup=plugin.dashboard.mount(document.querySelector('#plugin'),{
  capabilities:{queryTransports:['https']},
  query:async(method,payload={},options={})=>{
    const start=performance.now();
    const reply=await fetch('/query',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({method,payload})});
    const result=await reply.json();
    window.queryLog.push({method,payload,options,ms:performance.now()-start,bytes:JSON.stringify(result).length});
    if(!reply.ok)throw new Error(result.message);
    return result;
  }
});
</script></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--turns", type=int, choices=(26, 1000), default=26)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="roxy-akasha-graph-fixture-") as directory:
        workspace = Path(directory)
        reader = graph_fixture(workspace, args.turns)
        plugin = AkashaPlugin()
        plugin.context = SimpleNamespace(
            workspace=workspace,
            memory_engine=SimpleNamespace(
                describe=lambda: SimpleNamespace(name="akasha"),
                inspect_graph=reader.query,
            ),
        )
        plugin._reader = AkashaInspectorReader(workspace)
        plugin._reader.paths = InspectorPaths(memory=reader.memory, index=reader.index)

        class Handler(BaseHTTPRequestHandler):
            def send(self, code: int, content: bytes, kind: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def do_GET(self) -> None:
                if self.path == "/":
                    self.send(200, HTML.encode(), "text/html; charset=utf-8")
                elif self.path in ("/mobile_ui.js", "/mobile_ui.css"):
                    asset = ROOT / "plugins/akasha" / self.path.lstrip("/")
                    self.send(
                        200,
                        asset.read_bytes(),
                        "text/javascript" if asset.suffix == ".js" else "text/css",
                    )
                else:
                    self.send(404, b"not found", "text/plain")

            def do_POST(self) -> None:
                if self.path != "/query":
                    self.send(404, b"{}", "application/json")
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size < 16 * 1024:
                        raise ValueError("invalid fixture request size")
                    value = json.loads(self.rfile.read(size))
                    result = plugin.mobile_ui_query(
                        value["method"], value["payload"], session_id=None, turn_id=None
                    )
                    self.send(
                        200,
                        json.dumps(
                            result, ensure_ascii=False, allow_nan=False
                        ).encode(),
                        "application/json",
                    )
                except Exception as exc:
                    self.send(
                        400,
                        json.dumps({"message": str(exc)}, ensure_ascii=False).encode(),
                        "application/json",
                    )

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        print(
            json.dumps(
                {
                    "url": f"http://127.0.0.1:{server.server_port}/",
                    "workspace": directory,
                    "turns": args.turns,
                }
            ),
            flush=True,
        )
        try:
            server.serve_forever()
        finally:
            server.server_close()


if __name__ == "__main__":
    main()

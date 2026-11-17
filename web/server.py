"""网页界面后端：把 Agent 的每一步通过 SSE 实时推给浏览器。

运行：uv run python web/server.py   然后打开 http://127.0.0.1:8000
"""
import asyncio
import csv
import json
import sys
from pathlib import Path

import uvicorn
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))
import main as agent  # noqa: E402


async def index(request):
    return FileResponse(ROOT / "web" / "index.html")


async def wafers(request):
    """晶圆良率数据，给前端画晶圆格子（这是展示用，不经过 Agent）。"""
    with open(ROOT / "data" / "test_yield.csv") as f:
        rows = list(csv.DictReader(f))
    return JSONResponse([{"wafer": r["wafer"], "yield": float(r["yield_pct"]),
                          "pattern": r["fail_pattern"]} for r in rows])


async def run(request):
    question = request.query_params.get("q") or "LOT-A1024 的良率从 92% 掉到 86%，帮我查一下原因。"
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(e):
        await queue.put(e)

    async def worker():
        try:
            await agent.run(question, emit)
        except Exception as ex:  # 让前端看到错误而不是一直转圈
            await queue.put({"type": "error", "message": f"{type(ex).__name__}: {ex}"})
        await queue.put(None)

    task = asyncio.create_task(worker())

    async def stream():
        while (e := await queue.get()) is not None:
            yield {"data": json.dumps(e, ensure_ascii=False)}
        yield {"data": json.dumps({"type": "done"})}
        await task

    return EventSourceResponse(stream())


app = Starlette(routes=[Route("/", index), Route("/api/wafers", wafers), Route("/api/run", run)])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)

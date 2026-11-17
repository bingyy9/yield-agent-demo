"""网页界面后端（V2）。

- /api/run      SSE：把 Agent 每一步实时推给浏览器；带 sid 就在同一个会话里追问 [2]
- /api/approve  工程师审批后执行操作（扣货/停机/开单），系统直接调工具，不经过模型 [V3/10]
- /api/wafers   从 SQLite 读晶圆数据画晶圆图
- /api/timeline 保养/报警/加工/测试时间线 [13]
- /api/traces   历史排查轨迹列表 + 单条回放 [8]
- /api/export   把报告导出成 Word [12]
运行：uv run python web/server.py   然后打开 http://127.0.0.1:8000
"""
import asyncio
import io
import json
import sys
import uuid
from pathlib import Path

import uvicorn
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "servers"))
import fabdb  # noqa: E402
import main as agent  # noqa: E402

SESSIONS: dict[str, agent.Session] = {}  # sid -> 活着的会话（保留对话历史，支持追问）
LOCKS: dict[str, asyncio.Lock] = {}


async def get_session(sid: str | None):
    if sid and sid in SESSIONS:
        return sid, SESSIONS[sid]
    sid = uuid.uuid4().hex[:8]
    s = agent.Session(quiet=True)
    await s.__aenter__()
    SESSIONS[sid], LOCKS[sid] = s, asyncio.Lock()
    return sid, s


async def index(request):
    return FileResponse(ROOT / "web" / "index.html")


async def wafers(request):
    lot = fabdb.norm_lot(request.query_params.get("lot", "LOT-A1024"))
    rs = fabdb.rows("SELECT wafer, yield_pct, fail_pattern FROM test_yield WHERE lot=? ORDER BY wafer", (lot,))
    return JSONResponse({"lot": lot, "wafers": [{"wafer": r["wafer"], "yield": r["yield_pct"], "pattern": r["fail_pattern"]} for r in rs]})


async def timeline(request):
    q = request.query_params
    return JSONResponse(fabdb.timeline(q.get("lot", "LOT-A1024"), q.get("tool", "")))


async def run(request):
    q = request.query_params
    question = q.get("q") or "LOT-A1024 的良率从 92% 掉到 86%，帮我查一下原因。"
    sid, sess = await get_session(q.get("sid"))
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(e):
        await queue.put(e)

    async def worker():
        async with LOCKS[sid]:
            await queue.put({"type": "session", "sid": sid, "followup": sess.last_report is not None,
                             "tools": [t["function"]["name"] for t in sess.tools]})
            try:
                await sess.ask(question, emit)
            except Exception as ex:
                await queue.put({"type": "error", "message": f"{type(ex).__name__}: {ex}"})
        await queue.put(None)

    task = asyncio.create_task(worker())

    async def stream():
        while (e := await queue.get()) is not None:
            yield {"data": json.dumps(e, ensure_ascii=False, default=str)}
        yield {"data": json.dumps({"type": "done"})}
        await task

    return EventSourceResponse(stream())


async def approve(request):
    b = await request.json()
    if not (b.get("approved_by") or "").strip():
        return JSONResponse({"error": "必须填写审批人"}, status_code=400)
    _, sess = await get_session(b.get("sid"))
    res = await sess.execute_action(b["kind"], b["target"], b.get("reason", ""), b["approved_by"].strip())
    return JSONResponse(res)


async def traces(request):
    d = ROOT / "traces"
    tid = request.query_params.get("id")
    if tid:
        f = d / f"{Path(tid).name}.jsonl"
        if not f.exists():
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse([json.loads(l) for l in f.read_text().splitlines() if l.strip()])
    out = []
    for f in sorted(d.glob("*.jsonl"), reverse=True)[:40]:
        lines = f.read_text().splitlines()
        if not lines:
            continue
        first, last = json.loads(lines[0]), json.loads(lines[-1])
        out.append({"id": f.stem, "question": first.get("question", ""), "end": last.get("end", "未结束"),
                    "root": (last.get("report") or {}).get("root_tool"), "seconds": last.get("seconds")})
    return JSONResponse(out)


async def export(request):
    """[12] 报告导出 Word：工厂里报告要归档、要发邮件，Word 是最通用的格式。"""
    from docx import Document
    r = await request.json()
    doc = Document()
    doc.add_heading("良率异常排查报告", 0)
    doc.add_paragraph(f"结论：{r.get('conclusion')}（嫌疑设备：{r.get('root_tool')}）")
    doc.add_paragraph(f"置信度：{r.get('confidence')} —— {r.get('confidence_reason', '')}")
    doc.add_heading("证据链", 1)
    for e in r.get("evidence") or []:
        doc.add_paragraph(e, style="List Number")
    for k, t in [("impact", "影响范围"), ("other_findings", "其他发现"), ("next_checks", "建议补查")]:
        if r.get(k):
            doc.add_heading(t, 1)
            doc.add_paragraph(str(r[k]))
    acts = r.get("proposed_actions") or []
    if acts:
        doc.add_heading("建议操作（需审批）", 1)
        tb = doc.add_table(rows=1, cols=3)
        tb.style = "Light Grid Accent 1"
        for c, h in zip(tb.rows[0].cells, ["操作", "对象", "理由"]):
            c.text = h
        for a in acts:
            for c, v in zip(tb.add_row().cells, [a.get("kind"), a.get("target"), a.get("reason")]):
                c.text = str(v or "")
    doc.add_paragraph("\n本报告由良率排查 Agent 自动生成，结论需工程师确认。")
    buf = io.BytesIO()
    doc.save(buf)
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": "attachment; filename=yield_report.docx"})


app = Starlette(routes=[
    Route("/", index), Route("/api/wafers", wafers), Route("/api/timeline", timeline),
    Route("/api/run", run), Route("/api/approve", approve, methods=["POST"]),
    Route("/api/traces", traces), Route("/api/export", export, methods=["POST"]),
])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)

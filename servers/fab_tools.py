"""晶圆厂工具集：4 个工具，以 MCP 服务的形式对外提供。

每个工具对应工厂里的一个系统（V1 用合成数据模拟）：
  get_lot_yield         ← 测试系统 / YMS（良率管理系统）
  commonality_analysis  ← MES（制造执行系统）的加工履历
  get_equipment_alarms  ← 设备报警系统 / FDC
  search_8d_reports     ← 质量知识库（历史 8D 报告）

真实落地时，只要把函数内部的"读 CSV"换成调用工厂内部接口，Agent 端完全不用改。

单独运行（stdio 模式，供 Agent 或 MCP Inspector 连接）：
  uv run python servers/fab_tools.py
"""
import csv
import json
from collections import defaultdict
from pathlib import Path

from mcp.server.mcpserver import MCPServer

DATA = Path(__file__).resolve().parent.parent / "data"
mcp = MCPServer("fab-tools")


def _read_csv(name):
    with open(DATA / name) as f:
        return list(csv.DictReader(f))


def _norm_lot(lot: str) -> str:
    """批次号容错：模型常把 LOT-A1024 写成 A1024 或 lot-a1024，统一成标准格式。"""
    s = lot.strip().upper().replace("_", "-")
    return s if s.startswith("LOT-") else "LOT-" + s.removeprefix("LOT")


def _known_lots() -> list[str]:
    return sorted({r["lot"] for r in _read_csv("test_yield.csv")})


@mcp.tool()
def get_lot_yield(lot: str, low_threshold: float = 85.0) -> dict:
    """查询某个批次每片晶圆的测试良率和失效图案，并标出良率低于阈值的低良率晶圆。"""
    lot = _norm_lot(lot)
    rows = [r for r in _read_csv("test_yield.csv") if r["lot"] == lot]
    if not rows:
        return {"error": f"找不到批次 {lot}", "known_lots": _known_lots()}
    for r in rows:
        r["yield_pct"] = float(r["yield_pct"])
    low = [r for r in rows if r["yield_pct"] < low_threshold]
    patterns = defaultdict(list)
    for r in low:
        patterns[r["fail_pattern"]].append(r["wafer"])
    return {
        "lot": lot,
        "product": rows[0]["product"],
        "wafer_count": len(rows),
        "avg_yield_pct": round(sum(r["yield_pct"] for r in rows) / len(rows), 1),
        "low_yield_threshold": low_threshold,
        "low_yield_wafers": [{"wafer": r["wafer"], "yield_pct": r["yield_pct"],
                              "fail_pattern": r["fail_pattern"]} for r in low],
        "low_yield_by_pattern": {
            k: {"wafers": v, "count": len(v), "share_of_low": f"{len(v)}/{len(low)} ({len(v) / len(low):.0%})"}
            for k, v in sorted(patterns.items(), key=lambda kv: -len(kv[1]))
        },
    }


@mcp.tool()
def commonality_analysis(lot: str, bad_wafers: list[str]) -> dict:
    """共性分析：对比坏片和好片在每个加工步骤经过的设备/腔室，找出坏片集中经过的设备。
    bad_wafers 是要分析的低良率晶圆编号列表，例如 ["W03", "W07"]。"""
    lot = _norm_lot(lot)
    hist = [r for r in _read_csv("process_history.csv") if r["lot"] == lot]
    if not hist:
        return {"error": f"找不到批次 {lot} 的加工履历", "known_lots": _known_lots()}
    bad = set(bad_wafers)
    all_wafers = {r["wafer"] for r in hist}
    good = all_wafers - bad
    stats = defaultdict(lambda: {"bad": 0, "good": 0})
    for r in hist:
        key = (r["step"], r["tool"])
        stats[key]["bad" if r["wafer"] in bad else "good"] += 1
    ranked = []
    for (step, tool), s in stats.items():
        bad_ratio = s["bad"] / len(bad) if bad else 0
        good_ratio = s["good"] / len(good) if good else 0
        ranked.append({"step": step, "tool": tool,
                       "bad_wafers_through": f"{s['bad']}/{len(bad)}",
                       "good_wafers_through": f"{s['good']}/{len(good)}",
                       "score": round(bad_ratio - good_ratio, 2)})
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return {"lot": lot, "bad_wafers": sorted(bad), "top_suspects": ranked[:3],
            "note": "score = 坏片经过比例 - 好片经过比例，越接近 1 越可疑"}


@mcp.tool()
def get_equipment_alarms(tool: str) -> dict:
    """查询某台设备/腔室（例如 ETCH-03/B）近期的报警和保养记录。"""
    rows = [r for r in _read_csv("equipment_alarms.csv") if r["tool"] == tool]
    return {"tool": tool, "count": len(rows), "events": rows}


@mcp.tool()
def search_8d_reports(query: str) -> dict:
    """在历史 8D 质量报告中按关键词搜索相似案例。query 可以包含失效图案、设备类型、现象等，用空格分隔。"""
    reports = json.loads((DATA / "8d_reports.json").read_text())
    terms = [t.lower() for t in query.replace(",", " ").split() if t]
    scored = []
    for r in reports:
        text = " ".join([r["title"], r["symptom"], r["root_cause"], " ".join(r["keywords"])]).lower()
        hits = sum(1 for t in terms if t in text)
        if hits:
            scored.append((hits, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return {"query": query, "matches": [dict(r, match_score=h) for h, r in scored[:3]]}


if __name__ == "__main__":
    mcp.run()  # 默认 stdio 传输

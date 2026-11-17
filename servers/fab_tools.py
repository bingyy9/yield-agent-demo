"""晶圆厂工具集（MCP 服务）。V2。

只读工具（Agent 可直接调用）：
  get_lot_yield          测试良率：哪些片坏了、什么形状
  commonality_analysis   共性分析：坏片共同经过的设备，返回证据强度
  get_equipment_alarms   设备日志：保养和报警
  search_8d_reports      历史事故：语义检索（RAG）+ 关键词
  find_impacted_lots     影响范围：同一设备在某时间后还加工过哪些批次
  query_database         只读 SQL：Agent 自己写查询
  secom_sensor_analysis  真实数据：UCI SECOM 产线传感器 vs 合格/不合格

有操作的工具（destructive，必须工程师审批后由系统执行，Agent 只能"提议"）：
  hold_lots              扣留批次
  set_tool_down          设备停机
  create_ticket          创建维修工单

运行（stdio）：uv run python servers/fab_tools.py
"""
import hashlib
import json
import math
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fabdb as db  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

mcp = MCPServer("fab-tools")
RO = ToolAnnotations(read_only_hint=True, destructive_hint=False)
RW = ToolAnnotations(read_only_hint=False, destructive_hint=True)
EMBED_MODEL = "bge-m3"


def strength(score: float) -> str:
    return "强" if score >= 0.8 else "中" if score >= 0.5 else "弱"


# ------------------------------------------------------------------ 只读工具
@mcp.tool(annotations=RO)
def get_lot_yield(lot: str, low_threshold: float = 85.0) -> dict:
    """查询批次每片晶圆的测试良率和失效图案（坏芯片的分布形状），标出低于阈值的低良率晶圆，并按失效图案分组。"""
    lot = db.norm_lot(lot)
    info = db.rows("SELECT * FROM lots WHERE lot=?", (lot,))
    if not info:
        return {"error": f"找不到批次 {lot}", "known_lots": db.known_lots()}
    ys = db.rows("SELECT wafer, yield_pct, fail_pattern FROM test_yield WHERE lot=? ORDER BY wafer", (lot,))
    if not ys:
        return {"lot": lot, "status": info[0]["status"], "error": "该批次还没有测试数据"}
    low_threshold = min(low_threshold, 88.0)  # 阈值过高会把正常片当坏片，限制上限
    low = [r for r in ys if r["yield_pct"] < low_threshold and r["fail_pattern"] != "None"]
    groups = defaultdict(list)
    for r in low:
        groups[r["fail_pattern"]].append(r["wafer"])
    groups = dict(sorted(groups.items(), key=lambda kv: -len(kv[1])))
    out = {
        "lot": lot, "product": info[0]["product"], "test_time": info[0]["test_time"],
        "wafer_count": len(ys), "avg_yield_pct": round(sum(r["yield_pct"] for r in ys) / len(ys), 1),
        "low_yield_threshold": low_threshold,
        "low_yield_by_pattern": {k: {"wafers": v, "count": len(v), "share_of_low": f"{len(v)}/{len(low)} ({len(v) / len(low):.0%})"}
                                 for k, v in groups.items()},
    }
    if not low:
        out["hint"] = "没有低于阈值的晶圆。如果整体偏低，可以把 low_threshold 调高（如 90）再看。"
    elif len(groups) > 1:
        main = next(iter(groups))
        out["hint"] = (f"低良率晶圆有 {len(groups)} 种失效图案。建议只拿主要图案 {main} 的晶圆做共性分析，"
                       "其他图案可能是独立问题，单独分析。")
    if all(p in ("Random", "None") for p in groups):
        out["hint"] = "低良率晶圆没有明显的失效图案（Random），可能不是单一设备造成的。共性分析仍可一试，但要警惕证据不足。"
    return out


@mcp.tool(annotations=RO)
def commonality_analysis(lot: str, bad_wafers: list[str], combine_steps: bool = False) -> dict:
    """共性分析：对比坏片和好片在每道工序经过的设备，找出坏片集中经过的设备。
    bad_wafers 是要分析的晶圆编号列表（建议只放同一种失效图案的）。
    combine_steps=True 时分析"两道工序的设备组合"，用于单一设备找不到共性时。"""
    lot = db.norm_lot(lot)
    hist = db.rows("SELECT wafer, step, tool FROM process_history WHERE lot=?", (lot,))
    if not hist:
        return {"error": f"找不到批次 {lot} 的加工履历", "known_lots": db.known_lots()}
    bad = {w.strip().upper() for w in bad_wafers}
    route = defaultdict(dict)
    for r in hist:
        route[r["wafer"]][r["step"]] = r["tool"]
    bad &= set(route)
    good = set(route) - bad
    if not bad:
        return {"error": "bad_wafers 为空或都不属于该批次", "wafers": sorted(route)}
    keys = defaultdict(set)
    for w, steps in route.items():
        items = sorted(steps.items())
        if combine_steps:
            for i in range(len(items)):
                for j in range(i + 1, len(items)):
                    keys[(f"{items[i][0]}+{items[j][0]}", f"{items[i][1]} + {items[j][1]}")].add(w)
        else:
            for s, t in items:
                keys[(s, t)].add(w)
    ranked = []
    for (step, tool), ws in keys.items():
        b, g = len(ws & bad), len(ws & good)
        score = b / len(bad) - (g / len(good) if good else 0)
        ranked.append({"step": step, "tool": tool, "bad_wafers_through": f"{b}/{len(bad)}",
                       "good_wafers_through": f"{g}/{len(good)}", "score": round(score, 2)})
    ranked.sort(key=lambda x: -x["score"])
    top = ranked[0]["score"]
    out = {"lot": lot, "bad_wafers": sorted(bad), "mode": "两道工序组合" if combine_steps else "单道工序",
           "top_suspects": ranked[:3], "evidence_strength": strength(top),
           "note": "score = 坏片经过比例 - 好片经过比例；≥0.8 强，0.5~0.8 中，<0.5 弱"}
    if strength(top) != "强":
        out["hint"] = ("没有明显的嫌疑设备。可以尝试：①只保留同一种失效图案的晶圆再分析；"
                       "②combine_steps=True 看工序组合；③如果仍然很弱，如实报告证据不足。")
    return out


@mcp.tool(annotations=RO)
def get_equipment_alarms(tool: str, days: int = 3) -> dict:
    """查询某台设备/腔室（如 ETCH-03/B）最近 days 天的报警和保养记录（PM-DONE 表示保养完成）。"""
    tool = db.norm_tool(tool)
    if not db.rows("SELECT 1 FROM tool_status WHERE tool=?", (tool,)):
        return {"error": f"没有设备 {tool}", "known_tools": [r["tool"] for r in db.rows("SELECT tool FROM tool_status")]}
    since = (db.now() - timedelta(days=days)).strftime(db.TIME_FMT)
    ev = db.rows("SELECT time, level, code, msg FROM equipment_alarms WHERE tool=? AND time>=? ORDER BY time",
                 (tool, since))
    out = {"tool": tool, "days": days, "since": since, "count": len(ev), "events": ev,
           "warn_count": sum(e["level"] == "WARN" for e in ev),
           "last_pm": next((e["time"] for e in reversed(ev) if e["code"] == "PM-DONE"), None)}
    if not ev:
        out["hint"] = (f"最近 {days} 天没有记录。可以把 days 扩大到 14 再查一次。" if days < 14 else
                       f"最近 {days} 天都没有记录。这台设备没有报警证据，在报告里如实说明，并建议人工查看设备传感器原始数据。")
    return out


_emb_cache: dict = {}


def _embed(texts: list[str]) -> list[list[float]]:
    import ollama
    return ollama.embed(model=EMBED_MODEL, input=texts)["embeddings"]


def _kb_vectors(reports):
    key = hashlib.md5(json.dumps(reports, ensure_ascii=False).encode()).hexdigest()
    if key in _emb_cache:
        return _emb_cache[key]
    cache = db.KB_DIR / f"emb_{EMBED_MODEL.replace(':', '_')}.json"
    if cache.exists():
        c = json.loads(cache.read_text())
        if c.get("key") == key:
            _emb_cache[key] = c["vectors"]
            return c["vectors"]
    vecs = _embed([f"{r['title']}。{r['symptom']}。{r['root_cause']}" for r in reports])
    cache.write_text(json.dumps({"key": key, "vectors": vecs}))
    _emb_cache[key] = vecs
    return vecs


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) + 1e-9)


def keyword_search(query: str, reports, k=3):
    terms = [t.lower() for t in re.split(r"[\s,，]+", query) if t]
    scored = []
    for r in reports:
        text = f"{r['title']} {r['symptom']} {r['root_cause']} {r['area']}".lower()
        hits = sum(t in text for t in terms)
        if hits:
            scored.append((hits / len(terms), r))
    scored.sort(key=lambda x: -x[0])
    return scored[:k]


def semantic_search(query: str, reports, k=3):
    vecs = _kb_vectors(reports)
    q = _embed([query])[0]
    scored = sorted(((_cos(q, v), r) for v, r in zip(vecs, reports)), key=lambda x: -x[0])
    return scored[:k]


@mcp.tool(annotations=RO)
def search_8d_reports(query: str, mode: str = "semantic") -> dict:
    """在历史 8D 事故报告中检索相似案例。query 用自然语言描述现象即可，例如"刻蚀机保养后晶圆边缘一圈失效、RF 报警"。
    mode: semantic（语义检索，默认）或 keyword（关键词匹配）。"""
    reports = db.load_kb()
    try:
        hits = semantic_search(query, reports) if mode == "semantic" else keyword_search(query, reports)
        used = mode
    except Exception as e:  # 向量模型不可用时降级
        hits, used = keyword_search(query, reports), f"keyword（语义检索不可用：{type(e).__name__}，已降级）"
    return {"query": query, "mode": used,
            "matches": [dict(r, similarity=round(s, 3)) for s, r in hits],
            "note": "similarity 越接近 1 越相似；语义检索一般 >0.6 才算相关"}


@mcp.tool(annotations=RO)
def find_impacted_lots(tool: str, since: str) -> dict:
    """影响范围评估：找出某台设备在 since 时间（格式 2026-11-13 22:10，通常填保养时间）之后还加工过的所有批次，
    以及每个批次的测试状态和良率，用于决定哪些货需要扣留复查。"""
    tool = db.norm_tool(tool)
    lots = db.rows("""SELECT p.lot, COUNT(*) wafers_through, MIN(p.time) first_time, l.status, l.test_time
                      FROM process_history p JOIN lots l USING(lot)
                      WHERE p.tool=? AND p.time>=? GROUP BY p.lot ORDER BY first_time""", (tool, since))
    for x in lots:
        y = db.rows("SELECT AVG(yield_pct) a FROM test_yield WHERE lot=?", (x["lot"],))[0]
        x["avg_yield_pct"] = round(y["a"], 1) if y["a"] else None
        if x["status"] == "TESTED":
            # 只看经过这台设备的那几片：其中低良率的比例
            n = db.rows("""SELECT COUNT(*) n FROM test_yield t JOIN process_history p USING(lot, wafer)
                           WHERE t.lot=? AND p.tool=? AND t.yield_pct<85""", (x["lot"], tool))[0]["n"]
            x["low_yield_among_through"] = f"{n}/{x['wafers_through']}"
            x["risk"] = "已确认受影响" if n >= max(2, x["wafers_through"] // 2) else "已测试，良率正常"
        else:
            x["risk"] = "未测试，高风险，建议扣留"
    before = db.rows("SELECT COUNT(DISTINCT lot) n FROM process_history WHERE tool=? AND time<?", (tool, since))[0]["n"]
    return {"tool": tool, "since": since, "impacted_lot_count": len(lots), "lots": lots,
            "lots_before_since": before,
            "suggest_hold": [x["lot"] for x in lots if x["risk"].startswith("未测试")]}


ALLOWED_TABLES = {"lots", "process_history", "test_yield", "equipment_alarms", "tool_status"}
MAX_ROWS = 50


@mcp.tool(annotations=RO)
def query_database(sql: str) -> dict:
    """只读 SQL 查询（SQLite），用于其他工具覆盖不到的问题。只能 SELECT，最多返回 50 行。
    表：lots(lot, product, start_time, test_time, status)；process_history(lot, wafer, step, tool, time)；
    test_yield(lot, wafer, yield_pct, fail_pattern)；equipment_alarms(tool, time, level, code, msg)；tool_status(tool, state)。"""
    s = sql.strip().rstrip(";")
    if ";" in s:
        return {"error": "一次只能执行一条语句"}
    if not re.match(r"(?is)^\s*(select|with)\b", s):
        return {"error": "只允许 SELECT 查询"}
    if re.search(r"(?i)\b(insert|update|delete|drop|alter|create|attach|pragma|replace)\b", s):
        return {"error": "查询中包含不允许的关键字"}
    used = set(re.findall(r"(?i)\b(?:from|join)\s+([a-z_]+)", s))
    if used - ALLOWED_TABLES - {"t", "x", "sub"}:
        return {"error": f"不允许访问的表：{sorted(used - ALLOWED_TABLES)}", "allowed": sorted(ALLOWED_TABLES)}
    try:
        with db.connect(readonly=True) as c:  # 数据库层面只读，三重保险
            cur = c.execute(s)
            cols = [d[0] for d in cur.description]
            data = cur.fetchmany(MAX_ROWS + 1)
    except sqlite3.Error as e:
        return {"error": f"SQL 错误：{e}", "hint": "检查表名和列名是否正确"}
    return {"columns": cols, "rows": [list(r) for r in data[:MAX_ROWS]], "truncated": len(data) > MAX_ROWS}


_secom = None


def _load_secom():
    global _secom
    if _secom is None:
        import numpy as np
        d = db.ROOT / "data" / "secom"
        X = np.genfromtxt(d / "secom.data")
        y = np.array([int(l.split()[0]) for l in (d / "secom_labels.data").read_text().splitlines()])
        _secom = (X, y)
    return _secom


@mcp.tool(annotations=RO)
def secom_sensor_analysis(top_k: int = 5) -> dict:
    """【真实数据】UCI SECOM 半导体产线数据集：1567 次生产记录，590 个传感器读数，标签为合格/不合格。
    对比不合格和合格记录，找出差异最大的传感器（效应量 Cohen's d）。用于演示在真实数据上找异常信号。"""
    import numpy as np
    X, y = _load_secom()
    fail, ok = X[y == 1], X[y == -1]
    res = []
    for i in range(X.shape[1]):
        a, b = fail[:, i], ok[:, i]
        a, b = a[~np.isnan(a)], b[~np.isnan(b)]
        if len(a) < 30 or len(b) < 30:
            continue
        sd = math.sqrt((a.var() + b.var()) / 2)
        if sd < 1e-9:
            continue
        d = (a.mean() - b.mean()) / sd
        res.append({"sensor": f"S{i:03d}", "fail_mean": round(float(a.mean()), 4), "pass_mean": round(float(b.mean()), 4),
                    "effect_size_d": round(float(d), 3), "missing_rate": f"{np.isnan(X[:, i]).mean():.0%}"})
    res.sort(key=lambda r: -abs(r["effect_size_d"]))
    return {"dataset": "UCI SECOM", "records": int(len(y)), "fail": int((y == 1).sum()), "pass": int((y == -1).sum()),
            "sensors_total": int(X.shape[1]), "sensors_usable": len(res), "top_sensors": res[:top_k],
            "note": "|d|≥0.5 为中等差异，≥0.8 为大差异。SECOM 传感器是匿名的，只能定位到编号，需要工艺工程师对照实际含义。"}


# ------------------------------------------------------------------ 有操作的工具
def _write(kind, target, reason, approved_by):
    if not approved_by:
        return {"error": "缺少审批人 approved_by，拒绝执行"}
    ticket = None
    with db.connect(readonly=False) as c:
        if kind == "hold":
            for lot in target:
                c.execute("UPDATE lots SET status='ON_HOLD' WHERE lot=?", (db.norm_lot(lot),))
        elif kind == "down":
            c.execute("UPDATE tool_status SET state='DOWN' WHERE tool=?", (db.norm_tool(target),))
        cur = c.execute("INSERT INTO actions(time, kind, target, reason, approved_by) VALUES(datetime('now','localtime'),?,?,?,?)",
                        (kind, json.dumps(target, ensure_ascii=False), reason, approved_by))
        if kind == "ticket":
            ticket = f"TK-{cur.lastrowid:05d}"
            c.execute("UPDATE actions SET ticket=? WHERE id=?", (ticket, cur.lastrowid))
    return {"ok": True, "kind": kind, "target": target, "approved_by": approved_by, "ticket": ticket}


@mcp.tool(annotations=RW)
def hold_lots(lots: list[str], reason: str, approved_by: str = "") -> dict:
    """【有操作·需审批】扣留批次，暂停流转等待复查。"""
    return _write("hold", lots, reason, approved_by)


@mcp.tool(annotations=RW)
def set_tool_down(tool: str, reason: str, approved_by: str = "") -> dict:
    """【有操作·需审批】把设备状态设为停机（DOWN），不再派货。"""
    return _write("down", tool, reason, approved_by)


@mcp.tool(annotations=RW)
def create_ticket(tool: str, summary: str, approved_by: str = "") -> dict:
    """【有操作·需审批】给设备维修组创建工单。"""
    return _write("ticket", tool, summary, approved_by)


if __name__ == "__main__":
    mcp.run()

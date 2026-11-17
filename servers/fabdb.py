"""数据访问层：工具和网页后端共用。

数据库路径由环境变量 FAB_DB 指定（默认 data/fab.db）。评估时每个用例是一个独立的"工厂"，
各有一个 fab.db，Agent 启动 MCP 服务时传入不同的 FAB_DB 即可。
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("FAB_DB", ROOT / "data" / "fab.db"))
KB_DIR = ROOT / "data" / "kb"
TIME_FMT = "%Y-%m-%d %H:%M"


def connect(readonly: bool = True) -> sqlite3.Connection:
    """只读连接用 SQLite 的 URI 模式 mode=ro：即使 SQL 写了 DELETE 也会被数据库拒绝。"""
    uri = f"file:{DB_PATH}?mode={'ro' if readonly else 'rw'}"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def rows(sql: str, params=()) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute(sql, params)]


def now() -> datetime:
    """模拟数据里的"当前时间"存在 meta 表，保证结果可复现。"""
    r = rows("SELECT value FROM meta WHERE key='now'")
    return datetime.strptime(r[0]["value"], TIME_FMT) if r else datetime.now()


def norm_lot(lot: str) -> str:
    s = lot.strip().upper().replace("_", "-")
    return s if s.startswith("LOT-") else "LOT-" + s.removeprefix("LOT")


def norm_tool(tool: str) -> str:
    return tool.strip().upper().replace(" ", "")


def known_lots() -> list[str]:
    return [r["lot"] for r in rows("SELECT lot FROM lots ORDER BY lot")]


def load_kb() -> list[dict]:
    return json.loads((KB_DIR / "8d_reports.json").read_text())


def timeline(lot: str, tool: str) -> list[dict]:
    """给网页画时间线：保养/报警 → 加工 → 测试。"""
    lot, tool = norm_lot(lot), norm_tool(tool)
    proc = rows("SELECT MIN(time) t0, MAX(time) t1, COUNT(*) n FROM process_history WHERE lot=? AND tool=?",
                (lot, tool))[0]
    if not proc["n"]:
        return []
    t0 = datetime.strptime(proc["t0"], TIME_FMT)
    lo, hi = (t0 - timedelta(days=14)).strftime(TIME_FMT), (t0 + timedelta(days=1)).strftime(TIME_FMT)
    ev = [{"time": a["time"], "kind": "pm" if a["code"] == "PM-DONE" else "alarm",
           "label": a["code"], "detail": a["msg"]}
          for a in rows("SELECT * FROM equipment_alarms WHERE tool=? AND time BETWEEN ? AND ? ORDER BY time",
                        (tool, lo, hi))]
    ev.append({"time": proc["t0"], "kind": "process", "label": f"{lot} 在 {tool} 加工",
               "detail": f"{proc['n']} 片晶圆"})
    lt = rows("SELECT test_time FROM lots WHERE lot=?", (lot,))
    if lt and lt[0]["test_time"]:
        ev.append({"time": lt[0]["test_time"], "kind": "test", "label": "测试发现良率异常", "detail": lot})
    return sorted(ev, key=lambda e: e["time"])

"""生成模拟工厂数据（SQLite）+ 评估用例 + 8D 知识库。

一个"场景"（scenario）= 一座小工厂一周的数据：若干批次、加工履历、测试良率、设备报警。
- 主场景 main：演示用，LOT-A1024 是主角，另有 5 个批次用来演示"影响范围评估"
  （保养后 ETCH-03/B 又加工了哪些批次）。
- 评估场景 eval/case_XX：每个场景埋一个故障（或不埋），带标准答案 truth.json，给 eval 用。

运行：uv run python scripts/gen_data.py
"""
import json
import random
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
FMT = "%Y-%m-%d %H:%M"

STEPS = [
    ("01_LITHO", ["LITHO-01", "LITHO-02"]),
    ("02_ETCH", ["ETCH-01/A", "ETCH-01/B", "ETCH-03/A", "ETCH-03/B"]),
    ("03_DEPO", ["CVD-02/A", "CVD-02/B", "CVD-05/A"]),
    ("04_CMP", ["CMP-01", "CMP-02"]),
    ("05_IMPLANT", ["IMP-01", "IMP-02"]),
]
STEP_OF = {t: s for s, ts in STEPS for t in ts}
PRODUCTS = ["DDR5-16Gb", "LPDDR5-12Gb", "DDR4-8Gb"]

# 每台设备出问题时对应的失效图案和报警（给评估场景随机挑）
FAULTS = {
    "ETCH-01/A": ("Edge-Ring", "RF-PWR-DEV", "RF reflected power deviation {v}% (limit 5%)"),
    "ETCH-01/B": ("Edge-Ring", "RF-PWR-DEV", "RF reflected power deviation {v}% (limit 5%)"),
    "ETCH-03/A": ("Edge-Ring", "ESC-TEMP", "Electrostatic chuck temperature drift {v}C (limit 3C)"),
    "ETCH-03/B": ("Edge-Ring", "RF-PWR-DEV", "RF reflected power deviation {v}% (limit 5%)"),
    "CVD-02/A": ("Center", "GAS-FLOW", "MFC flow deviation {v}% (limit 2%)"),
    "CVD-02/B": ("Center", "GAS-FLOW", "MFC flow deviation {v}% (limit 2%)"),
    "CVD-05/A": ("Center", "HEATER", "Heater zone temperature deviation {v}C (limit 2C)"),
    "CMP-01": ("Scratch", "PAD-LIFE", "Pad life {v}% exceeded, particles detected"),
    "CMP-02": ("Scratch", "SLURRY", "Slurry flow unstable, deviation {v}%"),
    "LITHO-01": ("Donut", "FOCUS", "Focus drift {v}nm (limit 20nm)"),
    "LITHO-02": ("Donut", "FOCUS", "Focus drift {v}nm (limit 20nm)"),
    "IMP-01": ("Center", "BEAM", "Beam current deviation {v}% (limit 3%)"),
    "IMP-02": ("Center", "BEAM", "Beam current deviation {v}% (limit 3%)"),
}
NOISE_ALARMS = [
    ("CVD-02/A", "INFO", "GAS-FLOW", "MFC flow settle time slightly long, within spec"),
    ("CMP-02", "INFO", "PAD-LIFE", "Pad life 82%, schedule replacement"),
    ("LITHO-02", "INFO", "RETICLE", "Reticle clean reminder"),
    ("IMP-01", "INFO", "VACUUM", "Load lock pump-down 3s slower than baseline"),
]


def schema(conn):
    conn.executescript("""
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE lots(lot TEXT PRIMARY KEY, product TEXT, start_time TEXT, test_time TEXT,
                      status TEXT DEFAULT 'IN_PROCESS');  -- IN_PROCESS 在制 / TESTED 已测 / ON_HOLD 被扣留
    CREATE TABLE process_history(lot TEXT, wafer TEXT, step TEXT, tool TEXT, time TEXT);
    CREATE TABLE test_yield(lot TEXT, wafer TEXT, yield_pct REAL, fail_pattern TEXT);
    CREATE TABLE equipment_alarms(tool TEXT, time TEXT, level TEXT, code TEXT, msg TEXT);
    CREATE TABLE tool_status(tool TEXT PRIMARY KEY, state TEXT DEFAULT 'UP');  -- UP 运行 / DOWN 停机
    CREATE TABLE actions(id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, kind TEXT, target TEXT,
                         reason TEXT, approved_by TEXT, ticket TEXT);
    CREATE INDEX ph_lot ON process_history(lot); CREATE INDEX ph_tool ON process_history(tool);
    CREATE INDEX ty_lot ON test_yield(lot); CREATE INDEX al_tool ON equipment_alarms(tool);
    """)


class Fab:
    def __init__(self, path: Path, seed: int, now: datetime):
        self.rng = random.Random(seed)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        self.conn = sqlite3.connect(path)
        schema(self.conn)
        self.now = now
        self.conn.execute("INSERT INTO meta VALUES('now', ?)", (now.strftime(FMT),))
        for t in STEP_OF:
            self.conn.execute("INSERT INTO tool_status(tool) VALUES(?)", (t,))

    def lot(self, lot, start: datetime, n=25, forced=None, yields=None, tested=True, product=None):
        """forced: {wafer: {step: tool}} 强制路由；yields: {wafer: (yield, pattern)} 覆盖良率。"""
        rng = self.rng
        wafers = [f"W{i:02d}" for i in range(1, n + 1)]
        test_time = (start + timedelta(hours=30)).strftime(FMT) if tested else None
        self.conn.execute("INSERT INTO lots VALUES(?,?,?,?,?)",
                          (lot, product or rng.choice(PRODUCTS), start.strftime(FMT), test_time,
                           "TESTED" if tested else "IN_PROCESS"))
        route = {}
        for w in wafers:
            route[w] = {}
            for i, (step, tools) in enumerate(STEPS):
                f = (forced or {}).get(w, {})
                if step in f:
                    tool = f[step]
                else:
                    # 被强制路由占用的设备，其他片避开（让共性更干净）
                    taken = {v.get(step) for v in (forced or {}).values()} - {None}
                    pool = [t for t in tools if t not in taken] or tools
                    tool = rng.choice(pool)
                route[w][step] = tool
                t = start + timedelta(hours=1 + i * 2, minutes=rng.randint(0, 59))
                self.conn.execute("INSERT INTO process_history VALUES(?,?,?,?,?)",
                                  (lot, w, step, tool, t.strftime(FMT)))
            if tested:
                y, p = (yields or {}).get(w, (round(rng.uniform(90, 95), 1), "None"))
                self.conn.execute("INSERT INTO test_yield VALUES(?,?,?,?)", (lot, w, y, p))
        return route

    def alarm(self, tool, t: datetime, level, code, msg):
        self.conn.execute("INSERT INTO equipment_alarms VALUES(?,?,?,?,?)", (tool, t.strftime(FMT), level, code, msg))

    def noise(self, k=3):
        for tool, lvl, code, msg in self.rng.sample(NOISE_ALARMS, k):
            self.alarm(tool, self.now - timedelta(days=self.rng.uniform(0.5, 3)), lvl, code, msg)

    def close(self):
        self.conn.commit()
        self.conn.close()


def bad_yield(rng, pattern):
    return round(rng.uniform(70, 80), 1), pattern


# ---------------- 主场景（演示用） ----------------
def build_main():
    now = datetime(2026, 11, 16, 9, 0)
    fab = Fab(DATA / "fab.db", 42, now)
    rng = fab.rng
    bad = ["W03", "W07", "W08", "W11", "W15", "W18", "W21", "W24"]
    pm = datetime(2026, 11, 13, 22, 10)
    # 主角 LOT-A1024：8 片走 ETCH-03/B → Edge-Ring；W13 划伤（干扰）
    fab.lot("LOT-A1024", datetime(2026, 11, 14, 8, 0), product="DDR5-16Gb",
            forced={w: {"02_ETCH": "ETCH-03/B"} for w in bad},
            yields={**{w: bad_yield(rng, "Edge-Ring") for w in bad}, "W13": (83.5, "Scratch")})
    # 保养前的正常批次（ETCH-03/B 也加工过，但没问题 → 证明是保养后才坏）
    fab.lot("LOT-A0988", datetime(2026, 11, 12, 9, 0), forced={w: {"02_ETCH": "ETCH-03/B"} for w in ["W02", "W05", "W09"]})
    fab.lot("LOT-A0995", datetime(2026, 11, 13, 6, 0))
    # 保养后经过 ETCH-03/B 的其他批次（影响范围）
    fab.lot("LOT-A1031", datetime(2026, 11, 14, 14, 0), forced={w: {"02_ETCH": "ETCH-03/B"} for w in ["W01", "W04", "W06", "W10", "W12", "W19"]},
            yields={w: bad_yield(rng, "Edge-Ring") for w in ["W01", "W04", "W06", "W10", "W12", "W19"]})
    fab.lot("LOT-A1037", datetime(2026, 11, 15, 2, 0), forced={w: {"02_ETCH": "ETCH-03/B"} for w in ["W02", "W08", "W14", "W20", "W22"]},
            tested=False)  # 还在制，没测试 → 应该扣留
    fab.lot("LOT-A1040", datetime(2026, 11, 15, 20, 0), forced={w: {"02_ETCH": "ETCH-03/B"} for w in ["W05", "W06", "W07", "W16"]},
            tested=False)
    fab.lot("LOT-A1029", datetime(2026, 11, 14, 11, 0), forced={f"W{i:02d}": {"02_ETCH": "ETCH-01/A"} for i in range(1, 26)})  # 保养后但没经过 ETCH-03/B → 不受影响
    # 报警
    fab.alarm("ETCH-03/B", datetime(2026, 11, 6, 15, 0), "INFO", "PM-DONE", "Preventive maintenance completed: chamber wet clean")
    fab.alarm("ETCH-03/B", pm, "INFO", "PM-DONE", "Preventive maintenance completed: edge ring replaced, chamber wet clean")
    fab.alarm("ETCH-03/B", datetime(2026, 11, 14, 9, 32), "WARN", "RF-PWR-DEV", "RF reflected power deviation 8.5% (limit 5%), auto-recovered")
    fab.alarm("ETCH-03/B", datetime(2026, 11, 14, 10, 47), "WARN", "RF-PWR-DEV", "RF reflected power deviation 9.1% (limit 5%), auto-recovered")
    fab.alarm("ETCH-03/B", datetime(2026, 11, 15, 4, 15), "WARN", "RF-PWR-DEV", "RF reflected power deviation 8.8% (limit 5%), auto-recovered")
    fab.alarm("CVD-02/A", datetime(2026, 11, 14, 11, 5), "INFO", "GAS-FLOW", "MFC flow settle time slightly long, within spec")
    fab.alarm("CMP-02", datetime(2026, 11, 14, 12, 20), "INFO", "PAD-LIFE", "Pad life 82%, schedule replacement")
    fab.close()
    print("主场景 data/fab.db：7 个批次，主角 LOT-A1024")


# ---------------- 评估场景 ----------------
def build_case(cid: int, kind: str, seed: int):
    """kind:
    clear      明确：单台设备，有保养+报警
    no_alarm   设备有共性但近 3 天日志为空（报警在 7 天前）→ 要扩大时间范围
    scattered  坏片分散，需按失效图案筛选后才能看出共性
    two_issues 两个独立问题同时存在
    unknown    没有共性、没有报警 → 应回答证据不足
    """
    rng = random.Random(seed)
    now = datetime(2026, 11, 16, 9, 0)
    d = DATA / "eval" / f"case_{cid:02d}"
    if d.exists():
        shutil.rmtree(d)
    fab = Fab(d / "fab.db", seed, now)
    lot = f"LOT-{rng.choice('BCDEFGHJK')}{rng.randint(1000, 9999)}"
    start = datetime(2026, 11, 14, 8, 0)
    wafers = [f"W{i:02d}" for i in range(1, 26)]
    tool = rng.choice(list(FAULTS))
    step = STEP_OF[tool]
    pattern, code, msg = FAULTS[tool]
    nbad = rng.randint(6, 10)
    bad = sorted(rng.sample(wafers, nbad))
    truth = {"case": cid, "kind": kind, "lot": lot, "root_tool": tool, "pattern": pattern, "second_tool": None}
    pm_t = start - timedelta(hours=rng.randint(6, 30))

    if kind == "clear":
        fab.lot(lot, start, forced={w: {step: tool} for w in bad}, yields={w: bad_yield(rng, pattern) for w in bad})
        fab.alarm(tool, pm_t, "INFO", "PM-DONE", "Preventive maintenance completed")
        fab.alarm(tool, start + timedelta(hours=2), "WARN", code, msg.format(v=rng.randint(7, 12)))
    elif kind == "no_alarm":
        fab.lot(lot, start, forced={w: {step: tool} for w in bad}, yields={w: bad_yield(rng, pattern) for w in bad})
        fab.alarm(tool, now - timedelta(days=rng.randint(6, 9)), "INFO", "PM-DONE", "Preventive maintenance completed: part replaced")
    elif kind == "scattered":
        # 坏片里混了 3 片其他原因的低良率片（不同图案、不同设备），直接全量分析共性会被稀释
        others = sorted(rng.sample([w for w in wafers if w not in bad], 3))
        fab.lot(lot, start, forced={w: {step: tool} for w in bad},
                yields={**{w: bad_yield(rng, pattern) for w in bad},
                        **{w: (round(rng.uniform(78, 84), 1), rng.choice(["Random", "Local"])) for w in others}})
        fab.alarm(tool, start + timedelta(hours=2), "WARN", code, msg.format(v=rng.randint(7, 12)))
    elif kind == "two_issues":
        tool2 = rng.choice([t for t in FAULTS if STEP_OF[t] != step and FAULTS[t][0] != pattern])
        p2 = FAULTS[tool2][0]
        bad2 = sorted(rng.sample([w for w in wafers if w not in bad], 4))
        fab.lot(lot, start, forced={**{w: {step: tool} for w in bad}, **{w: {STEP_OF[tool2]: tool2} for w in bad2}},
                yields={**{w: bad_yield(rng, pattern) for w in bad}, **{w: bad_yield(rng, p2) for w in bad2}})
        fab.alarm(tool, start + timedelta(hours=2), "WARN", code, msg.format(v=rng.randint(7, 12)))
        fab.alarm(tool2, start + timedelta(hours=3), "WARN", FAULTS[tool2][1], FAULTS[tool2][2].format(v=rng.randint(7, 12)))
        truth["second_tool"] = tool2
    elif kind == "unknown":
        fab.lot(lot, start, yields={w: (round(rng.uniform(78, 86), 1), "Random") for w in bad})
        truth["root_tool"] = None
        truth["pattern"] = "Random"
    fab.noise(rng.randint(2, 4))
    fab.close()
    (d / "truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2))
    return truth


# ---------------- 8D 知识库 ----------------
def build_kb():
    """30 份历史 8D 报告。标题/现象故意用不同说法（中英混用、口语化），
    让关键词检索和语义检索拉开差距。"""
    R = [
        ("刻蚀腔室保养后边缘环状失效", "Edge-Ring 失效图案，良率下降约 15%，集中在单一刻蚀腔室",
         "保养时 edge ring 安装偏心，导致晶圆边缘等离子体分布不均，RF 反射功率异常",
         "暂停腔室、重新校准 edge ring 安装，复测 RF 匹配；在保养 SOP 中增加安装同心度检查", "ETCH"),
        ("晶圆外圈一圈芯片挂掉", "晶圆最外圈 die 大面积失效，内圈正常，同一台干法刻蚀机",
         "聚焦环（focus ring）磨损超寿命，边缘刻蚀速率偏高", "更换聚焦环，寿命管控从 800 小时降到 600 小时", "ETCH"),
        ("静电吸盘温度漂移导致边缘良率损失", "边缘区域良率低，温度报警频繁",
         "ESC 冷却气体泄漏，晶圆边缘温度偏高", "更换 ESC 密封圈，增加 He 漏率监控", "ETCH"),
        ("CMP 划伤导致局部失效", "Scratch 失效图案，单片良率下降",
         "CMP 研磨垫寿命末期，颗粒导致划伤", "更换研磨垫，缩短更换周期", "CMP"),
        ("抛光后出现长条状刮痕", "晶圆表面一条直线状的缺陷带，跨越多个 die",
         "研磨液（slurry）供应管路结晶，大颗粒进入研磨区", "清洗管路，加装过滤器，增加 slurry 粒径监控", "CMP"),
        ("沉积气体流量波动导致中心失效", "Center 失效图案，整批良率小幅下降",
         "MFC 流量控制器漂移", "校准 MFC，增加流量 SPC 监控", "CVD"),
        ("圆片中间一团坏点", "晶圆中心区域集中失效，周边正常",
         "CVD 加热盘中心区温控异常，薄膜厚度不均", "更换加热盘热电偶，增加分区温度监控", "CVD"),
        ("光刻焦距漂移导致环形失效", "Donut 失效图案，中间和外圈正常，中间一圈坏",
         "曝光机焦距随温度漂移", "增加焦距自动校准频率", "LITHO"),
        ("光罩脏污导致重复性缺陷", "每个曝光区域同一位置出现缺陷",
         "光罩表面颗粒", "清洗光罩，增加光罩检查频率", "LITHO"),
        ("离子注入束流不稳导致参数漂移", "电性参数偏移，中心区域良率低",
         "离子源灯丝老化，束流不稳", "更换灯丝，增加束流监控", "IMPLANT"),
        ("注入剂量偏差导致整片失效", "整片晶圆电性不良",
         "剂量计校准过期", "重新校准剂量计，加入每日点检", "IMPLANT"),
        ("刻蚀终点检测失效", "部分晶圆刻蚀过度，随机分布",
         "终点检测光学窗口污染", "清洁光学窗口，缩短清洁周期", "ETCH"),
        ("RF 匹配器故障导致刻蚀不均", "刻蚀速率波动，良率下降，RF 报警",
         "RF 匹配网络电容老化", "更换匹配器电容", "ETCH"),
        ("腔室湿洗后颗粒超标", "保养后首批晶圆颗粒数偏高，随机缺陷",
         "湿洗后腔室未充分 seasoning", "保养后增加 seasoning 晶圆数量", "ETCH"),
        ("晶圆传送机械手划伤", "晶圆背面和边缘划痕",
         "机械手末端执行器磨损", "更换末端执行器，调整传送速度", "CMP"),
        ("真空泵异常导致沉积不良", "薄膜质量异常，颗粒增加",
         "真空泵油污染", "更换泵油，增加真空度监控", "CVD"),
        ("温度控制器漂移导致批次良率下降", "整批良率下降 5%，无明显图案",
         "炉管温度控制器漂移", "校准温度控制器", "CVD"),
        ("清洗工序残留导致缺陷", "随机分布的残留物缺陷",
         "清洗液浓度不足", "调整清洗液更换周期", "CLEAN"),
        ("测试探针卡接触不良", "测试良率异常低，但复测正常",
         "探针卡针尖氧化", "清洁探针卡，非工艺问题", "TEST"),
        ("测试程序版本错误", "某批次测试良率异常，图案随机",
         "测试程序误用旧版本", "加入测试程序版本校验", "TEST"),
    ]
    reports = []
    for i, (title, sym, rc, act, area) in enumerate(R):
        reports.append({"id": f"8D-{2024 + i % 3}-{100 + i * 7:03d}", "title": title, "symptom": sym,
                        "root_cause": rc, "action": act, "area": area})
    reports[0]["id"] = "8D-2025-031"  # 和之前版本保持一致，方便对照
    kb = DATA / "kb"
    kb.mkdir(parents=True, exist_ok=True)
    (kb / "8d_reports.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2))
    print(f"知识库 data/kb/8d_reports.json：{len(reports)} 份 8D 报告")


if __name__ == "__main__":
    for old in ["test_yield.csv", "process_history.csv", "equipment_alarms.csv", "8d_reports.json"]:
        (DATA / old).unlink(missing_ok=True)
    build_main()
    build_kb()
    kinds = ["clear"] * 8 + ["no_alarm"] * 3 + ["scattered"] * 3 + ["two_issues"] * 3 + ["unknown"] * 3
    truths = [build_case(i + 1, k, 1000 + i) for i, k in enumerate(kinds)]
    (DATA / "eval" / "index.json").write_text(json.dumps(truths, ensure_ascii=False, indent=2))
    print(f"评估集 data/eval/：{len(truths)} 个场景 " + str({k: kinds.count(k) for k in dict.fromkeys(kinds)}))

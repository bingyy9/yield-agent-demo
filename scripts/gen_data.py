"""生成合成数据：一个批次 25 片晶圆，埋入已知故障。

埋入的故障（Agent 要找出来的"标准答案"）：
  刻蚀机 ETCH-03 的腔室 B 在 2026-11-14 保养后出现射频功率异常，
  经过它的晶圆出现边缘环状失效（Edge-Ring），良率明显下降。

运行：uv run python scripts/gen_data.py
"""
import csv
import json
import random
from pathlib import Path

random.seed(42)  # 固定随机种子，每次生成的数据一样，方便复现
DATA = Path(__file__).resolve().parent.parent / "data"
DATA.mkdir(exist_ok=True)

LOT = "LOT-A1024"
PRODUCT = "DDR5-16Gb"
WAFERS = [f"W{i:02d}" for i in range(1, 26)]

# 加工步骤：每步有若干台设备/腔室可选。只有 ETCH 这一步藏着故障。
STEPS = [
    ("01_LITHO", ["LITHO-01", "LITHO-02"]),
    ("02_ETCH", ["ETCH-01/A", "ETCH-01/B", "ETCH-03/A", "ETCH-03/B"]),
    ("03_DEPO", ["CVD-02/A", "CVD-02/B", "CVD-05/A"]),
    ("04_CMP", ["CMP-01", "CMP-02"]),
    ("05_IMPLANT", ["IMP-01", "IMP-02"]),
]
BAD_TOOL = "ETCH-03/B"

# 1) 加工履历：先给每片晶圆分配设备
history = []
route = {}
bad_wafers = {"W03", "W07", "W08", "W11", "W15", "W18", "W21", "W24"}  # 走过 ETCH-03/B 的片
for w in WAFERS:
    route[w] = {}
    for step, tools in STEPS:
        if step == "02_ETCH":
            tool = BAD_TOOL if w in bad_wafers else random.choice([t for t in tools if t != BAD_TOOL])
        else:
            tool = random.choice(tools)
        route[w][step] = tool
        history.append({"lot": LOT, "wafer": w, "step": step, "tool": tool,
                        "time": f"2026-11-14 {8 + int(step[:2]):02d}:{random.randint(0, 59):02d}"})

# 2) 测试良率：正常片 90~95%，坏片 70~80%；另加 1 片随机原因的坏片作干扰项
yields = []
noise_wafer = "W13"  # 干扰项：良率偏低但没走 ETCH-03/B，失效图案也不同
for w in WAFERS:
    if w in bad_wafers:
        y, pattern = random.uniform(70, 80), "Edge-Ring"
    elif w == noise_wafer:
        y, pattern = 83.5, "Scratch"
    else:
        y, pattern = random.uniform(90, 95), "None"
    yields.append({"lot": LOT, "wafer": w, "product": PRODUCT, "yield_pct": round(y, 1),
                   "fail_pattern": pattern, "test_time": "2026-11-15 14:00"})

# 3) 设备报警日志：ETCH-03/B 保养后两次射频报警；其他设备只有无关的轻微报警
alarms = [
    {"tool": "ETCH-03/B", "time": "2026-11-13 22:10", "level": "INFO", "code": "PM-DONE",
     "msg": "Preventive maintenance completed: edge ring replaced, chamber wet clean"},
    {"tool": "ETCH-03/B", "time": "2026-11-14 09:32", "level": "WARN", "code": "RF-PWR-DEV",
     "msg": "RF reflected power deviation 8.5% (limit 5%), auto-recovered"},
    {"tool": "ETCH-03/B", "time": "2026-11-14 10:47", "level": "WARN", "code": "RF-PWR-DEV",
     "msg": "RF reflected power deviation 9.1% (limit 5%), auto-recovered"},
    {"tool": "CVD-02/A", "time": "2026-11-14 11:05", "level": "INFO", "code": "GAS-FLOW",
     "msg": "MFC flow settle time slightly long, within spec"},
    {"tool": "CMP-02", "time": "2026-11-14 12:20", "level": "INFO", "code": "PAD-LIFE",
     "msg": "Pad life 82%, schedule replacement"},
]

# 4) 历史 8D 报告（知识库）：只有一份和本次真正相关
reports = [
    {"id": "8D-2025-031", "title": "刻蚀腔室保养后边缘环状失效",
     "symptom": "Edge-Ring 失效图案，良率下降约 15%，集中在单一刻蚀腔室",
     "root_cause": "保养时 edge ring 安装偏心，导致晶圆边缘等离子体分布不均，RF 反射功率异常",
     "action": "暂停腔室、重新校准 edge ring 安装，复测 RF 匹配；在保养 SOP 中增加安装同心度检查",
     "keywords": ["Edge-Ring", "刻蚀", "ETCH", "保养", "PM", "RF", "edge ring"]},
    {"id": "8D-2025-047", "title": "CMP 划伤导致局部失效",
     "symptom": "Scratch 失效图案，单片良率下降",
     "root_cause": "CMP 研磨垫寿命末期，颗粒导致划伤",
     "action": "更换研磨垫，缩短更换周期",
     "keywords": ["Scratch", "CMP", "划伤", "pad"]},
    {"id": "8D-2026-012", "title": "沉积气体流量波动导致中心失效",
     "symptom": "Center 失效图案，整批良率小幅下降",
     "root_cause": "MFC 流量控制器漂移",
     "action": "校准 MFC，增加流量 SPC 监控",
     "keywords": ["Center", "CVD", "沉积", "MFC", "gas"]},
]


def write_csv(name, rows):
    with open(DATA / name, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


write_csv("test_yield.csv", yields)
write_csv("process_history.csv", history)
write_csv("equipment_alarms.csv", alarms)
(DATA / "8d_reports.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2))

avg = sum(r["yield_pct"] for r in yields) / len(yields)
print(f"已生成 {DATA}：{len(yields)} 片晶圆，批次平均良率 {avg:.1f}%")
print(f"埋入故障：{BAD_TOOL}，影响 {sorted(bad_wafers)}；干扰项 {noise_wafer}")

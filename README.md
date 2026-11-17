# 良率异常排查 Agent（Yield Excursion Agent）V2

一句话提问，Agent 自己查良率、找坏片共同经过的机台、查设备报警、语义检索历史 8D 报告、评估还有哪些批次受影响，最后提交带证据链的结构化报告；扣货、停机、开单只能由工程师审批后执行。

- 大模型：本地 Ollama（默认 `qwen2.5:7b`；向量模型 `bge-m3`），**完全离线，数据不出本机**
- 工具协议：MCP（7 个只读工具给模型用，3 个操作工具只走人工审批）
- 数据：SQLite 模拟工厂（埋入已知故障）+ 20 个评估场景 + 公开真实数据集 SECOM
- 可靠性：报告代码校验、证据不足如实说、工具调用恢复、兜底报告、完整轨迹可回放

文档：
- [优化说明.md](优化说明.md)：17 项优化逐条说明（做了什么、怎么做、验证结果）
- [面试文档.md](面试文档.md)：电梯演讲、演示脚本、30+ 个面试问答
- [eval/report.md](eval/report.md)：20 场景量化评估结果；[eval/rag_report.md](eval/rag_report.md)：检索对比
- [数据说明.md](数据说明.md)、[开发步骤.md](开发步骤.md)、[调用流程.html](调用流程.html)

## 运行

```bash
uv sync                                   # 安装依赖
ollama pull qwen2.5:7b && ollama pull bge-m3
uv run python scripts/gen_data.py         # 生成主场景数据库 + 20 个评估场景

# 命令行（-i 进入追问模式）
uv run python agent/main.py -i "LOT-A1024 的良率从 92% 掉到 86%，帮我查一下原因。"

# 网页（推荐演示用）
uv run python web/server.py               # 打开 http://127.0.0.1:8000

# 评估 / 检索对比 / 主动巡检 / 多 Agent 对照
uv run python eval/run_eval.py --models qwen2.5:7b,qwen2.5:3b
uv run python eval/rag_compare.py
uv run python agent/monitor.py
uv run python agent/multi_agent.py LOT-A1024

# Docker（模型跑在宿主机 Ollama）
docker compose up --build
```

换模型：`YIELD_AGENT_MODEL=qwen2.5:3b uv run python agent/main.py "..."`

## 结构

```
scripts/gen_data.py     生成 SQLite 模拟工厂 + 20 个评估场景 + 8D 知识库
servers/fabdb.py        数据访问层（只读连接），工具和网页共用
servers/fab_tools.py    MCP 服务：7 个只读工具 + 3 个操作工具
agent/main.py           Agent：Session 循环、护栏、报告校验、轨迹、审批执行
agent/monitor.py        主动巡检
agent/multi_agent.py    多 Agent 分工对照实验
eval/run_eval.py        20 场景评估 + 多模型对比 → eval/report.md
eval/rag_compare.py     关键词 vs 语义检索 → eval/rag_report.md
web/                    网页：实时步骤、晶圆图、时间线、审批、导出 Word、追问、语音、轨迹回放
traces/                 每次排查的 JSONL 轨迹
```

## 工具

| 工具 | 模拟的工厂系统 | 作用 |
|---|---|---|
| `get_lot_yield` | 测试系统 / YMS | 每片良率和失效图案，按图案分组 |
| `commonality_analysis` | MES 加工履历 | 坏片 vs 好片经过的设备，给出证据强度 |
| `get_equipment_alarms` | 设备报警 / FDC | 报警和保养记录 |
| `search_8d_reports` | 质量知识库 | bge-m3 语义检索历史 8D 报告 |
| `find_impacted_lots` | MES | 影响范围：异常后还加工过哪些批次，哪些要扣 |
| `query_database` | 数据仓库 | 兜底只读 SQL（白名单 + 只读连接） |
| `secom_sensor_analysis` | FDC 传感器 | 公开数据集 SECOM 上找异常传感器 |
| `hold_lots` / `set_tool_down` / `create_ticket` | MES / EAP / 工单 | **模型不可见**，仅审批后执行 |

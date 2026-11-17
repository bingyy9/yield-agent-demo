# 良率异常排查 Agent（Yield Excursion Agent）V1

一句话提问，Agent 自己查良率、做共性分析、查设备报警、检索历史 8D 报告，最后给出带证据链的根因判断。

- 大模型：本地 Ollama（默认 `qwen2.5:7b`），**完全离线，数据不出本机**
- 工具协议：MCP（每个工厂系统包装成一个工具，换成真实接口时 Agent 不用改）
- 数据：合成数据，埋入已知故障（刻蚀机 ETCH-03 腔室 B 保养后导致边缘环状失效）

## 运行

```bash
# 1. 安装依赖（首次）
uv sync

# 2. 下载本地模型（首次，约 4.7GB）
ollama pull qwen2.5:7b

# 3. 生成合成数据
uv run python scripts/gen_data.py

# 4. 运行 Agent
uv run python agent/main.py "LOT-A1024 的良率从 92% 掉到 86%，帮我查一下原因。"
```

换模型：`YIELD_AGENT_MODEL=qwen3:8b uv run python agent/main.py "..."`

## 结构

```
data/                 合成数据（良率、加工履历、设备报警、8D 报告）
scripts/gen_data.py   生成合成数据
servers/fab_tools.py  MCP 服务：4 个工具
agent/main.py         Agent 循环：模型 ↔ MCP 工具
开发步骤.md           开发过程记录和学习笔记
```

## 4 个工具

| 工具 | 模拟的工厂系统 | 作用 |
|---|---|---|
| `get_lot_yield` | 测试系统 / YMS | 查每片晶圆良率和失效图案，标出低良率片 |
| `commonality_analysis` | MES 加工履历 | 对比坏片和好片经过的设备，找共性 |
| `get_equipment_alarms` | 设备报警 / FDC | 查设备的报警和保养记录 |
| `search_8d_reports` | 质量知识库 | 按关键词检索历史 8D 报告 |

## 网页界面

```bash
uv run python web/server.py   # 然后浏览器打开 http://127.0.0.1:8000
```

左侧是提问框和 25 片晶圆的晶圆图，右侧实时显示 Agent 每一步调用的工具和结果，最后生成结构化报告。Agent 圈定的坏片会在晶圆图上标红，独立问题标黄。

# cc-watermark-lab

CC_Codex 二期（26.11）**数据工程**产品线：**血缘水印台**（BlackSheep + Alpine CDN + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**产出表水印模板 → 批次盖印 → 下游追索 → 缺口对账**

与现有七线正交（不是采集目录、ETL 管道、同步源、解析入库、编排流、种子摄取、留存裁撤）。
不碰外仓常见指纹（Feed 归并 / CDC 游标 / 质检签发 / Slicegate 类仓名）。

## 领域

- **所属领域**：数据工程
- **子方向**：数据血缘与水印（产出表盖印 / 追索闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、BlackSheep、Uvicorn、SQLite |
| 前端 | 原生 JS SPA（全中文，底栏），雾灰底 + 青柠强调；不依赖外网 CDN |
| 测试 | pytest + httpx（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 产出表水印模板（表名 / 列名 / 现行版本）
2. 向运行批次盖印水印值
3. 按水印值追索下游批次
4. 缺口对账（已盖印 vs 应盖印）

## 齐备门禁（本轮）

**齐备判定与写印裁定同源**：同一套 `assess_batch` 同时驱动靶表附页色块、
写印卡片拒因芯片、齐备席命运标记、血缘边目录空边标记；点写印时后端再裁一次，
未齐备当场 409 并落拒因流水。

**空边印命运（已钉死）**：源表未齐备却已落印的批次，**留存入库并转「候审只读」，
拒绝继续写印**；空边印不会被静默回滚，也不被追索/缺口当作有效盖印。

- 库级闸：`trg_stamp_requires_ready` / `trg_stamp_no_double`，绕过服务层也拦。
- 并发：写印用 `BEGIN IMMEDIATE` 先锁后判，双人抢写只成一笔。
- 对账口径：`gap = expected − 有效盖印`；候审与受阻单列，缺口不虚低。
- 二验步骤见 [`docs/verify-round2.md`](docs/verify-round2.md)。

预留切口（未做，供后续功能题）：模板换版生效窗、盖印作废回灌、跨表依赖冻结、追索深度限制等。

## 本地（推荐反馈环）

```powershell
cd claude-workspace\cc-watermark-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:7000/login · **admin / 123456**

## Docker

```powershell
docker compose up -d --build
```

端口 **7000**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`

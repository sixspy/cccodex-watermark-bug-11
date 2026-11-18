# 二验步骤（另纸）

本纸只讲**怎么验**，不讲实现。所有结论都必须能从页面、API、库三处交叉复核。
验收口径：**齐备与可写印同源**；源表未齐备却已落印的批次，命运钉死为
**留存候审只读 + 拒绝继续写印**（空边印仍留在库，不会被静默回滚）。

> 约定：下列 `BID` 为批次 id，`RUN` 为运行码。默认种子库：
> `ord-20261114-01`（无上游，可写）、`pay-20261114-01`（无上游，可写）、
> `gmv-20261114-01`（上游 = ord / pay / ord-20261113，未齐备则受阻）、
> `ord-20261113-01`（已盖印）。

---

## 一、页面拒因（人眼可核）

1. 打开 `/app/`，切到 **靶表附页**。
   - `dm_gmv_d` 附页应为**警示色**，`gmv-20261114-01` 行命运显示 **等源齐备**，
     行尾按钮是 **看拒因**（不是“盖印”）。
   - 点 **看拒因**，应出现拒因卡，列出
     `SOURCE_NOT_STAMPED · 上游批次 … 尚未盖印`。
2. 切到 **齐备席**。
   - 顶部应显示命运条：**留存候审**。
   - 「批次命运」表中 `gmv-20261114-01` 的 **源齐备** 列显示 `未齐 0/2`（或 1/3，
     视是否已盖 `ord-20261113-01`），**命运** 列显示 **等源齐备**，
     **拒因** 列显示 `SOURCE_NOT_READY`。
   - 「血缘边目录」中指向 `gmv-20261114-01` 的上游里，未盖印的边应标 **空边**。
3. **四处同看**：靶表附页色块、写印卡片命运芯片、齐备席命运列、血缘边目录空边，
   对同一批次必须给出一致结论。若任一处文案/色块改动而另一处不跟，验收失败。

## 二、点写印被当场拦（后端一次裁定）

1. 用 API 或页面点 `gmv-20261114-01` 的写印（水印值随意，如 `WM-GMV-TRY`）。
2. 期望：HTTP **409**，响应体 `ok=false`、`reason_code=SOURCE_NOT_READY`、
   `error` 为中文白话。
3. 立刻查库：该批次 `status` 仍是 **待盖印**，`watermark_value` 仍为 **NULL**。
   —— 未齐备绝不落库。

```bash
# 直接查库（DATABASE_PATH 指到你自己的库）
sqlite3 "$DATABASE_PATH" \
  "SELECT run_code,status,watermark_value FROM run_batches WHERE run_code='gmv-20261114-01';"
```

## 三、拒因入库可核（编排任务对账）

1. `GET /api/adjudications`（或页面 **裁定流水**）。
2. 每次点写印都应有一行：`verdict`（allowed / rejected）、`reason_code`、
   `reason_text`、`operator`、`readiness_snapshot`。
3. 上面那次被拦的写印，应能找到一行
   `rejected / SOURCE_NOT_READY / admin`。
4. 编排任务落地时，以该表为准对账：**没有允许流水的批次不应出现已盖印状态**，
   反之亦然。

## 四、放行路径（齐备后可写）

1. 先给 `ord-20261114-01` 与 `pay-20261114-01` 写印（各自 200）。
2. 再点 `gmv-20261114-01` 写印 → **200**，状态 `已盖印`，命运 **已盖印**。
3. `GET /api/reconcile?as_of_date=2026-11-16`：`gmv-20261114-01` 的 `bucket`
   应为 **有效盖印**。
4. 重复点同一批次 → **409 / ALREADY_STAMPED**。

## 五、下游追复验（两轮，不许再吃空边印）

**第一轮 · 空边来源**

1. 先造一个“历史遗留空边印”：让某批源未齐备却已盖印
   （可用旧库迁移或直接置库；见第六节命令）。
2. 对该水印值 `POST /api/trace`。
3. 期望：`used_empty_edges=true`、`valid_source_count=0`、`downstream=[]`；
   源批次里该条带 `valid=false / empty_edge=true`。页面 **追索** 页应显示
   “仅命中空边印，未展开下游”，下游列表为“无有效下游”。

**第二轮 · 处置后再追**

1. 在 **齐备席** 点 **将遗留空边印收进候审只读**（或 `POST /api/readiness/sweep`）。
2. 再对该水印值 `POST /api/trace`：仍应为 `used_empty_edges=true`、下游为空，
   且该批次状态已变 **候审只读**。
3. 复核库：`status='候审只读'` 且 `watermark_value` **仍在**（留存，不回滚）。

> 两轮都不得把下游“拉活”。若任一出现了下游批次，即吃到了空边印，验收失败。

## 六、缺口口径（虚高/虚低都不许）

1. `POST /api/gap-reports {"as_of_date":"2026-11-16"}`。
2. 口径固定：`expected = 未作废批次`；`stamped = 已盖印且上游边全部有效`；
   `review = 候审只读 / 空边印`；`blocked = 因未齐备受阻的在途批次`；
   `gap = expected - stamped`。
3. 断言：`gap == expected - stamped`，且 `review_count` 覆盖了空边印，
   空边印**既不被算作有效盖印，也不被静默抹掉**。

## 七、双人同时抢写印（缺锁会双写）

1. 选一个 **待盖印** 且 **可写** 的批次（如刚重置库后的 `ord-20261114-01`）。
2. 两个终端同时打同一批次：

```bash
for wm in WM-RACE-A WM-RACE-B; do
  curl -s -X POST "http://127.0.0.1:7600/api/batches/BID/stamp" \
    -H 'Content-Type: application/json' -b "$COOKIE" \
    -d "{\"watermark_value\":\"$wm\"}" &
done; wait
```

3. 期望：**恰好一个 200**，另一个 **409 / ALREADY_STAMPED**；库中该批次只有
   **一个** watermark_value，等于那笔 200 的值。
4. 裁定流水里应同时出现一行 `STAMP_ALLOWED` 与一行 `ALREADY_STAMPED`。

## 八、库级闸门（绕过服务层也不许落印）

```bash
# 未齐备强行落印 → 必须被触发器 ABORT
sqlite3 "$DATABASE_PATH" \
  "UPDATE run_batches SET status='已盖印', watermark_value='WM-FORCE' \
   WHERE run_code='gmv-20261114-01';"
# 期望：Error: ... ERR_SOURCE_NOT_READY，且该行未改变。

# 已盖印再改印 → 必须被 ABORT
sqlite3 "$DATABASE_PATH" \
  "UPDATE run_batches SET watermark_value='WM-SECOND' \
   WHERE run_code='ord-20261113-01';"
# 期望：Error: ... ERR_DOUBLE_STAMP。
```

## 九、空边印处置命运（二选一已钉死）

- 本系统选定：**留存候审只读 + 拒绝继续写印**。
- 因此 `gmv-20261114-01` 若曾空边落印，收口后仍以 `候审只读` 留在库中，
  **不会**被回滚成“从未盖印”。若验收期望“先整笔回滚再等齐备重写”，
  则本系统与该期望不符——这是刻意选择的另一条命运，需产品侧确认后另行改判。

---

## 一键自检

```bash
pytest -q                       # 15 项，含并发、库闸、迁移
```

关键断言对应文件：`tests/test_readiness.py`。

"""齐备门禁与空边印验收。

覆盖题面要求：
- 齐备判定与写印裁定同源，未齐仍落库当场拦（API + 库级闸）
- 空边印命运：留存候审只读 + 拒继续写印；不会静默回滚
- 双人同时抢写印：加锁后只成一笔
- 页面拒因可由 API 复核；编排任务经 API 入库可核
- 下游追复验两轮不再吃空边印
"""

import os
import sqlite3
import tempfile
import threading

import pytest

from app.db import get_db, init_db
from app.services import readiness_board, reconcile, run_gap_report, trace_downstream
from app.services.ready import QUARANTINE_STATUS


def _batch_by_code(db, code):
    with get_db(db) as conn:
        row = conn.execute(
            "SELECT * FROM run_batches WHERE run_code=?", (code,)
        ).fetchone()
        return dict(row) if row else None


@pytest.mark.asyncio
async def test_unready_source_blocks_stamp_with_reason(authed):
    """pay 未盖印 → gmv 未齐备，点写印被拒，库中 gmv 仍待盖印。"""
    board = await (await authed.get("/api/readiness")).json()
    gmv = next(
        b for b in board["assessments"] if b["run_code"] == "gmv-20261114-01"
    )
    assert gmv["readiness"]["ready"] is False
    assert gmv["writable"] is False
    assert gmv["reason_code"] == "SOURCE_NOT_READY"

    r = await authed.post(
        f"/api/batches/{gmv['batch_id']}/stamp",
        json={"watermark_value": "WM-GMV-BAD"},
    )
    assert r.status == 409
    body = await r.json()
    assert body["ok"] is False
    assert body["reason_code"] == "SOURCE_NOT_READY"
    assert body["error"]

    with get_db() as conn:
        row = conn.execute(
            "SELECT status, watermark_value FROM run_batches WHERE run_code='gmv-20261114-01'"
        ).fetchone()
    assert row["status"] == "待盖印"
    assert row["watermark_value"] is None


@pytest.mark.asyncio
async def test_stamp_card_reason_matches_backend(authed):
    """写印卡片读取的裁定，与点写印的拒因同源同码。"""
    detail = await (
        await authed.get("/api/batches/3")
    ).json()
    card_reason = detail["assessment"]["reason_code"]

    r = await authed.post(
        "/api/batches/3/stamp", json={"watermark_value": "WM-GMV-CARD"}
    )
    body = await r.json()
    assert body["reason_code"] == card_reason


@pytest.mark.asyncio
async def test_ready_after_sources_stamped_then_target_ok(authed):
    all_b = (await (await authed.get("/api/batches")).json())["items"]
    ord_id = next(b["id"] for b in all_b if b["run_code"] == "ord-20261114-01")
    pay_id = next(b["id"] for b in all_b if b["run_code"] == "pay-20261114-01")
    gmv_id = next(b["id"] for b in all_b if b["run_code"] == "gmv-20261114-01")

    for bid, wm in ((ord_id, "WM-ORD-A"), (pay_id, "WM-PAY-A")):
        r = await authed.post(f"/api/batches/{bid}/stamp", json={"watermark_value": wm})
        assert r.status == 200, await r.text()

    r = await authed.post(
        f"/api/batches/{gmv_id}/stamp", json={"watermark_value": "WM-GMV-OK"}
    )
    assert r.status == 200
    body = await r.json()
    assert body["batch"]["status"] == "已盖印"
    assert body["assessment"]["fate"]["code"] == "STAMPED_VALID"


@pytest.mark.asyncio
async def test_empty_edge_stamp_quarantined_and_retained(authed):
    """模拟历史遗留空边印：源未齐备却已盖印 → 收进候审只读，印仍留库。"""
    # 直接用库写入遗留态（绕过闸门：临时禁用触发器）
    db = os.environ["DATABASE_PATH"]
    with get_db(db) as conn:
        conn.execute("DROP TRIGGER IF EXISTS trg_stamp_requires_ready")
        conn.execute(
            "UPDATE run_batches SET status='已盖印', watermark_value='WM-GMV-LEGACY' "
            "WHERE run_code='gmv-20261114-01'"
        )

    board = readiness_board(db)
    gmv = next(b for b in board["assessments"] if b["run_code"] == "gmv-20261114-01")
    assert gmv["status"] == "已盖印"
    assert gmv["fate"]["code"] == "QUARANTINE_PENDING"

    # 齐备席命运动作：留存候审只读
    r = await authed.post("/api/readiness/sweep")
    assert r.status == 200
    moved = (await r.json())["moved"]
    assert any(m["run_code"] == "gmv-20261114-01" for m in moved)

    row = _batch_by_code(db, "gmv-20261114-01")
    assert row["status"] == QUARANTINE_STATUS
    assert row["watermark_value"] == "WM-GMV-LEGACY"  # 印仍在库

    # 候审批次拒绝继续写印
    with get_db(db) as conn:
        gid = conn.execute(
            "SELECT id FROM run_batches WHERE run_code='gmv-20261114-01'"
        ).fetchone()["id"]
    r = await authed.post(
        f"/api/batches/{gid}/stamp", json={"watermark_value": "WM-GMV-AGAIN"}
    )
    assert r.status == 409
    assert (await r.json())["reason_code"] == "BATCH_QUARANTINED"


@pytest.mark.asyncio
async def test_trace_does_not_use_empty_edge(authed):
    """下游追复验：空边印不再作为有效上游把下游拉进来。"""
    db = os.environ["DATABASE_PATH"]
    with get_db(db) as conn:
        conn.execute("DROP TRIGGER IF EXISTS trg_stamp_requires_ready")
        conn.execute(
            "UPDATE run_batches SET status='已盖印', watermark_value='WM-GMV-EMPTY' "
            "WHERE run_code='gmv-20261114-01'"
        )
    init_db(db)  # 重建触发闸
    # 注：init_db 后 gmv 仍是空边印；启动时未走 sweep，这里手动收候审
    from app.services import sweep_quarantine

    sweep_quarantine(db)

    result = trace_downstream("WM-GMV-EMPTY", db)
    assert result["used_empty_edges"] is True
    assert result["downstream"] == []
    assert result["valid_source_count"] == 0


@pytest.mark.asyncio
async def test_gap_report_not_fooled_by_empty_edge(authed):
    db = os.environ["DATABASE_PATH"]
    with get_db(db) as conn:
        conn.execute("DROP TRIGGER IF EXISTS trg_stamp_requires_ready")
        conn.execute(
            "UPDATE run_batches SET status='已盖印', watermark_value='WM-GMV-GAP' "
            "WHERE run_code='gmv-20261114-01'"
        )
    init_db(db)
    from app.services import sweep_quarantine
    sweep_quarantine(db)

    report = run_gap_report("2026-11-16", db)
    # gmv 转候审：不算有效盖印，计入 review；缺口不被压低。
    assert report["review_count"] >= 1
    assert report["gap_count"] == (
        report["expected_count"] - report["stamped_count"]
    )
    items = reconcile(as_of_date="2026-11-16", db_path=db)["items"]
    gmv = next(i for i in items if i["run_code"] == "gmv-20261114-01")
    assert gmv["bucket"] == "候审只读"


@pytest.mark.asyncio
async def test_concurrent_service_stamp_only_one_wins(authed):
    """双人值班同时调用写印服务：只成一笔，另一笔留拒因。"""
    from app.services import stamp_batch

    db = os.environ["DATABASE_PATH"]
    with get_db(db) as conn:
        ord_id = conn.execute(
            "SELECT id FROM run_batches WHERE run_code='ord-20261114-01'"
        ).fetchone()["id"]

    outcomes = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker(wm, op):
        barrier.wait()
        try:
            row = stamp_batch(ord_id, wm, db, operator=op)
            with lock:
                outcomes.append((op, "ok", row["watermark_value"]))
        except Exception as exc:  # noqa: BLE001
            with lock:
                outcomes.append((op, "rejected", getattr(exc, "reason_code", str(exc))))

    threads = [
        threading.Thread(target=worker, args=("WM-RACE-A", "alice")),
        threading.Thread(target=worker, args=("WM-RACE-B", "bob")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    wins = [o for o in outcomes if o[1] == "ok"]
    losses = [o for o in outcomes if o[1] == "rejected"]
    assert len(wins) == 1
    assert len(losses) == 1
    assert losses[0][2] == "ALREADY_STAMPED"

    row = _batch_by_code(db, "ord-20261114-01")
    assert row["watermark_value"] == wins[0][2]

    with get_db(db) as conn:
        reasons = [
            r["reason_code"]
            for r in conn.execute(
                "SELECT reason_code FROM stamp_adjudications WHERE run_code='ord-20261114-01'"
            ).fetchall()
        ]
    assert "STAMP_ALLOWED" in reasons
    assert "ALREADY_STAMPED" in reasons


@pytest.mark.asyncio
async def test_concurrent_stamp_only_one_wins(authed):
    """双人值班抢写：加锁后只成一笔，另一笔拒且无覆盖。"""
    all_b = (await (await authed.get("/api/batches")).json())["items"]
    ord_id = next(b["id"] for b in all_b if b["run_code"] == "ord-20261114-01")

    results = []
    lock = threading.Lock()

    def worker(wm):
        try:
            with get_db() as conn:
                conn.isolation_level = None
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT status FROM run_batches WHERE id=?", (ord_id,)
                ).fetchone()
                if row["status"] == "已盖印":
                    raise ValueError("ALREADY_STAMPED")
                conn.execute(
                    "UPDATE run_batches SET status='已盖印', watermark_value=? "
                    "WHERE id=? AND status='待盖印'",
                    (wm, ord_id),
                )
                # 确保另一线程真的尝试过
                import time

                time.sleep(0.05)
                conn.execute("COMMIT")
            with lock:
                results.append((wm, "ok"))
        except Exception as exc:  # noqa: BLE001
            with lock:
                results.append((wm, type(exc).__name__))

    threads = [
        threading.Thread(target=worker, args=(f"WM-RACE-{i}",)) for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    wins = [r for r in results if r[1] == "ok"]
    assert len(wins) == 1
    row = _batch_by_code(None, "ord-20261114-01")
    assert row["watermark_value"] == wins[0][0]


def test_db_trigger_blocks_unready_stamp():
    """绕过服务层直接写库，闸门仍需拦住未齐备落印。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    try:
        init_db(path)
        with get_db(path) as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE run_batches SET status='已盖印', watermark_value='WM-X' "
                    "WHERE run_code='gmv-20261114-01'"
                )
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except FileNotFoundError:
                pass


def test_db_trigger_blocks_double_stamp():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    try:
        init_db(path)
        with get_db(path) as conn:
            conn.execute(
                "UPDATE run_batches SET status='已盖印', watermark_value='WM-ORD-131' "
                "WHERE run_code='ord-20261114-01'"
            )
        with get_db(path) as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE run_batches SET watermark_value='WM-OTHER' "
                    "WHERE run_code='ord-20261114-01'"
                )
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except FileNotFoundError:
                pass


def test_readiness_board_surfaces_agree():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    try:
        init_db(path)
        board = readiness_board(path)
        # 血缘边目录里的空边、齐备席的阻塞、靶表附页的 blocked 计数必须一致
        empty_edges = board["counts"]["empty_edges"]
        blocked = board["counts"]["blocked"]
        per_table_blocked = sum(t["blocked_count"] for t in board["target_tables"])
        assert empty_edges > 0
        assert blocked == per_table_blocked
        # 每条空边都应能在对应批次的 readiness.blockers 里找到
        for edge in board["edges"]:
            item = next(
                a for a in board["assessments"] if a["run_code"] == edge["downstream_run_code"]
            )
            if edge["empty_edge"]:
                assert not item["readiness"]["ready"]
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except FileNotFoundError:
                pass

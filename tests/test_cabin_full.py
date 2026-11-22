"""满舱闸（甲路·审计账册）红用例。

红线对应：
1. 无满舱落点仍塞满舱 -> 拒
2. 仅有满舱芯片无落点行 -> 不算对齐
3. 拒回刷、脚手架旁路（gate_bypass / 双轨）残留 -> 不过
4. 指纹不合还放行 -> 拒（白话报错）
5. 两侧同抢同一满舱闸 -> 只成一笔，钉点不互相盖
6. 主链绿；满舱舱拒超额入舱；对账含满舱口径
7. 舱位/抓取/对账/设置四处同口径，乙路双轨不得并存
"""
from __future__ import annotations

import sqlite3

import pytest

from app.db import get_db
from app.services import cabin_full, captures, policies, quarantine, reconcile
from app.services.errors import ServiceError


def _open_bin(db_path: str) -> dict:
    return next(b for b in quarantine.list_bins(db_path) if b["status"] == "开放")


def _fill_bin_to_full(db_path: str, bin_id: int) -> list[int]:
    """直接把舱塞到容量上限，返回占用行 id 列表（绕过服务层，造满舱事实）。"""
    with get_db(db_path) as conn:
        cap = conn.execute(
            "SELECT capacity FROM quarantine_bins WHERE id=?", (bin_id,)
        ).fetchone()["capacity"]
        have = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine_rows WHERE bin_id=? AND status IN ('待复核','回炉中')",
            (bin_id,),
        ).fetchone()["c"]
        batch_id = conn.execute(
            "INSERT INTO capture_batches(batch_code, source_name, status, note) "
            "VALUES ('CAP-FILL-0001','压舱源','采集中','压满舱')"
        ).lastrowid
        row_ids: list[int] = []
        for i in range(cap - have):
            item_id = conn.execute(
                "INSERT INTO capture_items(batch_id, item_key, quality, status) "
                "VALUES (?, ?, '缺字段', '待检')",
                (batch_id, f"fill-{bin_id}-{i}"),
            ).lastrowid
            row_id = conn.execute(
                "INSERT INTO quarantine_rows(bin_id, item_id, reason, status) "
                "VALUES (?, ?, '压舱', '待复核')",
                (bin_id, item_id),
            ).lastrowid
            row_ids.append(row_id)
        return row_ids


def _new_pending_bad_item(db_path: str) -> int:
    with get_db(db_path) as conn:
        batch_id = conn.execute(
            "INSERT INTO capture_batches(batch_code, source_name, status, note) "
            "VALUES ('CAP-OVER-0001','超额源','采集中','超额件')"
        ).lastrowid
        return conn.execute(
            "INSERT INTO capture_items(batch_id, item_key, quality, status) "
            "VALUES (?, 'over-1', '缺字段', '待检')",
            (batch_id,),
        ).lastrowid


# 红线 1：无满舱落点仍塞满舱，不得过关
def test_full_bin_without_gate_rejects_extra_item(db_path):
    bin_row = _open_bin(db_path)
    _fill_bin_to_full(db_path, bin_row["id"])
    item_id = _new_pending_bad_item(db_path)

    assert cabin_full.gate_for_bin(bin_row["bin_code"], db_path) is None
    with pytest.raises(ServiceError) as exc:
        quarantine.enter_item(item_id, bin_row["id"], "超额试探", db_path)
    assert "满舱" in exc.value.message
    # 舱内行数没有被冲垮
    with get_db(db_path) as conn:
        occupied = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine_rows WHERE bin_id=? AND status IN ('待复核','回炉中')",
            (bin_row["id"],),
        ).fetchone()["c"]
        assert occupied == bin_row["capacity"]


# 红线 2：满舱落闸必须写真实落点行；只有芯片没有行不算对齐
def test_commit_gate_writes_durable_landing_row(db_path):
    bin_row = _open_bin(db_path)
    _fill_bin_to_full(db_path, bin_row["id"])

    gate = cabin_full.commit_gate(bin_row["bin_code"], db_path)
    assert gate["bin_code"] == bin_row["bin_code"]
    assert gate["water_level"] == bin_row["capacity"]
    assert gate["capacity"] == bin_row["capacity"]
    assert gate["fingerprint"] and gate["fingerprint"] != "stale-fp"
    assert gate["revision"] == 1
    assert gate["committed_at"]

    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM cabin_full_gates WHERE bin_code=?",
            (bin_row["bin_code"],),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["active"] == 1

    snap = cabin_full.snapshot(db_path)
    assert snap["gates_total"] == 1
    assert snap["aligned_bins"] == 1
    # 皮面亮但没有落点行的状态必须被判未对齐
    assert cabin_full.is_aligned(db_path) is True


def test_chip_without_landing_row_is_not_aligned(db_path):
    bin_row = _open_bin(db_path)
    _fill_bin_to_full(db_path, bin_row["id"])
    # 满舱事实已存在，但没有任何落点行
    with get_db(db_path) as conn:
        gate_count = conn.execute("SELECT COUNT(*) AS c FROM cabin_full_gates").fetchone()["c"]
    assert gate_count == 0
    listed = next(b for b in quarantine.list_bins(db_path) if b["id"] == bin_row["id"])
    assert listed["is_full"] is True
    assert listed["gate_active"] is False
    assert listed["full_chip"] != "满舱已锁"
    assert cabin_full.is_aligned(db_path) is False


# 红线 3：落点只读、拒绝回刷；脚手架旁路不得残留
def test_gate_is_read_only_and_rejects_refresh(db_path):
    bin_row = _open_bin(db_path)
    _fill_bin_to_full(db_path, bin_row["id"])
    first = cabin_full.commit_gate(bin_row["bin_code"], db_path)

    with pytest.raises(ServiceError) as exc:
        cabin_full.commit_gate(bin_row["bin_code"], db_path)
    assert "已落" in exc.value.message or "回刷" in exc.value.message

    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM cabin_full_gates WHERE bin_code=? AND active=1",
            (bin_row["bin_code"],),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["revision"] == 1
        assert rows[0]["fingerprint"] == first["fingerprint"]
        # 旧脚手架的旁路钥匙一律不许存在
        leftover = conn.execute(
            "SELECT key FROM meta WHERE key LIKE 'cabin_full_%'"
        ).fetchall()
        assert not leftover


def test_no_scaffold_bypass_keys():
    import app.services.cabin_full as mod

    src = open(mod.__file__, encoding="utf-8").read()
    assert "gate_bypass" not in src
    assert "old_track" not in src
    assert "stale-fp" not in src
    assert "paint_ok" not in src


# 红线 4：指纹漂移还放行不得过关（白话报错）
def test_fingerprint_drift_rejects_entry(db_path):
    bin_row = _open_bin(db_path)
    row_ids = _fill_bin_to_full(db_path, bin_row["id"])
    gate = cabin_full.commit_gate(bin_row["bin_code"], db_path)

    # 人为篡改舱内钉点（绕过闸直接废一行），制造芯片与水位偏移
    with get_db(db_path) as conn:
        conn.execute(
            "UPDATE quarantine_rows SET status='已废弃' WHERE id=?",
            (row_ids[0],),
        )

    drifted = cabin_full.gate_for_bin(bin_row["bin_code"], db_path)
    assert drifted["fingerprint_locked"] == gate["fingerprint"]
    assert drifted["fingerprint_now"] != gate["fingerprint"]
    assert drifted["drifted"] is True

    item_id = _new_pending_bad_item(db_path)
    with pytest.raises(ServiceError) as exc:
        quarantine.enter_item(item_id, bin_row["id"], "漂移试探", db_path)
    # 白话：必须点明是水位/指纹偏了
    assert "指纹" in exc.value.message or "水位" in exc.value.message


# 红线 5：两侧同抢同一满舱闸，缺串行会互相盖钉点
def test_concurrent_commit_only_one_wins(db_path):
    bin_row = _open_bin(db_path)
    _fill_bin_to_full(db_path, bin_row["id"])

    left = sqlite3.connect(db_path)
    right = sqlite3.connect(db_path)
    try:
        left.execute("BEGIN IMMEDIATE")
        # 右侧在左侧持锁期间落闸：必须失败而不是静默覆盖
        with pytest.raises(ServiceError) as exc:
            cabin_full.commit_gate(bin_row["bin_code"], db_path, conn=right)
        assert "忙" in exc.value.message or "冲突" in exc.value.message or "抢" in exc.value.message

        left.execute(
            "INSERT INTO cabin_full_gates(bin_code, occupied, capacity, water_level, fingerprint, revision, active) "
            "VALUES (?, ?, ?, ?, ?, 1, 1)",
            (
                bin_row["bin_code"],
                bin_row["capacity"],
                bin_row["capacity"],
                bin_row["capacity"],
                cabin_full.compute_fingerprint(bin_row["bin_code"], db_path),
            ),
        )
        left.commit()
    finally:
        left.close()
        right.close()

    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM cabin_full_gates WHERE bin_code=? AND active=1",
            (bin_row["bin_code"],),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["revision"] == 1


# 红线 6：主链保持绿；满舱舱拒超额；对账挂满舱口径
def test_main_chain_green_and_full_bin_blocked(db_path):
    # 未满舱舱：完整主链不受闸影响
    batches = captures.list_batches(db_path)
    batch = next(b for b in batches if b["status"] != "已关闭")
    open_bin = _open_bin(db_path)
    detail = captures.get_batch(batch["id"], db_path)
    bad = next(i for i in detail["items"] if i["quality"] != "合格" and i["status"] == "待检")
    row = quarantine.enter_item(bad["id"], open_bin["id"], "主链入舱", db_path)
    assert row["status"] == "待复核"

    # 另起一个舱压满并落闸
    other = next(b for b in quarantine.list_bins(db_path) if b["status"] == "开放" and b["id"] != open_bin["id"])
    _fill_bin_to_full(db_path, other["id"])
    cabin_full.commit_gate(other["bin_code"], db_path)
    over = _new_pending_bad_item(db_path)
    with pytest.raises(ServiceError):
        quarantine.enter_item(over, other["id"], "超额", db_path)

    report = reconcile.run_reconcile(db_path)
    assert "满舱" in report["summary"]
    dash = reconcile.dashboard(db_path)
    assert "cabin_full" in dash
    assert dash["cabin_full"]["gates_total"] == 1


# 红线 7：四处同口径；乙路双轨/切流旧轨不得并存
def test_four_surfaces_share_one_full_bin_caliber(db_path):
    bin_row = _open_bin(db_path)
    _fill_bin_to_full(db_path, bin_row["id"])
    cabin_full.commit_gate(bin_row["bin_code"], db_path)

    # 舱位页：满舱芯片 + 水位值 + 落点
    listed = next(b for b in quarantine.list_bins(db_path) if b["id"] == bin_row["id"])
    assert listed["full_chip"] == "满舱已锁"
    assert listed["water_level"] == listed["capacity"]
    assert listed["gate_active"] is True
    assert listed["fingerprint"]

    # 抓取页口径：列表能读到同一份水位值
    snap = cabin_full.snapshot(db_path)
    assert any(g["bin_code"] == bin_row["bin_code"] for g in snap["gates"])

    # 对账页：满舱落点摘要
    dash = reconcile.dashboard(db_path)
    assert dash["cabin_full"]["gates_total"] >= 1
    assert dash["cabin_full"]["drifted"] == 0

    # 设置页：只展甲路满舱口径
    settings_view = cabin_full.settings_view(db_path)
    assert settings_view["track"] == "cutover_book"
    assert settings_view["gates_total"] == 1
    assert "archive" not in settings_view and "live_flag" not in settings_view


def test_dual_track_modules_removed():
    import os

    svc_dir = os.path.dirname(cabin_full.__file__)
    names = set(os.listdir(svc_dir))
    assert "cabin_full_dualwrite.py" not in names
    assert "cabin_full_cutover.py" not in names
    assert "cabin_full_audit.py" not in names


def test_policy_auto_enter_respects_full_gate(db_path):
    bin_row = _open_bin(db_path)
    _fill_bin_to_full(db_path, bin_row["id"])
    cabin_full.commit_gate(bin_row["bin_code"], db_path)

    batch = captures.create_batch("规则压舱源", "满舱规则批", db_path)
    captures.add_item(batch["id"], "pol-over-1", "{}", "缺字段", db_path)
    # 规则默认入 BIN-A；若该舱已满，套规则不得把超额件塞进去
    with pytest.raises(ServiceError):
        policies.apply_to_batch(batch["id"], db_path)

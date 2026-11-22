"""满舱闸（甲路·审计账册）。

工程形态：满舱当刻把舱码与水位指纹一次性钉进 cabin_full_gates 落点行；
抓取/入舱链路只读账册，拒绝回刷、拒绝指纹漂移。全库仅此一读轨，
不与双写归档轨并存。
"""
from __future__ import annotations

import hashlib
import sqlite3

from app.db import get_db
from app.services.errors import ServiceError

TRACK = "cutover_book"
CHIP_LOCKED = "满舱已锁"
CHIP_OPEN = "未满舱"
CHIP_DRIFT = "满舱芯片与水位偏移"
_ACTIVE = 1


def _occupied(conn: sqlite3.Connection, bin_code: str) -> int:
    return conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM quarantine_rows r
        JOIN quarantine_bins b ON b.id = r.bin_id
        WHERE b.bin_code=? AND r.status IN ('待复核','回炉中')
        """,
        (bin_code,),
    ).fetchone()["c"]


def _bin_row(conn: sqlite3.Connection, bin_code: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM quarantine_bins WHERE bin_code=?", (bin_code,)
    ).fetchone()
    if not row:
        raise ServiceError("隔离舱不存在", 404)
    return row


def _row_id_set(conn: sqlite3.Connection, bin_id: int) -> list[int]:
    rows = conn.execute(
        """
        SELECT r.id FROM quarantine_rows r
        WHERE r.bin_id=? AND r.status IN ('待复核','回炉中')
        ORDER BY r.id
        """,
        (bin_id,),
    ).fetchall()
    return [r["id"] for r in rows]


def compute_fingerprint(bin_code: str, db_path: str | None = None) -> str:
    """水位指纹：舱码 + 当刻占用/容量 + 占用行集合，缺一不可。"""
    with get_db(db_path) as conn:
        bin_row = _bin_row(conn, bin_code)
        occupied = _occupied(conn, bin_code)
        ids = _row_id_set(conn, bin_row["id"])
    digest = hashlib.sha256()
    digest.update(bin_code.encode("utf-8"))
    digest.update(f"|{occupied}/{bin_row['capacity']}|".encode("utf-8"))
    digest.update(",".join(str(i) for i in ids).encode("utf-8"))
    return digest.hexdigest()


def _gate_row(conn: sqlite3.Connection, bin_code: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM cabin_full_gates WHERE bin_code=? AND active=?",
        (bin_code, _ACTIVE),
    ).fetchone()


def gate_for_bin(bin_code: str, db_path: str | None = None) -> dict | None:
    """只读账册：返回落点视图；芯片与水位偏移时显式标 drifted。"""
    with get_db(db_path) as conn:
        gate = _gate_row(conn, bin_code)
        if not gate:
            return None
        bin_row = _bin_row(conn, bin_code)
        now_occupied = _occupied(conn, bin_code)
        ids = _row_id_set(conn, bin_row["id"])
    digest = hashlib.sha256()
    digest.update(bin_code.encode("utf-8"))
    digest.update(f"|{now_occupied}/{bin_row['capacity']}|".encode("utf-8"))
    digest.update(",".join(str(i) for i in ids).encode("utf-8"))
    fp_now = digest.hexdigest()
    return {
        "bin_code": bin_code,
        "occupied": gate["occupied"],
        "capacity": gate["capacity"],
        "water_level": gate["water_level"],
        "fingerprint": gate["fingerprint"],
        "fingerprint_locked": gate["fingerprint"],
        "fingerprint_now": fp_now,
        "revision": gate["revision"],
        "committed_at": gate["committed_at"],
        "drifted": fp_now != gate["fingerprint"],
        "full_chip": CHIP_LOCKED if fp_now == gate["fingerprint"] else CHIP_DRIFT,
    }


def commit_gate(
    bin_code: str,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """一次提交落定：锁当刻舱码与水位指纹；已落则拒回刷；同抢串行。

    传入外部 conn 时由调用方管理事务（用于同抢测试/复用连接）；
    否则自管一条连接并在出错时回滚。
    """
    if conn is not None:
        return _commit_on_conn(conn, bin_code)
    with get_db(db_path) as this_conn:
        return _commit_on_conn(this_conn, bin_code)


def _commit_on_conn(this_conn: sqlite3.Connection, bin_code: str) -> dict:
    try:
        this_conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        raise ServiceError("满舱闸正忙，两侧争抢，请稍后重试", 409) from exc
    bin_row = _bin_row(this_conn, bin_code)
    existing = _gate_row(this_conn, bin_code)
    if existing:
        raise ServiceError(f"{bin_code} 满舱落点已落定，拒绝回刷", 409)
    occupied = _occupied(this_conn, bin_code)
    if occupied < bin_row["capacity"]:
        raise ServiceError(
            f"{bin_code} 水位 {occupied}/{bin_row['capacity']}，未满舱不能落满舱闸",
            409,
        )
    fingerprint = _fingerprint_on(
        this_conn, bin_code, bin_row["id"], occupied, bin_row["capacity"]
    )
    try:
        cur = this_conn.execute(
            """
            INSERT INTO cabin_full_gates(
              bin_code, occupied, capacity, water_level, fingerprint, revision, active
            ) VALUES (?, ?, ?, ?, ?, 1, 1)
            """,
            (bin_code, occupied, bin_row["capacity"], occupied, fingerprint),
        )
    except sqlite3.IntegrityError as exc:
        raise ServiceError(f"{bin_code} 满舱落点已被对侧抢先落定", 409) from exc
    except sqlite3.OperationalError as exc:
        raise ServiceError("满舱闸正忙，两侧争抢，请稍后重试", 409) from exc
    row = this_conn.execute(
        "SELECT * FROM cabin_full_gates WHERE id=?", (cur.lastrowid,)
    ).fetchone()
    return _gate_to_dict(row)


def _gate_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "bin_code": row["bin_code"],
        "occupied": row["occupied"],
        "capacity": row["capacity"],
        "water_level": row["water_level"],
        "fingerprint": row["fingerprint"],
        "revision": row["revision"],
        "active": row["active"],
        "committed_at": row["committed_at"],
    }


def list_gates(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM cabin_full_gates WHERE active=1 ORDER BY id"
        ).fetchall()
    return [_gate_to_dict(r) for r in rows]


def _fingerprint_on(conn: sqlite3.Connection, bin_code: str, bin_id: int, occupied: int, capacity: int) -> str:
    ids = _row_id_set(conn, bin_id)
    digest = hashlib.sha256()
    digest.update(bin_code.encode("utf-8"))
    digest.update(f"|{occupied}/{capacity}|".encode("utf-8"))
    digest.update(",".join(str(i) for i in ids).encode("utf-8"))
    return digest.hexdigest()


def assert_entry_allowed_conn(conn: sqlite3.Connection, bin_row: sqlite3.Row) -> None:
    """入舱闸（同连接版）：满舱无落点拒；落点指纹漂移拒；未满舱放行。"""
    bin_code = bin_row["bin_code"]
    occupied = _occupied(conn, bin_code)
    gate = _gate_row(conn, bin_code)
    if occupied >= bin_row["capacity"] and gate is None:
        raise ServiceError(
            f"{bin_code} 已满舱（{occupied}/{bin_row['capacity']}）且无满舱落点，禁止再塞件",
            409,
        )
    if gate is not None:
        fp_now = _fingerprint_on(
            conn, bin_code, bin_row["id"], occupied, bin_row["capacity"]
        )
        if fp_now != gate["fingerprint"]:
            raise ServiceError(
                f"{bin_code} 满舱芯片与水位指纹偏移，禁止清洗入库",
                409,
            )
        if occupied >= bin_row["capacity"]:
            raise ServiceError(
                f"{bin_code} 满舱落点已锁定，禁止超额入舱",
                409,
            )


def is_aligned(db_path: str | None = None) -> bool:
    """所有满舱舱都必须有活动落点且指纹未漂移，才算皮面与里子对齐。"""
    with get_db(db_path) as conn:
        bins = conn.execute("SELECT * FROM quarantine_bins").fetchall()
        for bin_row in bins:
            occupied = _occupied(conn, bin_row["bin_code"])
            gate = _gate_row(conn, bin_row["bin_code"])
            if occupied >= bin_row["capacity"]:
                if gate is None:
                    return False
                ids = _row_id_set(conn, bin_row["id"])
                digest = hashlib.sha256()
                digest.update(bin_row["bin_code"].encode("utf-8"))
                digest.update(f"|{occupied}/{bin_row['capacity']}|".encode("utf-8"))
                digest.update(",".join(str(i) for i in ids).encode("utf-8"))
                if digest.hexdigest() != gate["fingerprint"]:
                    return False
    return True


def snapshot(db_path: str | None = None) -> dict:
    gates = list_gates(db_path)
    drifted = 0
    with get_db(db_path) as conn:
        full_bins = 0
        for bin_row in conn.execute("SELECT * FROM quarantine_bins").fetchall():
            if _occupied(conn, bin_row["bin_code"]) >= bin_row["capacity"]:
                full_bins += 1
    for g in gates:
        view = gate_for_bin(g["bin_code"], db_path)
        if view and view["drifted"]:
            drifted += 1
    return {
        "title": "满舱闸",
        "track": TRACK,
        "ok_chip": CHIP_LOCKED,
        "drift_chip": CHIP_DRIFT,
        "full_bin_count": full_bins,
        "gates_total": len(gates),
        "aligned_bins": len(gates) - drifted,
        "drifted": drifted,
        "aligned": is_aligned(db_path),
        "gates": gates,
    }


def settings_view(db_path: str | None = None) -> dict:
    """设置页只展甲路满舱口径，不出现归档层/新水位旗等双轨概念。"""
    snap = snapshot(db_path)
    return {
        "title": "满舱闸口径",
        "track": TRACK,
        "track_label": "审计账册（锁舱码与水位指纹，只读拒回刷）",
        "gates_total": snap["gates_total"],
        "full_bin_count": snap["full_bin_count"],
        "drifted": snap["drifted"],
        "aligned": snap["aligned"],
        "gates": snap["gates"],
    }

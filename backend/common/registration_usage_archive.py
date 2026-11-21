"""登记评估的实际用量归档。

正式试跑前，记录费用与失败尝试的机制就应就绪——这里用模拟响应即可验证。

每次尝试（**含重试与失败**）都记录：
- 任务标识（work_id / prefix_id / arm / stage）、请求哈希；
- 模型与采样配置、输入/输出 token；
- 成败、失败原因、尝试序号；
- 用量状态（measured / unknown / not_sent）。

费用在**给定价格表**时核算；模型未定时只记录用量，不预报价。
支持跨进程事务、名额预留与用量补算（解决并发争抢与未知消耗泄漏）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA = "registration_usage_archive_v1"

#: 阶段名
STAGE_GENERATION = "generation"
STAGE_REFERENCE = "reference_discovery"
STAGE_BLIND_REVIEW = "blind_review"
STAGE_ARBITRATION = "arbitration"


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


class FileLock:
    """跨平台文件锁（Windows 使用 msvcrt，POSIX 使用 fcntl）。

    支持重试、超时与排他性，确保跨进程事务互斥。
    """

    def __init__(self, lock_path: Path, timeout: float = 30.0, retry_interval: float = 0.05) -> None:
        self.lock_path = Path(lock_path)
        self.timeout = float(timeout)
        self.retry_interval = float(retry_interval)
        self._fd: int | None = None

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        start = time.time()
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT)
                    try:
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        self._fd = fd
                        return
                    except (BlockingIOError, OSError, PermissionError):
                        os.close(fd)
                else:
                    import fcntl

                    fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT)
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        self._fd = fd
                        return
                    except (BlockingIOError, OSError):
                        os.close(fd)
            except Exception:
                pass
            if time.time() - start >= self.timeout:
                raise TimeoutError(f"超时未能获取跨进程文件锁：{self.lock_path}（等待 {self.timeout}s）")
            time.sleep(self.retry_interval)

    def release(self) -> None:
        if self._fd is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    try:
                        os.lseek(self._fd, 0, os.SEEK_SET)
                        msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                    except Exception:
                        pass
                else:
                    import fcntl

                    try:
                        fcntl.flock(self._fd, fcntl.LOCK_UN)
                    except Exception:
                        pass
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()


class UsageArchive:
    """用量与费用归档。"""

    def __init__(self, *, price_table: Mapping[str, Mapping[str, float]] | None = None) -> None:
        #: model → {"input_per_1k": float, "output_per_1k": float}
        self.price_table = dict(price_table or {})
        self.records: list[dict] = []
        self.reservations: list[dict] = []

    @classmethod
    def load(cls, path: Path | str) -> UsageArchive:
        """从文件加载归档台账。

        严格校验：损坏或非合法 JSON 抛出 ValueError，
        绝不静默重置为空台账继续发送。
        """
        p = Path(path)
        if not p.is_file():
            return cls()
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"台账文件损坏或非合法JSON，拒绝继续请求：{p}") from exc
        if not isinstance(doc, dict):
            raise ValueError(f"台账格式错误（非字典对象），拒绝继续请求：{p}")
        if doc.get("schema") != SCHEMA:
            raise ValueError(f"台账 schema 不匹配（{doc.get('schema')}），拒绝继续请求：{p}")
        records = doc.get("records")
        if not isinstance(records, list):
            raise ValueError(f"台账 records 字段缺失或非列表，拒绝继续请求：{p}")
        raw_res = doc.get("reservations") or []
        if not isinstance(raw_res, list):
            raise ValueError(f"台账 reservations 字段非列表，拒绝继续请求：{p}")

        inst = cls(price_table=doc.get("price_table"))
        inst.records = list(records)
        inst.reservations = list(raw_res)
        return inst

    @classmethod
    @contextmanager
    def transaction(
        cls,
        path: Path | str,
        *,
        timeout: float = 30.0,
        auto_save: bool = True,
    ) -> Generator[UsageArchive, None, None]:
        """跨进程原子事务：重新读取最新台账 -> 检查/操作 -> 持久化。

        加锁并在退出上下文时自动持久化；发生未捕获异常则不自动覆盖持久化。
        网络请求等待必须置于本事务外部。
        """
        p = Path(path)
        lock_path = p.with_suffix(p.suffix + ".lock")
        lock = FileLock(lock_path, timeout=timeout)
        with lock:
            archive = cls.load(p)
            try:
                yield archive
            except Exception:
                raise
            else:
                if auto_save:
                    archive.save(p)

    def reserve(
        self,
        *,
        work_id: str,
        prefix_id: str,
        arm: str,
        stage: str,
        projected_input_tokens: int,
        projected_output_tokens: int,
        max_attempts: int = 42,
        max_input_tokens: int = 336000,
        max_output_tokens: int = 84000,
        per_request_input_limit: int = 20000,
        per_request_output_limit: int = 2000,
    ) -> dict:
        """发送前预检并预留名额。带入累计用量（含已知与未知占用）及所有在途预留。"""
        p_in = int(projected_input_tokens)
        p_out = int(projected_output_tokens)
        if p_in > per_request_input_limit:
            return {
                "ok": False,
                "reason": "per_request_input_limit_exceeded",
                "input_tokens": p_in,
                "limit": per_request_input_limit,
            }
        if p_out > per_request_output_limit:
            return {
                "ok": False,
                "reason": "per_request_output_limit_exceeded",
                "output_tokens": p_out,
                "limit": per_request_output_limit,
            }

        cur_attempts = len(self.records) + len(self.reservations)

        def _rec_in(r: dict) -> int:
            if r.get("usage_status") == "unknown":
                return int(r.get("occupied_input_tokens") or 0)
            val = r.get("input_tokens")
            return int(val if val is not None else (r.get("occupied_input_tokens") or 0))

        def _rec_out(r: dict) -> int:
            if r.get("usage_status") == "unknown":
                return int(r.get("occupied_output_tokens") or 0)
            val = r.get("output_tokens")
            return int(val if val is not None else (r.get("occupied_output_tokens") or 0))

        cur_input = sum(_rec_in(r) for r in self.records) + sum(
            int(res.get("projected_input_tokens") or 0) for res in self.reservations
        )
        cur_output = sum(_rec_out(r) for r in self.records) + sum(
            int(res.get("projected_output_tokens") or 0) for res in self.reservations
        )

        if cur_attempts + 1 > max_attempts:
            return {
                "ok": False,
                "reason": "max_attempts_reached",
                "current_attempts": cur_attempts,
                "limit": max_attempts,
            }
        if cur_input + p_in > max_input_tokens:
            return {
                "ok": False,
                "reason": "max_input_tokens_reached",
                "projected_input": cur_input + p_in,
                "limit": max_input_tokens,
            }
        if cur_output + p_out > max_output_tokens:
            return {
                "ok": False,
                "reason": "max_output_tokens_reached",
                "projected_output": cur_output + p_out,
                "limit": max_output_tokens,
            }

        res_id = f"resv_{cur_attempts + 1}_{_sha(f'{work_id}_{prefix_id}_{arm}_{stage}_{p_in}_{time.time_ns()}')[:8]}"
        resv = {
            "reservation_id": res_id,
            "work_id": work_id,
            "prefix_id": prefix_id,
            "arm": arm,
            "stage": stage,
            "projected_input_tokens": p_in,
            "projected_output_tokens": p_out,
            "created_at": time.time(),
        }
        self.reservations.append(resv)
        return {"ok": True, "reservation_id": res_id}

    def cancel_reservation(self, reservation_id: str) -> bool:
        """取消在途预留（若请求未实际发出且不记入尝试）。"""
        for i, resv in enumerate(self.reservations):
            if resv.get("reservation_id") == reservation_id:
                self.reservations.pop(i)
                return True
        return False

    def settle_reservation(
        self,
        reservation_id: str,
        *,
        request_sha256: str,
        model: str,
        usage_status: str = "measured",
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        occupied_input_tokens: int | None = None,
        occupied_output_tokens: int | None = None,
        success: bool = True,
        error: str | None = None,
        sampling: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> dict:
        """结算在途预留（支持 measured / unknown / not_sent，幂等）。

        找不到预留且未在 records 中时直接抛出 ValueError，绝不悄悄新增未知记录。
        """
        target = None
        for i, resv in enumerate(self.reservations):
            if resv.get("reservation_id") == reservation_id:
                target = self.reservations.pop(i)
                break

        if target is None:
            # 检查是否已结算过（幂等）
            for r in self.records:
                if r.get("reservation_id") == reservation_id:
                    return r
            raise ValueError(f"reservation_not_found: {reservation_id}")

        if usage_status == "measured":
            in_tok = int(input_tokens if input_tokens is not None else 0)
            out_tok = int(output_tokens if output_tokens is not None else 0)
            occ_in = in_tok
            occ_out = out_tok
            cost = self._cost(model, in_tok, out_tok)
        elif usage_status == "unknown":
            in_tok = None
            out_tok = None
            occ_in = int(
                occupied_input_tokens
                if occupied_input_tokens is not None
                else target.get("projected_input_tokens") or 0
            )
            occ_out = int(
                occupied_output_tokens
                if occupied_output_tokens is not None
                else target.get("projected_output_tokens") or 0
            )
            cost = {"status": "用量未知，未核算"}
        elif usage_status == "not_sent":
            in_tok = 0
            out_tok = 0
            occ_in = 0
            occ_out = 0
            cost = {"status": "未发送，零消耗"}
        else:
            raise ValueError(f"unknown usage_status: {usage_status}")

        rec = {
            "schema": SCHEMA,
            "reservation_id": reservation_id,
            "work_id": target["work_id"],
            "prefix_id": target["prefix_id"],
            "arm": target["arm"],
            "stage": target["stage"],
            "request_sha256": request_sha256,
            "model": model,
            "sampling": dict(sampling or {}),
            "usage_status": usage_status,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "occupied_input_tokens": occ_in,
            "occupied_output_tokens": occ_out,
            "success": bool(success),
            "error": error,
            "attempt": len(self.records) + 1,
            "cost": cost,
        }
        if extra:
            rec["extra"] = dict(extra)
        self.records.append(rec)
        return rec

    def reconcile_usage(
        self,
        reservation_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
    ) -> dict:
        """为用量未知的记录补齐真实用量（幂等）。"""
        target = None
        for r in self.records:
            if r.get("reservation_id") == reservation_id:
                target = r
                break
        if target is None:
            raise ValueError(f"record_not_found: {reservation_id}")

        if target.get("usage_status") != "unknown":
            # 已经核算过真实用量，保持幂等
            return target

        in_tok = int(input_tokens)
        out_tok = int(output_tokens)
        target["usage_status"] = "measured"
        target["reconciled_from_unknown"] = True
        target["input_tokens"] = in_tok
        target["output_tokens"] = out_tok
        target["occupied_input_tokens"] = in_tok
        target["occupied_output_tokens"] = out_tok
        target["cost"] = self._cost(target.get("model", ""), in_tok, out_tok)
        return target

    def record_attempt(
        self,
        *,
        work_id: str,
        prefix_id: str,
        arm: str,
        stage: str,
        request_sha256: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        success: bool,
        error: str | None = None,
        attempt: int = 1,
        sampling: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> dict:
        """直接记一次尝试（用于向后兼容单进程模式）。"""
        in_tok = int(input_tokens)
        out_tok = int(output_tokens)
        rec = {
            "schema": SCHEMA,
            "work_id": work_id,
            "prefix_id": prefix_id,
            "arm": arm,
            "stage": stage,
            "request_sha256": request_sha256,
            "model": model,
            "sampling": dict(sampling or {}),
            "usage_status": "measured",
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "occupied_input_tokens": in_tok,
            "occupied_output_tokens": out_tok,
            "success": bool(success),
            "error": error,
            "attempt": int(attempt),
            "cost": self._cost(model, in_tok, out_tok),
        }
        if extra:
            rec["extra"] = dict(extra)
        self.records.append(rec)
        return rec

    def _cost(self, model: str, input_tokens: int, output_tokens: int) -> dict:
        price = self.price_table.get(model)
        if not price:
            return {"status": "无价格表，未核算"}
        cost = (
            input_tokens / 1000.0 * float(price.get("input_per_1k", 0.0))
            + output_tokens / 1000.0 * float(price.get("output_per_1k", 0.0))
        )
        return {
            "status": "已核算",
            "amount": round(cost, 6),
            "currency": "CNY",
            "price_source": "price_table",
        }

    def summary(self) -> dict:
        by_stage: dict = {}
        by_arm: dict = {}
        total_in_measured = 0
        total_out_measured = 0
        total_in_occupied = 0
        total_out_occupied = 0
        failures = 0
        unknown_usage_count = 0
        total_cost = 0.0
        costed = 0
        for r in self.records:
            is_unknown = r.get("usage_status") == "unknown"
            if is_unknown:
                unknown_usage_count += 1
                in_tok = int(r.get("occupied_input_tokens") or 0)
                out_tok = int(r.get("occupied_output_tokens") or 0)
            else:
                in_val = r.get("input_tokens")
                out_val = r.get("output_tokens")
                in_tok = int(in_val if in_val is not None else (r.get("occupied_input_tokens") or 0))
                out_tok = int(out_val if out_val is not None else (r.get("occupied_output_tokens") or 0))
                if in_val is not None:
                    total_in_measured += in_tok
                if out_val is not None:
                    total_out_measured += out_tok

            total_in_occupied += in_tok
            total_out_occupied += out_tok

            if not r.get("success"):
                failures += 1
            c = r.get("cost") or {}
            if c.get("status") == "已核算":
                total_cost += float(c.get("amount") or 0.0)
                costed += 1

            stg = r.get("stage") or "unknown"
            s = by_stage.setdefault(
                stg, {"attempts": 0, "failures": 0, "input_tokens": 0, "output_tokens": 0}
            )
            s["attempts"] += 1
            s["failures"] += 0 if r.get("success") else 1
            s["input_tokens"] += in_tok
            s["output_tokens"] += out_tok

            arm_key = r.get("arm") or "-"
            a = by_arm.setdefault(
                arm_key, {"attempts": 0, "failures": 0, "input_tokens": 0, "output_tokens": 0}
            )
            a["attempts"] += 1
            a["failures"] += 0 if r.get("success") else 1
            a["input_tokens"] += in_tok
            a["output_tokens"] += out_tok

        resv_in = sum(int(res.get("projected_input_tokens") or 0) for res in self.reservations)
        resv_out = sum(int(res.get("projected_output_tokens") or 0) for res in self.reservations)

        return {
            "attempts": len(self.records),
            "reservations_in_flight": len(self.reservations),
            "unknown_usage_count": unknown_usage_count,
            "failures": failures,
            "input_tokens": total_in_occupied,
            "output_tokens": total_out_occupied,
            "input_tokens_measured": total_in_measured,
            "output_tokens_measured": total_out_measured,
            "input_tokens_occupied_total": total_in_occupied + resv_in,
            "output_tokens_occupied_total": total_out_occupied + resv_out,
            "by_stage": by_stage,
            "by_arm": by_arm,
            "cost": {
                "status": "已核算" if costed else "无价格表，未核算",
                "amount": round(total_cost, 6) if costed else None,
                "currency": "CNY",
                "costed_attempts": costed,
            },
            "note": "失败尝试与重试同样计入 attempts 与 token 用量；用量未知项按占用值计入预算",
        }

    def save(self, path: Path | str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": SCHEMA,
            "price_table": self.price_table,
            "records": self.records,
            "reservations": self.reservations,
            "summary": self.summary(),
        }
        tmp = p.with_suffix(f"{p.suffix}.tmp.{os.getpid()}.{time.time_ns()}")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)

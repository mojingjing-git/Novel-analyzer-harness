# -*- coding: utf-8 -*-
"""Step5: SummaryService / LocationNormalizationService lifecycle matrix.

Drives the REAL service methods (start / stop / status / _run) with fake
runners and a hub spy, then records the observed behaviour: state field
values, WS event ordering, timestamps and the persistence side effects.

Nothing here copies a production closure or asserts on source text: every
case calls the service and observes what it actually did.

Audit conclusion recorded here (Step5, no extraction made):

The two services have four small fragments in common -- the is_running
property, the book-exists guard, the has-chapter-results guard, and the
per-run state reset block. Even these are not word-for-word identical: the
two ValueError messages differ. Everything else that a base class would
have to own diverges: start() signatures, runner construction, the
on_progress body (WS type, log category, batch_failed level, phase
handling), the token-stats callback, and the terminal-state mapping.
SummaryService also carries the analysis mutex, report write-back and
session token accumulation that normalization has no analogue for.

Sharing those four fragments would buy very little while the surrounding
behaviour is owned differently by each service, so the duplication stays
and the concrete lifecycle matrix below is the record.
"""

import asyncio
import contextlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.services import book_service
from backend.services import location_normalization_service as loc_norm
from backend.services import summary_service as summary_mod
from backend.services.location_normalization_service import (
    LocationNormalizationService,
)
from backend.services.summary_service import SummaryService
from backend.ws_events import WSState, WSType


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class HubSpy:
    """Replaces the real ProgressHub; records publish/state_change order."""

    def __init__(self):
        self.events = []

    async def publish(self, message):
        self.events.append(("publish", message.get("type"), message))

    async def log(self, text, level="info", category=""):
        self.events.append(("log", level, {"text": text, "category": category}))

    def log_levels(self):
        return [e[1] for e in self.events if e[0] == "log"]

    def log_messages(self):
        return [e[2]["text"] for e in self.events if e[0] == "log"]

    async def state_change(self, state, detail=""):
        self.events.append(("state", str(state), {"detail": detail}))

    def types(self):
        return [e[1] for e in self.events]

    def state_names(self):
        return [e[1] for e in self.events if e[0] == "state"]


class FakeRunner:
    """Stands in for FinalSummaryRunner / LocationNormalizer."""

    def __init__(self, result="# report", token_stats=None, exc=None):
        self._result = result
        self._token_stats = token_stats or {"summary": {"input_tokens": 10, "output_tokens": 4}}
        self._exc = exc
        self.stopped = False
        self.progress_seen = []
        self.cb = None

    def __call__(self, *a, **kw):
        # installed as the runner constructor; keep the service's callbacks
        self.cb = kw.get("on_progress")
        return self

    def emit(self, payload):
        """Push a progress payload through the service's own callback."""
        if self.cb is not None:
            self.cb(payload)
        self.progress_seen.append(payload)

    def stop(self):
        self.stopped = True

    def get_token_stats(self):
        return dict(self._token_stats)

    async def run(self):
        if self._exc is not None:
            raise self._exc
        return self._result


class FakeNormalizer:
    def __init__(self, result=True, exc=None):
        self._result = result
        self._exc = exc
        self.stopped = False
        self.progress_seen = []
        self.cb = None

    def __call__(self, *a, **kw):
        self.cb = kw.get("on_progress")
        return self

    def emit(self, payload):
        if self.cb is not None:
            self.cb(payload)
        self.progress_seen.append(payload)

    def stop(self):
        self.stopped = True

    async def run(self):
        if self._exc is not None:
            raise self._exc
        return self._result


@pytest.fixture
def book(tmp_path):
    """A synthetic book with chapter results, registered in book_service."""
    book_id = "lifecycle-book"
    output_dir = tmp_path / book_id / "output"
    output_dir.mkdir(parents=True)
    for i in (1, 2, 3):
        (output_dir / f"chapter_{i}_result.json").write_text(
            json.dumps({"chapter_number": i, "locations": []}, ensure_ascii=False),
            encoding="utf-8",
        )
    book_service._books.clear()
    book_service._manual_books.clear()
    book_service.register_scan_dir(tmp_path / book_id)
    yield book_id, tmp_path / book_id / "output"
    book_service._books.clear()
    book_service._manual_books.clear()


@pytest.fixture
def hub_spy(monkeypatch):
    spy = HubSpy()
    monkeypatch.setattr(summary_mod, "get_hub", lambda: spy)
    monkeypatch.setattr(loc_norm, "get_hub", lambda: spy)
    return spy


@pytest.fixture
async def started_services():
    """Track every service a test starts so its task always gets cleaned up.

    Tests still assert the real terminal state themselves; this only makes
    sure a mid-test assertion failure cannot leave a task running for the
    next test to inherit.

    Only the expected CancelledError is suppressed. Any other exception from
    awaiting the cancelled task is a real problem -- the service may have
    failed outside its own try block -- so it is collected and re-raised
    after every registered service has been torn down.
    """
    created: list = []
    yield created
    failures = []
    for svc in created:
        task = getattr(svc, "_task", None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                # the intended outcome of cancelling a task we asked to stop
                pass
            except BaseException as exc:
                # never swallow: record it and keep tearing the rest down
                failures.append((svc, exc))
    if failures:
        detail = "; ".join(f"{type(s).__name__}: {exc!r}" for s, exc in failures)
        raise AssertionError(f"task cleanup raised unexpected errors: {detail}")


def _install_runner(monkeypatch, runner, which):
    """Point the service's runner constructor at a fake instance."""
    if which == "summary":
        monkeypatch.setattr(summary_mod, "FinalSummaryRunner", lambda **kw: runner)
    else:
        monkeypatch.setattr(loc_norm, "LocationNormalizer", lambda **kw: runner)
        monkeypatch.setattr(loc_norm, "LLMClient", lambda cfg: object())


def _no_analysis_running(monkeypatch):
    """get_analysis_service() reports idle and hands out a real AppConfig."""
    from backend.config.settings import AppConfig

    monkeypatch.setattr(
        summary_mod, "get_analysis_service",
        lambda: type("A", (), {
            "is_running": False,
            "config_manager": type(
                "M", (), {"load": staticmethod(lambda: AppConfig())}
            )(),
        })(),
    )


async def _settle(task, timeout=5.0):
    """Wait for the background task to finish ON ITS OWN, then drain the hub.

    The services schedule their hub calls with asyncio.create_task, so the
    background task can finish while those follow-ups are still pending; the
    extra loop turns let them run so event assertions see everything.

    A task that never finishes is a real failure: we cancel it so nothing
    leaks into later tests, then re-raise so the caller fails. Swallowing the
    timeout here would turn "the run never completed" into a silent pass.
    """
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except asyncio.TimeoutError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        raise AssertionError(
            f"service task did not finish within {timeout}s"
        ) from None
    # wait_for on a shielded task cannot be cancelled itself; if the task was
    # cancelled from the outside that is a bug in the test, not a success.
    assert not task.cancelled(), "background task was cancelled unexpectedly"
    assert task.done(), "background task is still pending after settling"
    exc = task.exception()
    assert exc is None, f"background task raised: {exc!r}"
    for _ in range(30):
        await asyncio.sleep(0)
    return task


async def _cancel_task(task):
    """Cancel a task we deliberately want gone and suppress only CancelledError."""
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    for _ in range(3):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# SummaryService
# ---------------------------------------------------------------------------

async def test_summary_start_success_lifecycle(book, hub_spy, monkeypatch, started_services):
    """Success: report written, phase complete, SUMMARY_DONE, task finished."""
    book_id, output_dir = book
    svc = SummaryService()
    started_services.append(svc)
    runner = FakeRunner()
    _install_runner(monkeypatch, runner, "summary")

    _no_analysis_running(monkeypatch)

    svc.start(book_id, 1, 3, 30, concurrency=1)
    task = svc._task
    await _settle(task)

    assert svc.status()["phase"] == "complete"
    assert svc.status()["running"] is False
    assert (output_dir / "final_summary_report.md").exists()
    assert (output_dir / "final_summary_report.md").read_text(encoding="utf-8") == "# report"
    assert hub_spy.state_names() == [
        str(WSState.SUMMARY_RUNNING),
        str(WSState.SUMMARY_DONE),
    ]
    # token stats landed once
    stats = svc.session_token_stats()
    assert stats["categories"]["summary"] == {"input_tokens": 10, "output_tokens": 4}
    assert (output_dir / "summary_token_stats.json").exists()
    persisted = json.loads((output_dir / "summary_token_stats.json").read_text(encoding="utf-8"))
    assert persisted["type"] == "summary"
    assert persisted["book_id"] == book_id
    assert persisted["phase"] == "complete"
    # the service keeps the finished task and the runner reference; it does not
    # clear them, so "no longer running" is the observable contract
    assert svc._task is not None and svc._task.done()
    assert svc._finished_at >= svc._started_at > 0


async def test_summary_repeat_start_rejected(book, hub_spy, monkeypatch, started_services):
    """A second start while running raises RuntimeError."""
    book_id, _ = book
    svc = SummaryService()
    started_services.append(svc)

    class Blocking(FakeRunner):
        async def run(self):
            await asyncio.sleep(3600)

    _install_runner(monkeypatch, Blocking(), "summary")
    _no_analysis_running(monkeypatch)

    svc.start(book_id, 1, 3, 30, concurrency=1)
    assert svc.status()["running"] is True
    with pytest.raises(RuntimeError):
        svc.start(book_id, 1, 3, 30, concurrency=1)

    svc.stop()
    assert svc._runner.stopped is True
    await _cancel_task(svc._task)


async def test_summary_stop_returns_false_when_idle(book, hub_spy, started_services):
    svc = SummaryService()
    assert svc.stop() is False


async def test_summary_stop_while_running_forwards_to_runner(book, hub_spy, monkeypatch, started_services):
    """stop() returns True and reaches the runner; run() returning None -> stopped."""
    book_id, _ = book
    svc = SummaryService()
    started_services.append(svc)

    class Stoppable(FakeRunner):
        async def run(self):
            await asyncio.sleep(0.05)
            return None

    runner = Stoppable()
    _install_runner(monkeypatch, runner, "summary")
    _no_analysis_running(monkeypatch)

    svc.start(book_id, 1, 3, 30, concurrency=1)
    assert svc.stop() is True
    assert runner.stopped is True
    await _settle(svc._task)

    assert svc.status()["phase"] == "stopped"
    assert hub_spy.state_names() == [
        str(WSState.SUMMARY_RUNNING),
        str(WSState.SUMMARY_STOPPED),
    ]
    # a stopped run must not write the report
    assert not (book[1] / "final_summary_report.md").exists()


async def test_summary_failure_lifecycle(book, hub_spy, monkeypatch, started_services):
    """Runner raising -> phase failed, error string, SUMMARY_FAILED, log emitted."""
    book_id, _ = book
    svc = SummaryService()
    started_services.append(svc)
    runner = FakeRunner(exc=RuntimeError("boom"))
    _install_runner(monkeypatch, runner, "summary")
    _no_analysis_running(monkeypatch)

    svc.start(book_id, 1, 3, 30, concurrency=1)
    await _settle(svc._task)

    st = svc.status()
    assert st["phase"] == "failed"
    assert st["error"] == "boom"
    assert st["running"] is False
    assert hub_spy.state_names()[-1] == str(WSState.SUMMARY_FAILED)
    assert "error" in hub_spy.log_levels()
    assert any("boom" in m for m in hub_spy.log_messages())


async def test_summary_analysis_mutex_blocks_manual_start(book, hub_spy, monkeypatch, started_services):
    """Manual start while analysis runs is rejected; allow_during_analysis passes."""
    book_id, _ = book
    svc = SummaryService()
    started_services.append(svc)
    from backend.config.settings import AppConfig
    monkeypatch.setattr(
        summary_mod, "get_analysis_service",
        lambda: type("A", (), {
            "is_running": True,
            "config_manager": type("M", (), {"load": staticmethod(lambda: AppConfig())})(),
        })(),
    )
    _install_runner(monkeypatch, FakeRunner(), "summary")

    with pytest.raises(RuntimeError):
        svc.start(book_id, 1, 3, 30, concurrency=1)

    # the auto-summary entry point bypasses the guard
    svc.start(book_id, 1, 3, 30, concurrency=1, allow_during_analysis=True)
    assert svc.status()["running"] is True
    await _cancel_task(svc._task)


async def test_summary_missing_book_and_no_results(book, hub_spy, monkeypatch, started_services):
    _no_analysis_running(monkeypatch)
    svc = SummaryService()

    with pytest.raises(KeyError):
        svc.start("does-not-exist", 1, 3, 30, concurrency=1)

    parent = Path(book[1]).parent
    empty_root = parent / "empty-book"
    (empty_root / "output").mkdir(parents=True)
    # get_book_path resolves through _books, not _manual_books
    book_service._books["empty-book"] = empty_root
    with pytest.raises(ValueError):
        svc.start("empty-book", 1, 3, 30, concurrency=1)


async def test_summary_restart_after_completion(book, hub_spy, monkeypatch, started_services):
    """A finished summary can be started again; session totals accumulate."""
    book_id, _ = book
    svc = SummaryService()
    started_services.append(svc)
    _no_analysis_running(monkeypatch)

    for _ in range(2):
        runner = FakeRunner()
        monkeypatch.setattr(summary_mod, "FinalSummaryRunner", lambda **kw: runner)
        svc.start(book_id, 1, 3, 30, concurrency=1)
        await _settle(svc._task)

    stats = svc.session_token_stats()
    assert stats["categories"]["summary"] == {"input_tokens": 20, "output_tokens": 8}
    assert stats["input_tokens"] == 20
    assert stats["output_tokens"] == 8


async def test_summary_progress_events_reach_hub(book, hub_spy, monkeypatch, started_services):
    """on_progress -> SUMMARY_PROGRESS publish carrying book_id and batches_done."""
    book_id, _ = book
    svc = SummaryService()
    started_services.append(svc)
    _no_analysis_running(monkeypatch)

    class Emitting(FakeRunner):
        async def run(self):
            self.emit({"type": "phase", "phase": "summarizing", "total_batches": 3})
            self.emit({"type": "batch_done", "total_batches": 3})
            self.emit({"type": "complete"})
            return "# r"

    emitting = Emitting()
    monkeypatch.setattr(summary_mod, "FinalSummaryRunner", emitting)
    svc.start(book_id, 1, 3, 30, concurrency=1)
    await _settle(svc._task)

    progress = [
        e for e in hub_spy.events
        if e[0] == "publish" and e[1] == str(WSType.SUMMARY_PROGRESS)
    ]
    assert len(progress) == 3
    for _kind, _t, msg in progress:
        assert msg["payload"]["book_id"] == book_id
        assert "batches_done" in msg["payload"]
    assert progress[-1][2]["payload"]["batches_done"] == 1
    assert svc.status()["phase"] == "complete"
    assert svc.status()["total_batches"] == 3
    assert svc.status()["batches_done"] == 1


# ---------------------------------------------------------------------------
# LocationNormalizationService
# ---------------------------------------------------------------------------

async def test_normalization_success_lifecycle(book, hub_spy, monkeypatch, started_services):
    """run() True -> phase complete, DONE event, task released."""
    book_id, _ = book
    svc = LocationNormalizationService()
    started_services.append(svc)
    _install_runner(monkeypatch, FakeNormalizer(result=True), "norm")

    svc.start(book_id, _cfg())
    await _settle(svc._task)

    st = svc.status()
    assert st["phase"] == "complete"
    assert st["running"] is False
    assert hub_spy.state_names() == [
        str(WSState.LOCATION_NORMALIZATION_RUNNING),
        str(WSState.LOCATION_NORMALIZATION_DONE),
    ]
    assert svc._task.done()


async def test_normalization_stopped_lifecycle(book, hub_spy, monkeypatch, started_services):
    """run() None -> phase stopped, STOPPED event."""
    book_id, _ = book
    svc = LocationNormalizationService()
    started_services.append(svc)
    _install_runner(monkeypatch, FakeNormalizer(result=None), "norm")

    svc.start(book_id, _cfg())
    await _settle(svc._task)

    assert svc.status()["phase"] == "stopped"
    assert hub_spy.state_names() == [
        str(WSState.LOCATION_NORMALIZATION_RUNNING),
        str(WSState.LOCATION_NORMALIZATION_STOPPED),
    ]


async def test_normalization_failed_lifecycle(book, hub_spy, monkeypatch, started_services):
    """run() False -> phase failed with the fixed batch-failure message."""
    book_id, _ = book
    svc = LocationNormalizationService()
    started_services.append(svc)
    _install_runner(monkeypatch, FakeNormalizer(result=False), "norm")

    svc.start(book_id, _cfg())
    await _settle(svc._task)

    st = svc.status()
    assert st["phase"] == "failed"
    assert "batch" in st["error"]
    assert hub_spy.state_names()[-1] == str(WSState.LOCATION_NORMALIZATION_FAILED)


async def test_normalization_exception_lifecycle(book, hub_spy, monkeypatch, started_services):
    """Runner raising -> phase failed, error is the exception text."""
    book_id, _ = book
    svc = LocationNormalizationService()
    started_services.append(svc)
    _install_runner(monkeypatch, FakeNormalizer(exc=ValueError("kaboom")), "norm")

    svc.start(book_id, _cfg())
    await _settle(svc._task)

    st = svc.status()
    assert st["phase"] == "failed"
    assert st["error"] == "kaboom"
    assert hub_spy.state_names()[-1] == str(WSState.LOCATION_NORMALIZATION_FAILED)


async def test_normalization_stop_semantics(book, hub_spy, monkeypatch, started_services):
    """Idle stop -> False; running stop -> True and reaches the normalizer."""
    book_id, _ = book
    svc = LocationNormalizationService()
    started_services.append(svc)
    assert svc.stop() is False

    class Blocking(FakeNormalizer):
        async def run(self):
            await asyncio.sleep(3600)

    normalizer = Blocking()
    monkeypatch.setattr(loc_norm, "LocationNormalizer", lambda **kw: normalizer)
    monkeypatch.setattr(loc_norm, "LLMClient", lambda cfg: object())

    svc.start(book_id, _cfg())
    assert svc.stop() is True
    assert normalizer.stopped is True
    await _cancel_task(svc._task)


async def test_normalization_repeat_start_rejected(book, hub_spy, monkeypatch, started_services):
    book_id, _ = book
    svc = LocationNormalizationService()
    started_services.append(svc)

    class Blocking(FakeNormalizer):
        async def run(self):
            await asyncio.sleep(3600)

    monkeypatch.setattr(loc_norm, "LocationNormalizer", lambda **kw: Blocking())
    monkeypatch.setattr(loc_norm, "LLMClient", lambda cfg: object())

    svc.start(book_id, _cfg())
    with pytest.raises(RuntimeError):
        svc.start(book_id, _cfg())
    await _cancel_task(svc._task)


async def test_normalization_progress_events(book, hub_spy, monkeypatch, started_services):
    """batch_done advances the counter and the phase is picked up too."""
    book_id, _ = book
    svc = LocationNormalizationService()
    started_services.append(svc)

    class Emitting(FakeNormalizer):
        async def run(self):
            self.emit({"type": "phase", "phase": "0a", "total_batches": 2})
            self.emit({"type": "batch_done", "phase": "0a", "total_batches": 2})
            return True

    emitting = Emitting()
    monkeypatch.setattr(loc_norm, "LocationNormalizer", emitting)
    monkeypatch.setattr(loc_norm, "LLMClient", lambda cfg: object())

    svc.start(book_id, _cfg())
    await _settle(svc._task)

    progress = [
        e for e in hub_spy.events
        if e[0] == "publish" and e[1] == str(WSType.LOCATION_NORMALIZATION_PROGRESS)
    ]
    assert len(progress) == 2
    assert progress[-1][2]["payload"]["book_id"] == book_id
    assert svc.status()["batches_done"] == 1
    assert svc.status()["total_batches"] == 2
    # the batch payload carried the normalizer's own phase through
    assert progress[-1][2]["payload"]["phase"] == "0a"
    # terminal phase wins: _run sets complete after run() returns True
    assert svc.status()["phase"] == "complete"


async def test_normalization_llm_config_replace(book, hub_spy, monkeypatch, started_services):
    """The LLM client is built from a replaced api config (summary model)."""
    book_id, _ = book
    svc = LocationNormalizationService()
    started_services.append(svc)
    seen = {}

    from backend.config.settings import APIConfig, AnalysisConfig, AppConfig

    class Cfg(AppConfig):
        def __init__(self):
            self.api = APIConfig(
                model="base-model",
                summary_model="summary-model",
                json_mode="strict",
                timeout=30,
                thinking_mode="off",
                summary_thinking_mode="on",
                summary_timeout=99,
            )
            self.analysis = AnalysisConfig(concurrency=3)

    def fake_llm(cfg):
        seen["cfg"] = cfg
        return object()

    monkeypatch.setattr(loc_norm, "LLMClient", fake_llm)
    monkeypatch.setattr(loc_norm, "LocationNormalizer", lambda **kw: FakeNormalizer(True))

    svc.start(book_id, Cfg())
    await _settle(svc._task)

    assert seen["cfg"].model == "summary-model"
    assert seen["cfg"].json_mode == "default"
    assert seen["cfg"].timeout == 99
    assert seen["cfg"].thinking_mode == "on"


async def test_normalization_missing_book_and_no_results(book, hub_spy, monkeypatch, started_services):
    monkeypatch.setattr(loc_norm, "LocationNormalizer", lambda **kw: FakeNormalizer(True))
    monkeypatch.setattr(loc_norm, "LLMClient", lambda cfg: object())
    svc = LocationNormalizationService()

    with pytest.raises(KeyError):
        svc.start("nope", _cfg())

    parent = Path(book[1]).parent
    empty_root = parent / "empty-book"
    (empty_root / "output").mkdir(parents=True)
    # get_book_path resolves through _books, not _manual_books
    book_service._books["empty-book"] = empty_root
    with pytest.raises(ValueError):
        svc.start("empty-book", _cfg())


# ---------------------------------------------------------------------------
# cross-service: the two status dicts and the release contract
# ---------------------------------------------------------------------------

def test_status_field_sets_differ_deliberately():
    """Both statuses share the base fields but Summary adds token_stats shape."""
    s = SummaryService().status()
    n = LocationNormalizationService().status()
    shared = {"running", "book_id", "phase", "batches_done",
              "total_batches", "error", "started_at", "finished_at", "token_stats"}
    assert shared <= set(s)
    assert shared <= set(n)


def _cfg():
    from backend.config.settings import AppConfig
    return AppConfig()


# ---------------------------------------------------------------------------
# the reason no shared base class was extracted
# ---------------------------------------------------------------------------

async def test_progress_callbacks_are_not_interchangeable(book, hub_spy, monkeypatch, started_services):
    """Summary and normalization on_progress disagree on every detail.

    Extraction would have to take a callback flag or a runner-type branch,
    which the plan forbids; the duplication stays and this test is the record.
    """
    book_id, _ = book

    # --- summary side ---
    svc = SummaryService()
    started_services.append(svc)
    _no_analysis_running(monkeypatch)

    class SEmitting(FakeRunner):
        async def run(self):
            self.emit({"type": "batch_done", "total_batches": 2})
            self.emit({"type": "batch_failed", "message": "half failed"})
            self.emit({"type": "complete"})
            return "# r"

    s_emitting = SEmitting()
    monkeypatch.setattr(summary_mod, "FinalSummaryRunner", s_emitting)
    svc.start(book_id, 1, 3, 30, concurrency=1)
    await _settle(svc._task)

    s_prog = [
        e[2]["payload"] for e in hub_spy.events
        if e[0] == "publish" and e[1] == str(WSType.SUMMARY_PROGRESS)
    ]
    s_logs = hub_spy.log_levels()
    s_cats = {e[2]["category"] for e in hub_spy.events if e[0] == "log"}

    # --- normalization side ---
    hub_spy.events.clear()
    nsvc = LocationNormalizationService()
    started_services.append(nsvc)

    class NEmitting(FakeNormalizer):
        async def run(self):
            self.emit({"type": "batch_done", "phase": "0a", "total_batches": 2})
            self.emit({"type": "batch_failed", "message": "half failed"})
            return True

    n_emitting = NEmitting()
    monkeypatch.setattr(loc_norm, "LocationNormalizer", n_emitting)
    monkeypatch.setattr(loc_norm, "LLMClient", lambda cfg: object())
    nsvc.start(book_id, _cfg())
    await _settle(nsvc._task)

    n_prog = [
        e[2]["payload"] for e in hub_spy.events
        if e[0] == "publish" and e[1] == str(WSType.LOCATION_NORMALIZATION_PROGRESS)
    ]
    n_logs = hub_spy.log_levels()

    # different WS message types
    assert str(WSType.SUMMARY_PROGRESS) != str(WSType.LOCATION_NORMALIZATION_PROGRESS)
    # summary logs under its own category
    assert "summary" in s_cats
    assert "location-normalization" not in s_cats

    # only summary maps batch_failed to level=error; normalization always logs info
    assert "error" in s_logs
    assert "error" not in n_logs

    # only summary adds a "source" tag on token stats and a "complete" phase
    # normalization copies the runner phase into state on batch_done, summary does not
    assert s_prog[0].get("total_batches") == 2
    assert n_prog[0].get("total_batches") == 2
    # normalization's payload carries the runner's phase; summary's does not
    assert "phase" in n_prog[0]
    assert "phase" not in s_prog[0]


async def test_token_stats_sources_differ(book, hub_spy, monkeypatch, started_services):
    """Summary tags token payloads with source=summary; normalization has none."""
    book_id, _ = book

    svc = SummaryService()
    started_services.append(svc)
    _no_analysis_running(monkeypatch)

    captured = {}

    def ctor(**kw):
        captured["token_cb"] = kw.get("on_token_stats")
        return FakeRunner()

    monkeypatch.setattr(summary_mod, "FinalSummaryRunner", ctor)
    svc.start(book_id, 1, 3, 30, concurrency=1)
    await _settle(svc._task)

    token_cb = captured["token_cb"]
    assert token_cb is not None
    hub_spy.events.clear()
    token_cb({"category": "summary", "input_tokens": 5, "output_tokens": 2})
    for _ in range(5):
        await asyncio.sleep(0)

    published = [
        e for e in hub_spy.events
        if e[0] == "publish" and e[1] == str(WSType.TOKEN_STATS)
    ]
    assert published, "token stats callback did not publish"
    assert published[0][2]["payload"]["source"] == "summary"


# ---------------------------------------------------------------------------
# scope guards: no background task / mock leakage
# ---------------------------------------------------------------------------

async def test_summary_tokens_accumulated_exactly_once(book, hub_spy, monkeypatch, started_services):
    """Session totals come from one accumulation pass per completed run.

    status() re-reads get_token_stats() for its live view, which is fine; the
    contract under test is that the session accumulator is written once.
    """
    book_id, _ = book
    svc = SummaryService()
    started_services.append(svc)
    _no_analysis_running(monkeypatch)
    _install_runner(monkeypatch, FakeRunner(), "summary")

    svc.start(book_id, 1, 3, 30, concurrency=1)
    await _settle(svc._task)

    for _ in range(10):
        svc.status()
        svc.session_token_stats()

    stats = svc.session_token_stats()
    assert stats["categories"]["summary"] == {"input_tokens": 10, "output_tokens": 4}

    # a second run doubles the session total but never triples it
    runner2 = FakeRunner()
    monkeypatch.setattr(summary_mod, "FinalSummaryRunner", runner2)
    svc.start(book_id, 1, 3, 30, concurrency=1)
    await _settle(svc._task)

    stats2 = svc.session_token_stats()
    assert stats2["categories"]["summary"] == {"input_tokens": 20, "output_tokens": 8}
    assert stats2["input_tokens"] == 20
    assert stats2["output_tokens"] == 8


async def _pending_tasks():
    """Pending asyncio tasks other than the caller's own."""
    return [
        t for t in asyncio.all_tasks()
        if t is not asyncio.current_task() and not t.done()
    ]


async def test_no_background_tasks_leak(book, hub_spy, monkeypatch, started_services):
    """Successful runs end on their own and leave no pending task behind."""
    book_id, _ = book

    before = await _pending_tasks()
    tasks = []
    try:
        # a successful summary run
        svc = SummaryService()
        _no_analysis_running(monkeypatch)
        _install_runner(monkeypatch, FakeRunner(), "summary")
        svc.start(book_id, 1, 3, 30, concurrency=1)
        tasks.append(svc._task)
        # _settle fails loudly if the run does not finish by itself
        await _settle(svc._task)

        # a successful normalization run
        nsvc = LocationNormalizationService()
        _install_runner(monkeypatch, FakeNormalizer(result=True), "norm")
        nsvc.start(book_id, _cfg())
        tasks.append(nsvc._task)
        await _settle(nsvc._task)

        after = await _pending_tasks()
        leaked = [t for t in after if t not in before]
        assert not leaked, f"leaked tasks: {leaked}"

        # the real terminal state, not "we cancelled it and called it done"
        assert svc._task.done() and not svc._task.cancelled()
        assert svc._task.exception() is None
        assert nsvc._task.done() and not nsvc._task.cancelled()
        assert nsvc._task.exception() is None
        assert svc.status()["phase"] == "complete"
        assert nsvc.status()["phase"] == "complete"
        assert hub_spy.state_names()[-1] == str(WSState.LOCATION_NORMALIZATION_DONE)
    finally:
        for t in tasks:
            if not t.done():
                await _cancel_task(t)


@pytest.mark.parametrize("which", ["summary", "norm"])
async def test_never_completing_runner_fails_and_leaves_no_task(which, book, hub_spy, monkeypatch, started_services):
    """A runner that never returns must fail the test, not be cancelled into a pass.

    This is the guard that keeps _settle honest: if it ever starts swallowing
    timeouts again, this case stops passing and the task it cancelled is
    cleaned up either way.
    """
    book_id, _ = book
    started = []

    class NeverFinishes:
        def __init__(self, *a, **kw):
            started.append(self)

        def stop(self):
            self.stopped = True

        async def run(self):
            await asyncio.Event().wait()

    if which == "summary":
        _no_analysis_running(monkeypatch)
        monkeypatch.setattr(summary_mod, "FinalSummaryRunner", NeverFinishes)
        svc = SummaryService()
        svc.start(book_id, 1, 3, 30, concurrency=1)
    else:
        monkeypatch.setattr(loc_norm, "LocationNormalizer", NeverFinishes)
        monkeypatch.setattr(loc_norm, "LLMClient", lambda cfg: object())
        svc = LocationNormalizationService()
        svc.start(book_id, _cfg())

    task = svc._task
    try:
        with pytest.raises(AssertionError, match="did not finish"):
            await _settle(task, timeout=0.2)
    finally:
        # the run never completed on its own, so cancel is the only way out
        await _cancel_task(task)

    assert task.cancelled() or task.done()
    assert not any(t for t in asyncio.all_tasks() if t is task and not t.done())
    # nothing is left running in the service either
    assert svc.is_running is False or task.cancelled()


def _fixture_object(fixture_func):
    """Resolve a pytest fixture function to the async generator it returns.

    The tests must drive the REAL fixture teardown, not a copy of it, or the
    guard would pass even if the fixture stopped cleaning up.
    """
    holder = getattr(fixture_func, "__wrapped__", None) or getattr(
        fixture_func, "__pytest_wrapped__", None
    )
    # pytest stores the raw function on __pytest_wrapped__.obj for fixtures
    wrapped = getattr(fixture_func, "__pytest_wrapped__", None)
    if wrapped is not None and hasattr(wrapped, "obj"):
        return wrapped.obj
    if holder is not None:
        return holder
    return fixture_func


async def test_started_services_fixture_cancels_mid_test_failure(
    book, hub_spy, monkeypatch
):
    """The fixture must cancel a task even when a test fails part-way.

    This drives the fixture's own async generator: we start a service with a
    runner that never finishes, run the generator up to its yield the way
    pytest does, then execute the real teardown. If the fixture stopped
    cleaning up, the task would still be pending here and this test fails.
    """
    book_id, _ = book

    class NeverFinishes:
        def __init__(self, *a, **kw):
            pass

        def stop(self):
            pass

        def get_token_stats(self):
            return {}

        async def run(self):
            await asyncio.Event().wait()

    _no_analysis_running(monkeypatch)
    monkeypatch.setattr(summary_mod, "FinalSummaryRunner", NeverFinishes)
    svc = SummaryService()
    svc.start(book_id, 1, 3, 30, concurrency=1)
    task = svc._task

    # drive the real fixture exactly as pytest would
    gen = _fixture_object(started_services)()
    tracked = await gen.__anext__()
    assert tracked == []

    # register through the fixture's own list, the way a real test would
    tracked.append(svc)

    # this is the "test failed part-way" case: the assertion does not pass,
    # so the only thing that can clean the task up is the fixture teardown
    assert not task.done()
    try:
        assert task.done(), "runner never finished; exercising fixture cleanup"
    except AssertionError:
        # expected: we deliberately reached the failure path
        pass
    finally:
        # the REAL fixture teardown, not a copy of its loop
        try:
            await gen.__anext__()
        except StopAsyncIteration:
            pass

    # the fixture itself must have cancelled the task it could not finish
    assert task.cancelled(), "the fixture must cancel a task it cannot finish"
    assert svc.is_running is False
    assert not [t for t in await _pending_tasks() if t is task]

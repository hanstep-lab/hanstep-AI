"""Worker 프로세스 준비: 엔진 전용 스레드, 설정 누락 시 종료."""

from __future__ import annotations

import sys
import threading

import pytest

from worker import runner
from worker.config import Settings

pytestmark = pytest.mark.unit


def test_engine_thread_runs_every_call_on_one_thread() -> None:
    engine_thread = runner.EngineThread()
    try:
        names = {engine_thread.call(lambda: threading.current_thread().name) for _ in range(5)}
        assert engine_thread.call(lambda x, y: x + y, 2, 3) == 5
    finally:
        engine_thread.close()

    assert len(names) == 1
    assert names.pop().startswith("ocr-engine")


def test_engine_thread_propagates_errors() -> None:
    engine_thread = runner.EngineThread()

    def boom() -> None:
        raise RuntimeError("engine failed")

    try:
        with pytest.raises(RuntimeError):
            engine_thread.call(boom)
    finally:
        engine_thread.close()


def test_main_exits_before_loading_engine_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "get_settings", lambda: Settings(_env_file=None, rabbitmq_url=""))
    monkeypatch.setattr(runner, "configure_logging", lambda level: None)
    monkeypatch.delitem(sys.modules, "pipeline.engine", raising=False)

    assert runner.main() == runner.EXIT_NOT_CONFIGURED
    assert "pipeline.engine" not in sys.modules  # paddle을 올리지 않는다

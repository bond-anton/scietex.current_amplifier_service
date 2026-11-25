"""Tests for the ``monitoring:*`` control-plane task handler."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import msgspec
import pytest
from _helpers import (
    _make_capabilities,
    _make_context,
    _make_task,
    make_amplifier_settings,
    make_settings,
    register_fake_driver,
)
from scietex.service import ValkeyWorkerConfig
from scietex.service.task_handler import TaskData

from scietex.current_amplifier_service.amplifier_worker import CurrentAmplifierWorker
from scietex.current_amplifier_service.task_handlers import (
    MONITORING_START_TASK_NAME,
    MONITORING_STOP_TASK_NAME,
    MonitoringHandler,
    MonitoringResponse,
)


def _make_worker(tmp_path) -> CurrentAmplifierWorker:
    """A worker with a real conf_dir and no remote-config handlers."""
    return CurrentAmplifierWorker(ValkeyWorkerConfig(service_name="test", conf_dir=str(tmp_path), remote_config_enabled=False))


async def _build(worker: CurrentAmplifierWorker, labels: list[str]) -> None:
    """Seed the registry with one fake board per label."""
    await worker._build_amplifiers(
        make_settings([make_amplifier_settings(label=label, port=f"/dev/ttyUSB{index}") for index, label in enumerate(labels)])
    )


def _handler(worker: CurrentAmplifierWorker) -> MonitoringHandler:
    return MonitoringHandler("monitoring", _make_context(), worker=worker)


async def _teardown(worker: CurrentAmplifierWorker) -> None:
    """Cancel and await any lingering poll task, then close the boards."""
    tasks = list(worker._poll_tasks.values())
    await worker.close_amplifiers()
    await asyncio.gather(*tasks, return_exceptions=True)


def _raw_task(task_name: str, payload: bytes) -> TaskData:
    """A ``TaskData`` carrying an already-encoded payload."""
    return TaskData(task_id=str(uuid4()), task=task_name, payload=payload)


@pytest.fixture(autouse=True)
def _fake_driver(monkeypatch):
    """Advertise the shared fake driver for every test in this module."""
    register_fake_driver(monkeypatch)


def test_handler_is_control_lane() -> None:
    """The monitoring handler is dispatched on the control lane."""
    assert MonitoringHandler.control is True


def test_supported_tasks_are_start_and_stop(tmp_path) -> None:
    """The handler declares exactly the two ``monitoring:*`` task names."""
    handler = _handler(_make_worker(tmp_path))

    assert len(handler.supported_tasks) == 2
    assert set(handler.supported_tasks) == {MONITORING_START_TASK_NAME, MONITORING_STOP_TASK_NAME}
    assert all(name.startswith("monitoring:") for name in handler.supported_tasks)


@pytest.mark.asyncio
async def test_start_without_payload_starts_every_board(tmp_path) -> None:
    """An empty payload starts every board and echoes all labels."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a", "b"])

    result = await _handler(worker).handle(_make_task(MONITORING_START_TASK_NAME, None), capabilities=_make_capabilities())

    assert result.status == "success"
    response = msgspec.json.decode(result.payload, type=MonitoringResponse)
    assert response.action == "start"
    assert response.labels == ["a", "b"]
    assert response.monitoring == {"a": True, "b": True}
    assert worker.monitoring == {"a": True, "b": True}
    await _teardown(worker)


@pytest.mark.asyncio
async def test_start_with_null_label_starts_every_board(tmp_path) -> None:
    """An explicit null label selects every board, same as an empty payload."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a", "b"])

    result = await _handler(worker).handle(
        _make_task(MONITORING_START_TASK_NAME, {"label": None}), capabilities=_make_capabilities()
    )

    assert result.status == "success"
    response = msgspec.json.decode(result.payload, type=MonitoringResponse)
    assert response.labels == ["a", "b"]
    assert response.monitoring == {"a": True, "b": True}
    await _teardown(worker)


@pytest.mark.asyncio
async def test_start_with_label_starts_only_that_board(tmp_path) -> None:
    """A label payload starts only that board and reports it as affected."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a", "b"])

    result = await _handler(worker).handle(
        _make_task(MONITORING_START_TASK_NAME, {"label": "b"}), capabilities=_make_capabilities()
    )

    assert result.status == "success"
    response = msgspec.json.decode(result.payload, type=MonitoringResponse)
    assert response.labels == ["b"]
    assert response.monitoring == {"a": False, "b": True}
    assert worker.monitoring == {"a": False, "b": True}
    await _teardown(worker)


@pytest.mark.asyncio
async def test_start_is_idempotent(tmp_path) -> None:
    """Starting an already-monitoring board affects nothing and reports no label."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    worker.start_monitoring("a")

    result = await _handler(worker).handle(_make_task(MONITORING_START_TASK_NAME, None), capabilities=_make_capabilities())

    assert result.status == "success"
    response = msgspec.json.decode(result.payload, type=MonitoringResponse)
    assert response.labels == []
    assert response.monitoring == {"a": True}
    await _teardown(worker)


@pytest.mark.asyncio
async def test_stop_without_payload_stops_every_board(tmp_path) -> None:
    """An empty payload stops every monitoring board and echoes their labels."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a", "b"])
    worker.start_monitoring()

    result = await _handler(worker).handle(_make_task(MONITORING_STOP_TASK_NAME, None), capabilities=_make_capabilities())

    assert result.status == "success"
    response = msgspec.json.decode(result.payload, type=MonitoringResponse)
    assert response.action == "stop"
    assert response.labels == ["a", "b"]
    assert response.monitoring == {"a": False, "b": False}
    await _teardown(worker)


@pytest.mark.asyncio
async def test_stop_with_label_stops_only_that_board(tmp_path) -> None:
    """A label payload stops only that board and leaves the others running."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a", "b"])
    worker.start_monitoring()

    result = await _handler(worker).handle(
        _make_task(MONITORING_STOP_TASK_NAME, {"label": "b"}), capabilities=_make_capabilities()
    )

    assert result.status == "success"
    response = msgspec.json.decode(result.payload, type=MonitoringResponse)
    assert response.labels == ["b"]
    assert response.monitoring == {"a": True, "b": False}
    assert worker.monitoring == {"a": True, "b": False}
    await _teardown(worker)


@pytest.mark.asyncio
@pytest.mark.parametrize("task", [MONITORING_START_TASK_NAME, MONITORING_STOP_TASK_NAME])
async def test_unknown_label_is_not_retryable(tmp_path, task) -> None:
    """An unknown label yields UNKNOWN_AMPLIFIER and is not retried."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])

    result = await _handler(worker).handle(_make_task(task, {"label": "nope"}), capabilities=_make_capabilities())

    assert result.status == "error"
    assert result.error_code == "UNKNOWN_AMPLIFIER"
    assert result.retryable is False
    await _teardown(worker)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task", "method"),
    [
        (MONITORING_START_TASK_NAME, "start_monitoring"),
        (MONITORING_STOP_TASK_NAME, "stop_monitoring"),
    ],
)
async def test_delegates_the_selector_to_the_worker(tmp_path, monkeypatch, task, method) -> None:
    """The handler forwards the label selector unchanged to the worker method."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    seen: list[str | None] = []

    def _record(label: str | None = None) -> list[str]:
        seen.append(label)
        return [] if label is None else [label]

    monkeypatch.setattr(worker, method, _record)

    all_result = await _handler(worker).handle(_make_task(task, None), capabilities=_make_capabilities())
    one_result = await _handler(worker).handle(_make_task(task, {"label": "a"}), capabilities=_make_capabilities())

    assert seen == [None, "a"]
    assert msgspec.json.decode(all_result.payload, type=MonitoringResponse).labels == []
    assert msgspec.json.decode(one_result.payload, type=MonitoringResponse).labels == ["a"]
    await _teardown(worker)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task", "raw"),
    [
        (MONITORING_START_TASK_NAME, b'{"label": "a", "bogus": 1}'),
        (MONITORING_START_TASK_NAME, b'{"label": 5}'),
        (MONITORING_STOP_TASK_NAME, b"[]"),
        (MONITORING_STOP_TASK_NAME, b"null"),
    ],
)
async def test_malformed_payload_is_rejected(tmp_path, task, raw) -> None:
    """An unknown field, a wrong label type or a non-object payload is INVALID_PAYLOAD."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])

    result = await _handler(worker).handle(_raw_task(task, raw), capabilities=_make_capabilities())

    assert result.status == "error"
    assert result.error_code == "INVALID_PAYLOAD"
    assert result.retryable is False
    assert worker.monitoring == {"a": False}
    await _teardown(worker)


@pytest.mark.asyncio
async def test_worker_failure_is_not_retryable(tmp_path, monkeypatch) -> None:
    """A worker-level failure maps to MONITORING_FAILED and is not retried."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])

    def _fail(_label: str | None = None) -> list[str]:
        raise RuntimeError("poll loop already running")

    monkeypatch.setattr(worker, "start_monitoring", _fail)

    result = await _handler(worker).handle(_make_task(MONITORING_START_TASK_NAME, None), capabilities=_make_capabilities())

    assert result.status == "error"
    assert result.error_code == "MONITORING_FAILED"
    assert result.retryable is False
    assert "poll loop already running" in result.error
    await _teardown(worker)

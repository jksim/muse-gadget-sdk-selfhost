"""Crash reports (protocols/ERROR_REPORT.md): validated, logged, newest kept per gadget."""

import asyncio
import logging
import sqlite3

import pytest
from test_esp32_flows import REGISTER, connect, on_server, open_control

from musehost import crash_report
from musehost.store import Store

FULL = {
    "method": "link.error_report",
    "report": {
        "schema_version": 1,
        "kind": "crash",
        "reason": "panic",
        "consecutive_count": 2,
        "version": "0.1.6",
        "build_sha": "a1b2c3d4e5f60718",
        "uptime_seconds": 5321,
        "fault": {
            "address": "0x42012345",
            "return_address": "0x4200a1b2",
            "cause": "Load access fault",
            "detail": "muse_ui.c:88 in draw",
        },
    },
}
MINIMAL = {
    "method": "link.error_report",
    "report": {"schema_version": 1, "kind": "crash", "reason": "brownout", "consecutive_count": 1},
}


def with_report(**changes) -> dict:
    return {**FULL, "report": {**FULL["report"], **changes}}


def with_fault(**changes) -> dict:
    return with_report(fault={**FULL["report"]["fault"], **changes})


def test_a_full_report_gives_every_contract_field():
    assert crash_report.validate(FULL) == {
        "reason": "panic",
        "consecutive_count": 2,
        "version": "0.1.6",
        "build_sha": "a1b2c3d4e5f60718",
        "uptime_seconds": 5321,
        "fault_address": "0x42012345",
        "fault_return_address": "0x4200a1b2",
        "fault_cause": "Load access fault",
        "fault_detail": "muse_ui.c:88 in draw",
    }


def test_a_minimal_report_leaves_the_optional_fields_empty():
    report = crash_report.validate(MINIMAL)
    assert report["reason"] == "brownout" and report["consecutive_count"] == 1
    assert report["version"] is None and report["fault_cause"] is None


def test_fields_outside_the_contract_are_dropped():
    report = crash_report.validate(with_report(ssid="home", log="line"))
    assert "ssid" not in report and "log" not in report


@pytest.mark.parametrize(
    "message",
    [
        {"method": "link.error_report"},
        {"method": "link.error_report", "report": "panic"},
        with_report(schema_version=2),
        with_report(schema_version=True),
        with_report(kind="hang"),
        with_report(reason="tired"),
        with_report(consecutive_count=0),
        with_report(consecutive_count="1"),
        with_report(version=""),
        with_report(version="v" * 32),
        with_report(version="0.1.6\n"),
        with_report(build_sha="A1B2C3D4"),
        with_report(build_sha="a1b2c3"),
        with_report(uptime_seconds=-1),
        with_report(fault="boom"),
        with_fault(address="0x4201"),
        with_fault(return_address="42012345"),
        with_fault(cause=""),
        with_fault(cause="c" * 48),
        with_fault(detail="d" * 96),
        with_fault(detail="café"),
        with_fault(detail="x" * 95, cause="c" * 47) | {"pad": "p" * 300},
    ],
)
def test_a_report_outside_the_contract_is_refused(message):
    assert crash_report.validate(message) is None


def store_with_device(tmp_path) -> Store:
    store = Store.open(tmp_path / "musehost.db")
    store.db.execute(
        "INSERT INTO devices (node_id, display_name, enrolled_at) VALUES ('homelink-abcdef', '', 1)"
    )
    return store


def test_a_second_report_replaces_the_first(tmp_path):
    store = store_with_device(tmp_path)
    store.save_crash_report("homelink-abcdef", crash_report.validate(FULL), received_at=10)
    store.save_crash_report("homelink-abcdef", crash_report.validate(MINIMAL), received_at=20)
    row = store.crash_report("homelink-abcdef")
    assert (row["received_at"], row["reason"], row["fault_cause"]) == (20, "brownout", None)
    assert store.db.execute("SELECT count(*) FROM crash_reports").fetchone()[0] == 1


def test_deleting_the_device_removes_its_report(tmp_path):
    store = store_with_device(tmp_path)
    store.save_crash_report("homelink-abcdef", crash_report.validate(FULL), received_at=10)
    store.db.execute("DELETE FROM devices WHERE node_id = 'homelink-abcdef'")
    assert store.crash_report("homelink-abcdef") is None


def test_a_database_from_before_crash_reports_gains_the_table(tmp_path):
    path = tmp_path / "musehost.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE devices (node_id TEXT PRIMARY KEY, enrolled_at INTEGER NOT NULL)")
    old.close()
    assert Store.open(path).crash_report("homelink-abcdef") is None


async def send_report(live, message: dict) -> dict:
    """Register, send the report, then prove the stream still works with an invoke."""
    client = await connect(live)
    try:
        sid = await open_control(client, "/api/gadget/control", "gadget")
        await client.send_message(sid, REGISTER)
        await client.read_message(sid)
        await client.send_message(sid, message)
        pending = on_server(
            live, live.app.state.hub.invoke("homelink-abcdef", "device.health", {}, timeout_s=5)
        )
        invoke = await client.read_message(sid)
        await client.send_message(sid, {"method": "link.result", "id": invoke["id"], "ok": True})
        return await pending
    finally:
        await client.close()


def test_a_report_on_the_control_stream_is_logged_and_kept(live, caplog):
    caplog.set_level(logging.INFO, logger="musehost")
    result = asyncio.run(send_report(live, FULL))
    assert result.ok
    lines = [r.getMessage() for r in caplog.records if "crash report" in r.getMessage()]
    assert lines == [
        "crash report from homelink-abcdef: reason=panic count=2 version=0.1.6 "
        "cause=Load access fault"
    ]
    row = live.app.state.tokens.store.crash_report("homelink-abcdef")
    assert row["build_sha"] == "a1b2c3d4e5f60718" and row["fault_detail"] == "muse_ui.c:88 in draw"


def test_a_malformed_report_is_refused_and_the_stream_stays_up(live, caplog):
    caplog.set_level(logging.INFO, logger="musehost")
    result = asyncio.run(send_report(live, with_report(reason="tired")))
    assert result.ok
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warnings] == [
        "ignoring a crash report from homelink-abcdef outside the contract"
    ]
    assert live.app.state.tokens.store.crash_report("homelink-abcdef") is None

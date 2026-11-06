"""Regression tests: no real FunPay requests or sales."""
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import tests_plugin_ad_sync as harness

mod = harness.mod


@pytest.fixture
def stock(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "PLUGIN_DIR", str(tmp_path))
    monkeypatch.setattr(mod, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(mod, "READ_DELAY", 0)
    monkeypatch.setattr(mod, "SAVE_DELAY", 0)
    mod._stop_event.clear()
    store = harness._store({11: [], 22: ["last-account"]})
    for fields in store.values():
        fields.active = True
    crd = harness.MockCardinal(account=harness.MockAccount(store))
    mod._save_settings({"groups": [harness._group([11, 22], ["last-account"])], "interval": 60})
    return crd, store


def test_last_item_not_resurrected_after_failed_save_and_reload(stock):
    crd, store = stock
    with patch.object(crd.account, "save_lot", side_effect=RuntimeError("offline")):
        mod._sync_all(crd)
    assert store[22].secrets == ["last-account"]
    # A fresh settings load represents a restart; no in-memory protection suffices.
    mod._sync_all(crd)
    assert store[11].secrets == store[22].secrets == []


def test_last_item_not_resurrected_after_read_failure(stock):
    crd, store = stock
    crd.account.errors.add(22)
    mod._sync_all(crd)
    crd.account.errors.clear()
    mod._sync_all(crd)
    assert store[11].secrets == store[22].secrets == []


def test_delayed_copy_is_not_a_restock(stock):
    crd, store = stock
    mod._sync_all(crd)
    store[22].secrets = ["last-account", "new-account"]
    mod._sync_all(crd)
    assert store[11].secrets == store[22].secrets == ["new-account"]


def test_sale_between_snapshot_and_save_is_not_overwritten(stock):
    crd, store = stock
    store[11].secrets = ["last-account", "new-account"]
    original_get = crd.account.get_lot_fields
    calls = 0

    def get(lid):
        nonlocal calls
        fields = original_get(lid)
        calls += 1
        if calls == 2:
            store[22].secrets = []  # sold after the initial read
        return fields

    with patch.object(crd.account, "get_lot_fields", side_effect=get):
        mod._sync_all(crd)
    assert "last-account" not in store[22].secrets


def test_unmatched_order_triggers_stock_check(stock):
    crd, store = stock
    workers = []
    real_thread = threading.Thread

    def thread(*args, **kwargs):
        worker = real_thread(*args, **kwargs)
        workers.append(worker)
        return worker

    with patch.object(mod.threading, "Thread", side_effect=thread):
        mod.new_order_handler(crd, SimpleNamespace(lot_id=None))
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()
    assert store[22].secrets == []


def test_corrupt_state_stops_instead_of_reinitializing(stock):
    from pathlib import Path
    Path(mod.SETTINGS_PATH).write_text("{broken", encoding="utf-8")
    with pytest.raises((ValueError, OSError)):
        mod._load_settings()


def test_empty_lots_are_disabled_without_disabling_delivery(stock):
    crd, store = stock
    mod._sync_all(crd)
    assert all(not f.active and f.auto_delivery for f in store.values())
    store[22].secrets = ["new-account"]
    mod._sync_all(crd)
    assert all(f.secrets == ["new-account"] and not f.active for f in store.values())


def test_cannot_write_remote_stock_without_durable_deletion_record(stock):
    crd, store = stock
    with patch.object(mod.os, "replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            mod._sync_all(crd)
    assert crd.account.saved == []
    assert mod._load_settings()["groups"][0]["baseline"] == ["last-account"]


def test_restart_after_crash_between_remote_writes(stock):
    crd, store = stock
    with patch.object(crd.account, "save_lot", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            mod._sync_all(crd)
    assert mod._load_settings()["groups"][0]["removed_hashes"]
    mod._sync_all(crd)
    assert all(f.secrets == [] for f in store.values())


def test_stale_group_cannot_restore_a_sold_item(stock):
    crd, store = stock
    stale = mod._load_settings()["groups"][0]
    mod._sync_all(crd)
    store[22].secrets = ["last-account"]
    mod.sync_group(crd, stale)
    assert all(f.secrets == [] for f in store.values())


def test_queued_sync_loads_settings_inside_the_transaction(stock):
    crd, store = stock
    started, loaded = threading.Event(), threading.Event()
    original_load = mod._load_settings
    errors = []

    def load():
        loaded.set()
        return original_load()

    def run():
        started.set()
        try:
            mod._sync_group_thread(crd, 1)
        except BaseException as exc:
            errors.append(exc)

    with mod._sync_lock:
        with patch.object(mod, "_load_settings", side_effect=load):
            worker = threading.Thread(target=run)
            worker.start()
            assert started.wait(2)
            assert not loaded.wait(0.1), "queued worker read stale settings before acquiring lock"
        mod._sync_all(crd)
    worker.join(5)
    assert not worker.is_alive() and not errors
    assert all(f.secrets == [] for f in store.values())


def test_unavailable_initial_source_is_not_replaced_with_replica(stock):
    crd, store = stock
    settings = mod._load_settings()
    settings["groups"][0]["baseline"] = None
    mod._save_settings(settings)
    crd.account.errors.add(11)
    mod._sync_all(crd)
    assert crd.account.saved == []
    assert mod._load_settings()["groups"][0]["baseline"] is None


def test_silent_remote_failure_is_visible_and_does_not_revive_stock(stock):
    crd, store = stock
    with patch.object(crd.account, "save_lot", return_value=None):
        mod._sync_all(crd)
    group = mod._load_settings()["groups"][0]
    assert 22 in group["last"]["failed"]
    assert group["last"]["changed"] == 0
    mod._sync_all(crd)
    assert all(f.secrets == [] for f in store.values())


def test_failed_lots_are_reported_in_manual_sync(stock):
    crd, store = stock
    crd.account.errors.add(22)
    mod._sync_group_thread(crd, 1, report_to=100)
    text = crd.telegram.bot.messages[-1]["text"]
    assert "22" in text and "Не удалось подтвердить" in text
    assert "уже синхронны" not in text

from datetime import datetime, timedelta

import pytest

from jornada.pim.models import Appointment
from jornada.sync.accounts import Account, AccountError
from jornada.sync.calendar import window
from jornada.sync.calendar.window import WindowedStore, in_window, recurring_mode, resolve_window, sync_window, windowed
from jornada.sync.registry import BuildContext
from tests.memory_store import MemoryStore

NOW = datetime(2026, 3, 1, 15, 45)


def account(**settings) -> Account:
    return Account("c", "calendar", "caldav", tuple(settings.items()))


def appt(summary, start, hours=1, **fields) -> Appointment:
    return Appointment(summary, start, start + timedelta(hours=hours), **fields)


def test_sync_window_defaults_and_settings():
    assert sync_window(account(), NOW) == (datetime(2026, 1, 30), datetime(2027, 3, 2))
    assert sync_window(account(window_past="0", window_future="0"), NOW) == (datetime(2026, 3, 1), datetime(2026, 3, 2))
    assert sync_window(account(window_past=" 7 ", window_future=""), NOW) == (datetime(2026, 2, 22), datetime(2027, 3, 2))
    start, end = sync_window(account())
    assert start <= datetime.now() < end


@pytest.mark.parametrize("value", ["x", "-1", "1.5", "99999"])
def test_bad_window_settings_are_account_errors(value):
    with pytest.raises(AccountError) as info:
        sync_window(account(window_past=value), NOW)
    assert "window_past" in str(info.value) and "'c'" in str(info.value)
    with pytest.raises(AccountError):
        sync_window(account(window_future=value), NOW)


def test_recurring_mode():
    assert recurring_mode(account()) == "skip" and recurring_mode(account(recurring="First")) == "first"
    assert recurring_mode(account(recurring=" ")) == "skip"
    assert window.skips_recurring(account()) and not window.skips_recurring(account(recurring="first"))
    with pytest.raises(AccountError) as info:
        recurring_mode(account(recurring="all"))
    assert "skip or first" in str(info.value)


def test_in_window_uses_overlap():
    start, end = datetime(2026, 3, 1), datetime(2026, 4, 1)
    assert in_window(appt("inside", datetime(2026, 3, 10, 9)), start, end)
    assert in_window(appt("crosses the start", datetime(2026, 2, 28, 23), hours=2), start, end)
    assert in_window(appt("crosses the end", datetime(2026, 3, 31, 23), hours=2), start, end)
    assert in_window(appt("spans everything", datetime(2026, 1, 1), hours=24 * 200), start, end)
    assert not in_window(appt("ends at the start", datetime(2026, 2, 28, 23), hours=1), start, end)
    assert not in_window(appt("starts at the end", datetime(2026, 4, 1), hours=1), start, end)
    assert not in_window(appt("before", datetime(2026, 1, 1)), start, end)
    assert not in_window(object(), start, end)


def test_windowed_store_filters_listing_and_passes_writes_through():
    inner = MemoryStore({
        "1": appt("old", datetime(2025, 1, 1)), "2": appt("now", datetime(2026, 3, 10)),
        "3": appt("weekly", datetime(2026, 3, 11), recurring=True), "4": appt("later", datetime(2028, 1, 1)),
    })
    store = WindowedStore(inner, datetime(2026, 1, 30), datetime(2027, 3, 2))
    assert [item.id for item in store.list()] == ["2", "3"]
    assert store.name == "memory" and store.inner is inner and not store.skip_recurring
    assert store.window == (datetime(2026, 1, 30), datetime(2027, 3, 2))
    skipping = WindowedStore(inner, datetime(2026, 1, 30), datetime(2027, 3, 2), skip_recurring=True)
    assert [item.id for item in skipping.list()] == ["2"] and skipping.skip_recurring
    new = store.create(appt("created", datetime(2025, 6, 1)))          # outside the window: still written
    assert inner.records[new].summary == "created"
    assert store.update("1", appt("old edited", datetime(2025, 1, 1))) is None
    assert inner.records["1"].summary == "old edited"
    store.delete("4")
    assert "4" not in inner.records and inner.calls[-3:] == [("create", new), ("update", "1"), ("delete", "4")]
    with pytest.raises(ValueError):
        WindowedStore(inner, datetime(2026, 1, 1), datetime(2026, 1, 1))


def test_unreadable_records_pass_through_the_window():
    inner = MemoryStore({"1": appt("now", datetime(2026, 3, 10)), "bad": None, "2": appt("old", datetime(2020, 1, 1))})
    store = WindowedStore(inner, datetime(2026, 1, 30), datetime(2027, 3, 2), skip_recurring=True)
    assert [(item.id, item.unreadable) for item in store.list()] == [("1", False), ("bad", True)]


def test_resolve_window_and_windowed_read_the_context_and_account(tmp_path):
    context = BuildContext(log=lambda _l: None, sync_dir=tmp_path, save_secrets=lambda _c: None, extra={"now": NOW})
    assert resolve_window(account(window_past="1", window_future="1"), context) == (datetime(2026, 2, 28), datetime(2026, 3, 3))
    odd = BuildContext(log=lambda _l: None, sync_dir=tmp_path, save_secrets=lambda _c: None, extra={"now": "not a date"})
    start, end = resolve_window(account(), odd)
    assert start <= datetime.now() < end
    inner = MemoryStore({"r": appt("weekly", datetime(2026, 3, 11), recurring=True), "p": appt("plain", datetime(2026, 3, 12))})
    hidden = windowed(inner, account(), datetime(2026, 3, 1), datetime(2026, 4, 1))
    assert [item.id for item in hidden.list()] == ["p"] and hidden.skip_recurring
    shown = windowed(inner, account(recurring="first"), datetime(2026, 3, 1), datetime(2026, 4, 1))
    assert [item.id for item in shown.list()] == ["r", "p"]
    with pytest.raises(AccountError):
        windowed(inner, account(recurring="never"), datetime(2026, 3, 1), datetime(2026, 4, 1))

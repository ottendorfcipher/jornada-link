"""Calendar module: Pocket Outlook's Appointments Database ⇄ Apple Calendar, Google Calendar,
Microsoft 365 or any CalDAV server.

The device side is :class:`~jornada.pim.store.DeviceStore` over the Appointments
Database; each backend implements the :class:`~jornada.sync.base.Store` protocol
and the engine does the three-way merge. Both sides are wrapped in a
:class:`~jornada.sync.calendar.window.WindowedStore`, so only appointments in
the sync window (30 days back, 365 ahead by default) take part; older or later
records are simply absent on both sides and stay untouched. Recurring
appointments are read-only on the device and, by default, kept out of the sync
on both sides (``recurring=first`` syncs a series master as a one-off).
"""
from __future__ import annotations

from ...pim.codecs import APPOINTMENTS
from ...pim.store import DeviceStore
from ...rapi import RapiClient
from ..accounts import Account
from ..registry import BuildContext, ModuleSpec, SettingSpec
from .apple import BACKEND as APPLE
from .caldav import BACKEND as CALDAV
from .google import BACKEND as GOOGLE_CAL
from .m365 import BACKEND as M365
from .window import (DEFAULT_FUTURE_DAYS, DEFAULT_PAST_DAYS, FUTURE_SETTING, PAST_SETTING, RECURRING_SETTING,
                     RECURRING_SKIP, WindowedStore, resolve_window, sync_window, windowed)

SNAPSHOT_SUBDIR = "snapshots"


def device_store(client: RapiClient, account: Account, context: BuildContext) -> WindowedStore:
    """The device's appointments inside the account's window (snapshots go under the sync dir)."""
    start, end = resolve_window(account, context)
    store = DeviceStore(client, APPOINTMENTS, snapshot_dir=context.sync_dir / SNAPSHOT_SUBDIR, log=context.log)
    return windowed(store, account, start, end)


MODULE = ModuleSpec(
    key="calendar",
    title="Calendar (Pocket Outlook Appointments)",
    backends=(APPLE, GOOGLE_CAL, M365, CALDAV),
    device_store=device_store,
    settings=(
        SettingSpec(PAST_SETTING, f"days back to sync (default {DEFAULT_PAST_DAYS})", required=False,
                    default=str(DEFAULT_PAST_DAYS)),
        SettingSpec(FUTURE_SETTING, f"days ahead (default {DEFAULT_FUTURE_DAYS})", required=False,
                    default=str(DEFAULT_FUTURE_DAYS)),
        SettingSpec(RECURRING_SETTING, "remote recurring events: skip (default) or first (sync the master as a one-off)",
                    required=False, default=RECURRING_SKIP),
    ),
    notes="Only appointments inside the window are synced; the device keeps its recurring appointments "
          "read-only, so a change made to a synced series on the modern side is reported, not applied.",
)

__all__ = ["MODULE", "APPLE", "GOOGLE_CAL", "M365", "CALDAV", "device_store", "sync_window", "WindowedStore",
           "SNAPSHOT_SUBDIR"]

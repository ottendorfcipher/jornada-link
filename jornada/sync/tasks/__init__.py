"""Tasks module: the Pocket Outlook Tasks Database ⇄ Apple Reminders, Microsoft To Do,
Google Tasks, a CalDAV calendar (VTODO) or a todo.txt / Markdown checklist file.

The device side is :class:`~jornada.pim.store.DeviceStore` over the Tasks
Database; each backend implements the :class:`~jornada.sync.base.Store`
protocol and the engine does the three-way merge. The module's one setting,
``completed``, decides whether completed tasks take part (see
:mod:`jornada.sync.tasks.filters`).
"""
from __future__ import annotations

from ...pim.codecs import TASKS
from ...pim.store import DeviceStore
from ...rapi import RapiClient
from ..accounts import Account
from ..base import Store
from ..registry import BuildContext, ModuleSpec
from .apple import BACKEND as APPLE
from .caldav import BACKEND as CALDAV
from .filters import COMPLETED_SETTING, wrap_completed
from .gtasks import BACKEND as GTASKS
from .mstodo import BACKEND as MSTODO
from .todotxt import BACKEND as TODOTXT


def device_store(client: RapiClient, account: Account, context: BuildContext) -> Store:
    """The Tasks Database as a Store, filtered by the account's ``completed`` setting."""
    return wrap_completed(DeviceStore(client, TASKS, log=context.log), account, context.log)


MODULE = ModuleSpec(
    key="tasks",
    title="Tasks (Pocket Outlook Tasks)",
    backends=(APPLE, MSTODO, GTASKS, CALDAV, TODOTXT),
    device_store=device_store,
    settings=(COMPLETED_SETTING,),
    notes="Tasks pair up by subject and due date on the first run. Fields a service lacks (priorities in "
          "Google Tasks, categories in Reminders, notes in a todo.txt line) do not travel.",
)

__all__ = ["MODULE", "device_store", "APPLE", "MSTODO", "GTASKS", "CALDAV", "TODOTXT"]

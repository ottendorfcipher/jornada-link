"""Contacts module: Pocket Outlook's Contacts Database ⇄ Apple Contacts, Google Contacts,
Microsoft 365 or any CardDAV server.

The device side is :class:`~jornada.pim.store.DeviceStore` over the Contacts
Database; each backend implements the :class:`~jornada.sync.base.Store` protocol
and the engine does the three-way merge. Contacts pair up by display name and
first e-mail address on the first run. The device holds one number per kind
(a second work or home number lands in the work2 / home2 slot), three e-mail
addresses and one home, work and other address; what a service cannot hold is
listed in each backend's notes.
"""
from __future__ import annotations

from ...pim.codecs import CONTACTS
from ...pim.store import DeviceStore
from ...rapi import RapiClient
from ..accounts import Account
from ..registry import BuildContext, ModuleSpec
from .apple import BACKEND as APPLE
from .carddav import BACKEND as CARDDAV
from .google import BACKEND as GOOGLE_CONTACTS
from .m365 import BACKEND as M365


def device_store(client: RapiClient, account: Account, context: BuildContext) -> DeviceStore:
    """The Contacts Database as a Store (a JSON snapshot precedes the first write of a session)."""
    del account  # the module has no device-side settings
    return DeviceStore(client, CONTACTS, log=context.log)


MODULE = ModuleSpec(
    key="contacts",
    title="Contacts (Pocket Outlook Contacts)",
    backends=(APPLE, GOOGLE_CONTACTS, M365, CARDDAV),
    device_store=device_store,
    notes="Contacts pair up by name and first e-mail address on the first run. The device keeps two numbers "
          "of each kind at most and three e-mail addresses; fields a service lacks (categories in Google and "
          "Apple Contacts, fax and pager numbers in Microsoft 365) do not travel.",
)

__all__ = ["MODULE", "device_store", "APPLE", "GOOGLE_CONTACTS", "M365", "CARDDAV"]

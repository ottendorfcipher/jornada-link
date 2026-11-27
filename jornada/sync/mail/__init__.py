"""Mail module: a POP3/SMTP bridge on the Mac's PPP address, so the device's own Inbox client
reads and sends mail through a modern account (IMAP, Gmail, Microsoft 365, Mail.app).

Unlike the store modules there is no device store and no three-way engine here:
`jornada sync run NAME` starts the bridge and keeps it running until Ctrl-C, and the
device's Inbox stays in step with the real mailbox by talking to it.
"""
from __future__ import annotations

from ..registry import ModuleSpec
from .backend import MailBackend, MailError, MailSummary
from .backends import BACKENDS
from .bridge import MailBridge, run_bridge
from .config import DEFAULT_LISTEN, SERVER_SETTINGS, BridgeConfig

MODULE = ModuleSpec(
    key="mail",
    title="Mail (Inbox bridge for the device's own mail client)",
    backends=BACKENDS,
    bridge=run_bridge,
    settings=SERVER_SETTINGS,
    notes=f"the bridge listens on the PPP address ({DEFAULT_LISTEN}) only; on the Jornada create an Inbox "
          "service with that address as POP3 and SMTP host and the local_user/local_password of the account.",
)

__all__ = ["MODULE", "MailBackend", "MailBridge", "MailError", "MailSummary", "BridgeConfig", "run_bridge"]

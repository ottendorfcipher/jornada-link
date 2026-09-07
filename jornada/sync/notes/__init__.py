"""Notes module: a folder of .txt notes on the Jornada ⇄ Apple Notes, a Logseq graph, or Bear.

Pocket Word saves plain text and HP Quick Pad jottings can be saved as text,
so the device side is one directory of ``.txt`` files (default
``\\My Documents\\Notes``): the file stem is the note's title, the file body
its text. The three backends implement the :class:`~jornada.sync.base.Store`
protocol; the engine does the three-way merge.
"""
from __future__ import annotations

from typing import Optional, Tuple

from ...pim.filestore import DeviceFolderStore, FileCodec
from ...pim.models import Note
from ...pim.textfiles import decode_text, encode_text, safe_filename
from ...rapi import RapiClient
from ..accounts import Account
from ..registry import BuildContext, ModuleSpec, SettingSpec
from .apple import BACKEND as APPLE
from .bear import BACKEND as BEAR
from .common import device_wall_clock
from .logseq import BACKEND as LOGSEQ

DEFAULT_FOLDER = "\\My Documents\\Notes"
FOLDER_SETTING = "folder"
TXT_EXTENSIONS: Tuple[str, ...] = (".txt",)


def decode_txt(name: str, data: bytes, mtime: Optional[float]) -> Note:
    """A device text file → Note: the stem is the title, the text (BOM/encoding aware) the body."""
    stem = name[:-4] if name.lower().endswith(".txt") else name
    return Note(title=stem, body=decode_text(data), modified=device_wall_clock(mtime))


def encode_txt(record: Note) -> Tuple[str, bytes]:
    """Note → (device file name, CRLF ANSI text) as Pocket Word reads it."""
    return safe_filename(record.title, ".txt"), encode_text(record.body)


TXT_CODEC = FileCodec(extensions=TXT_EXTENSIONS, decode=decode_txt, encode=encode_txt)


def device_store(client: RapiClient, account: Account, context: BuildContext) -> DeviceFolderStore:
    folder = (account.setting(FOLDER_SETTING, DEFAULT_FOLDER) or "").strip() or DEFAULT_FOLDER
    return DeviceFolderStore(client, folder, TXT_CODEC, source=f"sync:{account.name}", log=context.log)


MODULE = ModuleSpec(
    key="notes",
    title="Notes",
    backends=(APPLE, LOGSEQ, BEAR),
    device_store=device_store,
    settings=(SettingSpec(FOLDER_SETTING, f"device folder of .txt notes (default {DEFAULT_FOLDER})",
                          required=False, default=DEFAULT_FOLDER),),
    notes="Notes are plain text: formatting, checklists and attachments of the modern side do not travel.",
)

__all__ = ["MODULE", "DEFAULT_FOLDER", "TXT_CODEC", "decode_txt", "encode_txt", "device_store"]

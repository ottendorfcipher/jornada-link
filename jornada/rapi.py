"""RAPI client for Windows CE 2.x devices (librapi2 "old protocol", port 990).

Each call is a length-prefixed frame: ``u32 command`` followed by marshalled
arguments. Replies start with ``u32 result_1`` (1 means an extra HRESULT
follows), then ``u32 last_error``, ``u32 return_value`` and outputs.
"""
from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Iterator, List, Optional

from . import password as pw
from . import transport, wire
from .constants import (
    CMD_CLOSE_HANDLE,
    CMD_CREATE_DIRECTORY,
    CMD_CREATE_FILE,
    CMD_CREATE_PROCESS,
    CMD_CREATE_SHORTCUT,
    CMD_DELETE_FILE,
    CMD_FIND_ALL_FILES,
    CMD_GET_FILE_ATTRIBUTES,
    CMD_GET_STORE_INFORMATION,
    CMD_GET_SYSTEM_POWER_STATUS_EX,
    CMD_GET_VERSION_EX,
    CMD_MOVE_FILE,
    CMD_SYNC_TIME_TO_PC,
    CMD_READ_FILE,
    CMD_REMOVE_DIRECTORY,
    CMD_WRITE_FILE,
    CREATE_ALWAYS,
    FAF_ATTRIBUTES,
    FAF_CREATION_TIME,
    FAF_LASTACCESS_TIME,
    FAF_LASTWRITE_TIME,
    FAF_LISTING,
    FAF_NAME,
    FAF_OID,
    FAF_SIZE_HIGH,
    FAF_SIZE_LOW,
    FILE_ATTRIBUTE_DIRECTORY,
    GENERIC_READ,
    GENERIC_WRITE,
    INVALID_FILE_ATTRIBUTES,
    INVALID_HANDLE_VALUE,
    OPEN_EXISTING,
    RAPI_MAX_FRAME,
    RAPI_PORT,
    RAPI_RECV_TIMEOUT_S,
    READ_CHUNK,
    SIZEOF_PROCESS_INFORMATION,
    SIZEOF_STORE_INFORMATION,
    SIZEOF_SYSTEM_POWER_STATUS_EX,
    WRITE_CHUNK,
)


class RapiError(RuntimeError):
    """A RAPI call failed; ``last_error`` is the Win32 error code if known."""

    def __init__(self, message: str, last_error: int = 0) -> None:
        super().__init__(message)
        self.last_error = last_error


@dataclass(frozen=True)
class FileEntry:
    name: str
    attributes: int
    size: int
    mtime: Optional[float]

    @property
    def is_dir(self) -> bool:
        return bool(self.attributes & FILE_ATTRIBUTE_DIRECTORY)


@dataclass(frozen=True)
class VersionInfo:
    major: int
    minor: int
    build: int
    platform_id: int
    csd_version: str


@dataclass(frozen=True)
class StoreInfo:
    store_size: int
    free_size: int


@dataclass(frozen=True)
class PowerStatus:
    ac_line_status: int
    battery_flag: int
    battery_percent: int
    backup_battery_flag: int
    backup_battery_percent: int


@dataclass(frozen=True)
class Reply:
    last_error: int
    return_value: int
    reader: wire.Reader


class RapiClient:
    def __init__(self, ip: str, port: int = RAPI_PORT, timeout: float = RAPI_RECV_TIMEOUT_S) -> None:
        self._ip = ip
        self._port = port
        self._timeout = timeout
        self._sock: Optional[socket.socket] = None

    # -- connection -----------------------------------------------------------
    def connect(self, password: Optional[str] = None, key: int = 0) -> None:
        sock = socket.create_connection((self._ip, self._port), timeout=self._timeout)
        sock.settimeout(self._timeout)
        if password:
            pw.send_password(sock, password, key)
            if not pw.recv_password_reply(sock, 1):
                sock.close()
                raise RapiError("device rejected the password")
        self._sock = sock

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def __enter__(self) -> "RapiClient":
        if self._sock is None:
            self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- low level ------------------------------------------------------------
    def call(self, command: int, payload: bytes = b"") -> wire.Reader:
        if self._sock is None:
            raise RapiError("not connected")
        transport.send_frame(self._sock, wire.u32(command) + payload)
        reader = wire.Reader(transport.recv_frame(self._sock, max_size=RAPI_MAX_FRAME))
        result_1 = reader.u32()
        if result_1 == 1:
            hresult = reader.u32()
            raise RapiError(f"command 0x{command:02x} failed, HRESULT 0x{hresult:08x}")
        return reader

    def _call_simple(self, command: int, payload: bytes = b"") -> Reply:
        reader = self.call(command, payload)
        last_error = reader.u32()
        return_value = reader.u32()
        return Reply(last_error=last_error, return_value=return_value, reader=reader)

    def _check_bool(self, what: str, reply: Reply) -> None:
        if reply.return_value == 0:
            raise RapiError(f"{what} failed (Win32 error {reply.last_error})", reply.last_error)

    # -- directory listing ----------------------------------------------------
    def find_all_files(self, pattern: str, flags: int = FAF_LISTING) -> List[FileEntry]:
        """CeFindAllFiles: ``pattern`` like ``\\\\My Documents\\\\*`` (device syntax)."""
        reader = self.call(CMD_FIND_ALL_FILES, wire.string(pattern) + wire.u32(flags))
        count = reader.u32()
        return [_read_find_entry(reader, flags) for _ in range(count)]

    def listdir(self, directory: str) -> List[FileEntry]:
        pattern = directory.rstrip("\\") + "\\*" if directory not in ("", "\\") else "\\*"
        return self.find_all_files(pattern)

    # -- file handles ---------------------------------------------------------
    def create_file(
        self,
        path: str,
        access: int,
        share_mode: int = 0,
        disposition: int = OPEN_EXISTING,
        flags_and_attributes: int = 0,
    ) -> int:
        payload = (
            wire.u32(access) + wire.u32(share_mode) + wire.u32(disposition)
            + wire.u32(flags_and_attributes) + wire.u32(0) + wire.string(path)
        )
        reply = self._call_simple(CMD_CREATE_FILE, payload)
        if reply.return_value == INVALID_HANDLE_VALUE:
            raise RapiError(f"cannot open {path!r} (Win32 error {reply.last_error})", reply.last_error)
        return reply.return_value

    def read_file(self, handle: int, size: int) -> bytes:
        payload = wire.u32(handle) + wire.optional_out(size) + wire.optional_in(None)
        reply = self._call_simple(CMD_READ_FILE, payload)
        self._check_bool("CeReadFile", reply)
        bytes_read = reply.reader.u32()
        return reply.reader.take(bytes_read)

    def write_file(self, handle: int, data: bytes) -> int:
        payload = wire.u32(handle) + wire.optional_in(data) + wire.optional_in(None)
        reply = self._call_simple(CMD_WRITE_FILE, payload)
        self._check_bool("CeWriteFile", reply)
        return reply.reader.u32()

    def close_handle(self, handle: int) -> None:
        reply = self._call_simple(CMD_CLOSE_HANDLE, wire.u32(handle))
        self._check_bool("CeCloseHandle", reply)

    # -- whole-file helpers ---------------------------------------------------
    def iter_download(self, path: str, chunk: int = READ_CHUNK) -> Iterator[bytes]:
        handle = self.create_file(path, GENERIC_READ, share_mode=1, disposition=OPEN_EXISTING)
        try:
            while True:
                data = self.read_file(handle, chunk)
                if not data:
                    break
                yield data
        finally:
            self.close_handle(handle)

    def download(self, path: str) -> bytes:
        return b"".join(self.iter_download(path))

    def upload(self, path: str, data: bytes, chunk: int = WRITE_CHUNK, progress=None) -> int:
        handle = self.create_file(path, GENERIC_WRITE, disposition=CREATE_ALWAYS)
        written_total = 0
        try:
            for offset in range(0, len(data), chunk):
                piece = data[offset:offset + chunk]
                written = self.write_file(handle, piece)
                if written != len(piece):
                    raise RapiError(f"short write: {written} of {len(piece)} bytes")
                written_total += written
                if progress is not None:
                    progress(written_total, len(data))
        finally:
            self.close_handle(handle)
        return written_total

    # -- file management ------------------------------------------------------
    def create_directory(self, path: str) -> None:
        payload = wire.optional_string(path) + wire.optional_in(None)
        self._check_bool("CeCreateDirectory", self._call_simple(CMD_CREATE_DIRECTORY, payload))

    def remove_directory(self, path: str) -> None:
        self._check_bool("CeRemoveDirectory",
                         self._call_simple(CMD_REMOVE_DIRECTORY, wire.optional_string(path)))

    def delete_file(self, path: str) -> None:
        self._check_bool("CeDeleteFile", self._call_simple(CMD_DELETE_FILE, wire.optional_string(path)))

    def move_file(self, src: str, dst: str) -> None:
        payload = wire.optional_string(src) + wire.optional_string(dst)
        self._check_bool("CeMoveFile", self._call_simple(CMD_MOVE_FILE, payload))

    def get_file_attributes(self, path: str) -> Optional[int]:
        reply = self._call_simple(CMD_GET_FILE_ATTRIBUTES, wire.string(path))
        if reply.return_value == INVALID_FILE_ATTRIBUTES:
            return None
        return reply.return_value

    # -- processes ------------------------------------------------------------
    def create_process(self, application: str, command_line: Optional[str] = None) -> int:
        payload = (
            wire.optional_string(application) + wire.optional_string(command_line)
            + wire.u32(0) * 7 + wire.optional_out(SIZEOF_PROCESS_INFORMATION)
        )
        reply = self._call_simple(CMD_CREATE_PROCESS, payload)
        self._check_bool("CeCreateProcess", reply)
        info = reply.reader.optional()
        if info is None or len(info) < SIZEOF_PROCESS_INFORMATION:
            return 0
        return wire.Reader(info, 8).u32()  # dwProcessId

    def create_shortcut(self, shortcut_path: str, target: str) -> None:
        """CeSHCreateShortcut: make a .lnk on the device pointing at ``target``."""
        payload = wire.optional_string(shortcut_path) + wire.optional_string(target)
        self._check_bool("CeSHCreateShortcut", self._call_simple(CMD_CREATE_SHORTCUT, payload))

    def sync_time_from_mac(self, now: Optional[float] = None, use_local: bool = True) -> None:
        """CeSyncTimeToPc: set the device clock (date and time) from this Mac.

        The FILETIME carries the full date and time, so both are set. By default
        the Mac's **local** wall-clock is pushed, so the Jornada reads the same
        date and time you see on the Mac. Windows CE stores time as UTC and
        applies its own time-zone for display; a device whose zone is unset or
        wrong would otherwise show the UTC date (a day ahead in the evening).
        Pass ``use_local=False`` to send true UTC when the device's own
        time-zone is correctly configured.
        """
        import time as _time
        unix_now = _time.time() if now is None else now
        offset = _time.localtime(unix_now).tm_gmtoff if use_local else 0
        ticks = int(((unix_now + offset) * 10_000_000) + 116_444_736_000_000_000)
        payload = (
            wire.u32(ticks & 0xFFFFFFFF) + wire.u32(ticks >> 32)
            + wire.u32(0) + wire.u32(10_000)
        )
        reader = self.call(CMD_SYNC_TIME_TO_PC, payload)
        reader.u32()  # last_error (SynCE synthesizes success; verify via read_device_clock)

    def read_device_clock(self, probe_dir: str = "\\Temp") -> Optional[float]:
        """Read the device's current clock by timestamping a throwaway file.

        Returns the device wall-clock as a Unix timestamp whose UTC calendar
        fields are the device's displayed date and time (CE stamps file times
        from its own clock). ``None`` if the probe could not be read back.
        """
        probe = probe_dir.rstrip("\\") + "\\.jornada_clock"
        try:
            self.upload(probe, b"")
            entry = next((e for e in self.listdir(probe_dir) if e.name == ".jornada_clock"), None)
        finally:
            try:
                self.delete_file(probe)
            except (RapiError, OSError):
                pass
        return entry.mtime if entry is not None else None

    # -- system information ---------------------------------------------------
    def get_version(self) -> VersionInfo:
        reply = self._call_simple(CMD_GET_VERSION_EX)
        self._check_bool("CeGetVersionEx", reply)
        size = reply.reader.u32()
        data = wire.Reader(reply.reader.take(size))
        data.u32()  # dwOSVersionInfoSize
        major, minor, build, platform_id = data.u32(), data.u32(), data.u32(), data.u32()
        csd = data.wchars(data.remaining // 2) if data.remaining else ""
        return VersionInfo(major, minor, build, platform_id, csd)

    def get_store_information(self) -> StoreInfo:
        reply = self._call_simple(CMD_GET_STORE_INFORMATION, wire.optional_out(SIZEOF_STORE_INFORMATION))
        self._check_bool("CeGetStoreInformation", reply)
        data = reply.reader.optional()
        if data is None or len(data) < SIZEOF_STORE_INFORMATION:
            raise RapiError("CeGetStoreInformation returned no data")
        reader = wire.Reader(data)
        return StoreInfo(store_size=reader.u32(), free_size=reader.u32())

    def get_power_status(self) -> PowerStatus:
        payload = wire.optional_out(SIZEOF_SYSTEM_POWER_STATUS_EX) + wire.u32(1)
        reply = self._call_simple(CMD_GET_SYSTEM_POWER_STATUS_EX, payload)
        self._check_bool("CeGetSystemPowerStatusEx", reply)
        data = reply.reader.optional()
        if data is None or len(data) < SIZEOF_SYSTEM_POWER_STATUS_EX:
            raise RapiError("CeGetSystemPowerStatusEx returned no data")
        return PowerStatus(
            ac_line_status=data[0],
            battery_flag=data[1],
            battery_percent=data[2],
            backup_battery_flag=data[13],
            backup_battery_percent=data[14],
        )


def _read_find_entry(reader: wire.Reader, flags: int) -> FileEntry:
    name_size = reader.u32() if flags & FAF_NAME else 0
    attributes = reader.u32() if flags & FAF_ATTRIBUTES else 0
    if flags & FAF_CREATION_TIME:
        reader.u32(); reader.u32()
    if flags & FAF_LASTACCESS_TIME:
        reader.u32(); reader.u32()
    mtime = None
    if flags & FAF_LASTWRITE_TIME:
        low, high = reader.u32(), reader.u32()
        mtime = wire.filetime_to_unix(low, high)
    size_high = reader.u32() if flags & FAF_SIZE_HIGH else 0
    size_low = reader.u32() if flags & FAF_SIZE_LOW else 0
    if flags & FAF_OID:
        reader.u32()
    name = reader.wchars(name_size) if flags & FAF_NAME else ""
    return FileEntry(name=name, attributes=attributes, size=(size_high << 32) | size_low, mtime=mtime)

"""An in-memory fake of a Windows CE 2.x device's RAPI server and ActiveSync client.

Used by the test-suite (and handy for developing the CLI without hardware).
The RAPI half serves the same command subset the client implements, over an
in-memory filesystem. The "ActiveSync client" half connects to a dccm port
exactly like the device does after PPP comes up.
"""
from __future__ import annotations

import socket
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional, Set

from jornada import wire
from jornada.constants import (
    CMD_CLOSE_HANDLE,
    CMD_CREATE_DATABASE,
    CMD_CREATE_DIRECTORY,
    CMD_CREATE_FILE,
    CMD_CREATE_PROCESS,
    CMD_CREATE_SHORTCUT,
    CMD_DELETE_DATABASE,
    CMD_DELETE_FILE,
    CMD_DELETE_RECORD,
    CMD_FIND_ALL_DATABASES,
    CMD_FIND_ALL_FILES,
    CMD_GET_FILE_ATTRIBUTES,
    CMD_GET_STORE_INFORMATION,
    CMD_GET_SYSTEM_POWER_STATUS_EX,
    CMD_GET_VERSION_EX,
    CMD_MOVE_FILE,
    CMD_OPEN_DATABASE,
    CMD_READ_FILE,
    CMD_READ_RECORD_PROPS,
    CMD_SEEK_DATABASE,
    CMD_SYNC_TIME_TO_PC,
    CMD_REMOVE_DIRECTORY,
    CMD_WRITE_FILE,
    CMD_WRITE_RECORD_PROPS,
    CREATE_ALWAYS,
    DCCM_PING,
    FAF_ATTRIBUTES,
    FAF_CREATION_TIME,
    FAF_LASTACCESS_TIME,
    FAF_LASTWRITE_TIME,
    FAF_NAME,
    FAF_OID,
    FAF_SIZE_HIGH,
    FAF_SIZE_LOW,
    FILE_ATTRIBUTE_ARCHIVE,
    FILE_ATTRIBUTE_DIRECTORY,
    GENERIC_WRITE,
    INVALID_FILE_ATTRIBUTES,
    INVALID_HANDLE_VALUE,
    OPEN_EXISTING,
)
from jornada.info import DeviceInfo, build_info_packet
from jornada.transport import recv_exact, recv_frame, send_frame
from tests.fake_cedb import FakeDatabaseStore

ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_INVALID_HANDLE = 6
ERROR_DIR_NOT_EMPTY = 145

# The fake's legacy fixed last-write time, still used for files with no stamp.
DEFAULT_FILETIME = (0x01D9E0F0 << 32) | 0x9B2C3D4E


def _unix_to_filetime(unix_seconds: float) -> int:
    return int(unix_seconds * 10_000_000) + 116_444_736_000_000_000

JORNADA_INFO = DeviceInfo(
    os_version=0x0B02, build_number=11171, processor_type=0x2A11,
    partner_id_1=0x1234, partner_id_2=0x5678,
    name="Jornada680", device_class="HPC", hardware="SH3",
)


@dataclass
class OpenFile:
    path: str
    position: int = 0
    writable: bool = False


@dataclass
class FakeFilesystem:
    files: Dict[str, bytes] = field(default_factory=dict)
    dirs: Set[str] = field(default_factory=lambda: {"\\"})
    mtimes: Dict[str, int] = field(default_factory=dict)  # path -> FILETIME (100ns since 1601)

    def parent(self, path: str) -> str:
        head = path.rstrip("\\").rsplit("\\", 1)[0]
        return head or "\\"

    def exists_dir(self, path: str) -> bool:
        return path.rstrip("\\") in {d.rstrip("\\") for d in self.dirs} or path == "\\"


def _ok(return_value: int, extra: bytes = b"", last_error: int = 0) -> bytes:
    return wire.u32(0) + wire.u32(last_error) + wire.u32(return_value) + extra


def _read_string(reader: wire.Reader) -> Optional[str]:
    present = reader.u32()
    if present != 1:
        return None
    length = reader.u32()
    return reader.wchars(length)


def _read_optional_string(reader: wire.Reader) -> Optional[str]:
    data = reader.optional()
    if data is None:
        return None
    return data.decode("utf-16-le").split("\x00", 1)[0]


class FakeRapiServer:
    """Serves RAPI on 127.0.0.1:<port> in a background thread."""

    def __init__(self, fs: Optional[FakeFilesystem] = None, password: Optional[str] = None, key: int = 0x42,
                 db: Optional[FakeDatabaseStore] = None) -> None:
        self.fs = fs or FakeFilesystem()
        self.db = db or FakeDatabaseStore()
        self.password = password
        self.key = key
        self.launched: list = []
        self.clock_set_to: list = []
        # Device clock (Unix seconds) once set; stamps newly-written files so a
        # read_device_clock() round-trip is faithful. None until first sync.
        self.clock: Optional[float] = None
        self.shortcuts: list = []
        self._handles: Dict[int, OpenFile] = {}
        self._next_handle = 0x100
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._stopping = False

    def start(self) -> "FakeRapiServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stopping = True
        self._listener.close()

    def _accept_loop(self) -> None:
        while not self._stopping:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            if self.password:
                size = wire.Reader(recv_exact(conn, 2)).u16()
                encoded = recv_exact(conn, size)
                decoded = bytes(b ^ self.key for b in encoded).decode("utf-16-le").split("\x00", 1)[0]
                conn.sendall(b"\x01" if decoded == self.password else b"\x00")
                if decoded != self.password:
                    return
            while True:
                request = recv_frame(conn)
                send_frame(conn, self.dispatch(request))
        except (ConnectionError, OSError):
            pass
        finally:
            conn.close()

    # -- command dispatch ----------------------------------------------------
    def dispatch(self, request: bytes) -> bytes:
        reader = wire.Reader(request)
        command = reader.u32()
        handler = {
            CMD_FIND_ALL_FILES: self._find_all_files,
            CMD_CREATE_FILE: self._create_file,
            CMD_READ_FILE: self._read_file,
            CMD_WRITE_FILE: self._write_file,
            CMD_CLOSE_HANDLE: self._close_handle,
            CMD_CREATE_DIRECTORY: self._create_directory,
            CMD_REMOVE_DIRECTORY: self._remove_directory,
            CMD_DELETE_FILE: self._delete_file,
            CMD_MOVE_FILE: self._move_file,
            CMD_GET_FILE_ATTRIBUTES: self._get_file_attributes,
            CMD_CREATE_PROCESS: self._create_process,
            CMD_GET_VERSION_EX: self._get_version,
            CMD_GET_STORE_INFORMATION: self._get_store_information,
            CMD_GET_SYSTEM_POWER_STATUS_EX: self._get_power_status,
            CMD_SYNC_TIME_TO_PC: self._sync_time,
            CMD_CREATE_SHORTCUT: self._create_shortcut,
            CMD_FIND_ALL_DATABASES: self.db.find_all_databases,
            CMD_OPEN_DATABASE: self.db.open_database,
            CMD_CREATE_DATABASE: self.db.create_database,
            CMD_DELETE_DATABASE: self.db.delete_database,
            CMD_READ_RECORD_PROPS: self.db.read_record_props,
            CMD_WRITE_RECORD_PROPS: self.db.write_record_props,
            CMD_DELETE_RECORD: self.db.delete_record,
            CMD_SEEK_DATABASE: self.db.seek_database,
        }.get(command)
        if handler is None:
            return wire.u32(1) + wire.u32(0x80004001)  # E_NOTIMPL as result_2
        return handler(reader)

    def _find_all_files(self, reader: wire.Reader) -> bytes:
        pattern = _read_string(reader) or ""
        flags = reader.u32()
        directory = pattern.rsplit("\\", 1)[0] or "\\"
        entries = []
        for path in sorted(self.fs.dirs):
            if path != "\\" and self.fs.parent(path) == directory:
                entries.append((path.rsplit("\\", 1)[-1], FILE_ATTRIBUTE_DIRECTORY, 0, DEFAULT_FILETIME))
        for path, data in sorted(self.fs.files.items()):
            if self.fs.parent(path) == directory:
                stamp = self.fs.mtimes.get(path, DEFAULT_FILETIME)
                entries.append((path.rsplit("\\", 1)[-1], FILE_ATTRIBUTE_ARCHIVE, len(data), stamp))
        out = wire.u32(0) + wire.u32(len(entries))
        for name, attributes, size, filetime in entries:
            encoded = wire.wstr(name)
            if flags & FAF_NAME:
                out += wire.u32(len(encoded) // 2)
            if flags & FAF_ATTRIBUTES:
                out += wire.u32(attributes)
            if flags & FAF_CREATION_TIME:
                out += wire.u32(0) * 2
            if flags & FAF_LASTACCESS_TIME:
                out += wire.u32(0) * 2
            if flags & FAF_LASTWRITE_TIME:
                out += wire.u32(filetime & 0xFFFFFFFF) + wire.u32(filetime >> 32)
            if flags & FAF_SIZE_HIGH:
                out += wire.u32(0)
            if flags & FAF_SIZE_LOW:
                out += wire.u32(size)
            if flags & FAF_OID:
                out += wire.u32(0)
            if flags & FAF_NAME:
                out += encoded
        return out

    def _create_file(self, reader: wire.Reader) -> bytes:
        access, _share, disposition, _flags, _template = (reader.u32() for _ in range(5))
        path = _read_string(reader) or ""
        writable = bool(access & GENERIC_WRITE)
        if disposition == OPEN_EXISTING and path not in self.fs.files:
            return _ok(INVALID_HANDLE_VALUE, last_error=ERROR_FILE_NOT_FOUND)
        if disposition == CREATE_ALWAYS:
            if not self.fs.exists_dir(self.fs.parent(path)):
                return _ok(INVALID_HANDLE_VALUE, last_error=ERROR_PATH_NOT_FOUND)
            self.fs.files[path] = b""
            if self.clock is not None:
                self.fs.mtimes[path] = _unix_to_filetime(self.clock)
        handle = self._next_handle
        self._next_handle += 1
        self._handles[handle] = OpenFile(path=path, writable=writable)
        return _ok(handle)

    def _read_file(self, reader: wire.Reader) -> bytes:
        handle = reader.u32()
        present = reader.u32()
        size = reader.u32() if present else 0
        if present:
            reader.u32()  # has_value = 0
        open_file = self._handles.get(handle)
        if open_file is None:
            return _ok(0, wire.u32(0), ERROR_INVALID_HANDLE)
        data = self.fs.files[open_file.path]
        chunk = data[open_file.position:open_file.position + size]
        open_file.position += len(chunk)
        return _ok(1, wire.u32(len(chunk)) + chunk)

    def _write_file(self, reader: wire.Reader) -> bytes:
        handle = reader.u32()
        present = reader.u32()
        data = b""
        if present:
            size = reader.u32()
            data = reader.take(size)
        open_file = self._handles.get(handle)
        if open_file is None or not open_file.writable:
            return _ok(0, wire.u32(0), ERROR_INVALID_HANDLE)
        current = self.fs.files[open_file.path]
        self.fs.files[open_file.path] = current[:open_file.position] + data
        open_file.position += len(data)
        return _ok(1, wire.u32(len(data)))

    def _close_handle(self, reader: wire.Reader) -> bytes:
        handle = reader.u32()
        if self._handles.pop(handle, None) is None and not self.db.close(handle):
            return _ok(0, last_error=ERROR_INVALID_HANDLE)
        return _ok(1)

    def _create_directory(self, reader: wire.Reader) -> bytes:
        path = _read_optional_string(reader) or ""
        if not self.fs.exists_dir(self.fs.parent(path)):
            return _ok(0, last_error=ERROR_PATH_NOT_FOUND)
        self.fs.dirs.add(path)
        return _ok(1)

    def _remove_directory(self, reader: wire.Reader) -> bytes:
        path = _read_optional_string(reader) or ""
        if path not in self.fs.dirs:
            return _ok(0, last_error=ERROR_PATH_NOT_FOUND)
        if any(self.fs.parent(p) == path for p in list(self.fs.files) + list(self.fs.dirs - {path})):
            return _ok(0, last_error=ERROR_DIR_NOT_EMPTY)
        self.fs.dirs.discard(path)
        return _ok(1)

    def _delete_file(self, reader: wire.Reader) -> bytes:
        path = _read_optional_string(reader) or ""
        if self.fs.files.pop(path, None) is None:
            return _ok(0, last_error=ERROR_FILE_NOT_FOUND)
        return _ok(1)

    def _move_file(self, reader: wire.Reader) -> bytes:
        src = _read_optional_string(reader) or ""
        dst = _read_optional_string(reader) or ""
        if src not in self.fs.files:
            return _ok(0, last_error=ERROR_FILE_NOT_FOUND)
        self.fs.files[dst] = self.fs.files.pop(src)
        return _ok(1)

    def _get_file_attributes(self, reader: wire.Reader) -> bytes:
        path = _read_string(reader) or ""
        if path in self.fs.files:
            return _ok(FILE_ATTRIBUTE_ARCHIVE)
        if path in self.fs.dirs:
            return _ok(FILE_ATTRIBUTE_DIRECTORY)
        return _ok(INVALID_FILE_ATTRIBUTES, last_error=ERROR_FILE_NOT_FOUND)

    def _create_process(self, reader: wire.Reader) -> bytes:
        application = _read_optional_string(reader)
        command_line = _read_optional_string(reader)
        self.launched.append((application, command_line))
        pid = 0x1000 + len(self.launched)
        info = wire.u32(0x10) + wire.u32(0x11) + wire.u32(pid) + wire.u32(0x12)
        return _ok(1, wire.u32(1) + wire.u32(len(info)) + wire.u32(1) + info)

    def _create_shortcut(self, reader: wire.Reader) -> bytes:
        shortcut = _read_optional_string(reader)
        target = _read_optional_string(reader)
        self.shortcuts.append((shortcut, target))
        return _ok(1)

    def _sync_time(self, reader: wire.Reader) -> bytes:
        low, high = reader.u32(), reader.u32()
        ticks = (high << 32) | low
        wall = (ticks - 116_444_736_000_000_000) / 10_000_000
        self.clock_set_to.append(wall)
        self.clock = wall
        return wire.u32(0) + wire.u32(0)  # result_1, last_error (no return value)

    def _get_version(self, reader: wire.Reader) -> bytes:
        csd = wire.wstr("")
        payload = wire.u32(20 + len(csd)) + wire.u32(2) + wire.u32(11) + wire.u32(11171) + wire.u32(3) + csd
        return _ok(1, wire.u32(len(payload)) + payload)

    def _get_store_information(self, reader: wire.Reader) -> bytes:
        data = wire.u32(16 * 1024 * 1024) + wire.u32(9 * 1024 * 1024)
        return _ok(1, wire.u32(1) + wire.u32(len(data)) + wire.u32(1) + data)

    def _get_power_status(self, reader: wire.Reader) -> bytes:
        data = bytes([1, 0x08, 77, 0]) + wire.u32(0xFFFFFFFF) * 2 + bytes([0, 0x01, 100, 0]) + wire.u32(0xFFFFFFFF) * 2
        return _ok(1, wire.u32(1) + wire.u32(len(data)) + wire.u32(1) + data)


class FakeActiveSyncClient:
    """Connects to a dccm port like the device does and answers pings."""

    def __init__(self, port: int, info: DeviceInfo = JORNADA_INFO, password: Optional[str] = None, key: int = 0x5A) -> None:
        self._port = port
        self._info = info
        self._password = password
        self._key = key
        self.pings_seen = 0
        self.password_ok: Optional[bool] = None
        self._sock: Optional[socket.socket] = None

    def connect(self) -> None:
        sock = socket.create_connection(("127.0.0.1", self._port), timeout=5)
        self._sock = sock
        if self._password:
            # challenge: header >= 512 whose low byte is the key
            sock.sendall(wire.u32(0x1000 | self._key))
            size = wire.Reader(recv_exact(sock, 2)).u16()
            encoded = recv_exact(sock, size)
            decoded = bytes(b ^ self._key for b in encoded).decode("utf-16-le").split("\x00", 1)[0]
            self.password_ok = decoded == self._password
        body = build_info_packet(self._info)
        sock.sendall(wire.u32(len(body)) + body)
        if self._password:
            sock.sendall(wire.u16(1 if self.password_ok else 0))
        first = wire.Reader(recv_exact(sock, 4)).u32()
        if first != DCCM_PING:
            raise AssertionError(f"expected ping after info packet, got 0x{first:08x}")
        self.pings_seen += 1

    def answer_pings(self, count: int) -> None:
        assert self._sock is not None
        for _ in range(count):
            header = wire.Reader(recv_exact(self._sock, 4)).u32()
            if header == DCCM_PING:
                self.pings_seen += 1
                self._sock.sendall(wire.u32(DCCM_PING))

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()

import json
from pathlib import Path

import pytest

from jornada import cli
from jornada.cedb import PropVal
from jornada.pim import ids
from tests.fake_device import FakeRapiServer


@pytest.fixture
def device():
    server = FakeRapiServer()
    server.db.create(ids.DB_TASKS, records=[
        (PropVal.string(ids.SUBJECT, "Buy batteries"), PropVal.filetime(ids.TASK_DUE, 133_000_000_000_000_000),
         PropVal.i4(ids.IMPORTANCE, ids.IMPORTANCE_HIGH)),
        (PropVal.string(ids.SUBJECT, "Call mum"),),
    ])
    server.db.create("Inbox Folders", records=[(PropVal.string(0x10, "Inbox"), PropVal.i4(0x11, 3))], db_type=0x77)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def run(device, *argv):
    return cli.main(["--ip", "127.0.0.1", "--rapi-port", str(device.port), "db", *argv])


def test_ls_dump_and_raw(device, capsys):
    assert run(device, "ls") == 0
    out = capsys.readouterr().out
    assert "Tasks Database" in out and "Inbox Folders" in out and "2 database(s)" in out and "0x0077" in out
    assert run(device, "dump", "Tasks Database") == 0
    out = capsys.readouterr().out
    assert "summary='Buy batteries'" in out and "priority='high'" in out and "2 record(s)" in out
    assert run(device, "dump", "Tasks Database", "--raw", "--limit", "1") == 0
    out = capsys.readouterr().out
    assert "0x0037 string   'Buy batteries'" in out and "showing 1" in out
    assert run(device, "dump", "Inbox Folders", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["props"][0] == {"id": 0x10, "kind": "string", "value": "Inbox", "flags": 0}
    assert run(device, "dump", "Nope") == 1 and "no database" in capsys.readouterr().err


def test_snapshot_and_restore(device, tmp_path: Path, capsys):
    assert run(device, "snapshot", "Tasks Database", "--dir", str(tmp_path)) == 0
    path = Path(capsys.readouterr().out.strip().splitlines()[-1])
    assert path.exists() and json.loads(path.read_text())["database"] == ids.DB_TASKS
    assert run(device, "restore", str(path)) == 0
    assert len(device.db.find(ids.DB_TASKS).records) == 4
    with pytest.raises(SystemExit):
        run(device, "dump")
    with pytest.raises(SystemExit):
        run(device, "snapshot")
    with pytest.raises(SystemExit):
        run(device, "restore")

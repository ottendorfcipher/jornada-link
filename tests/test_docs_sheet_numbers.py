import json
from pathlib import Path

import pytest

from jornada.pim.models import Document
from jornada.sync.accounts import Account, AccountError
from jornada.sync.base import StoreError
from jornada.sync.documents import numbers
from jornada.sync.registry import BuildContext
from jornada.webapi.applescript import fake_runner


class NumbersRunner:
    """Plays Numbers: export writes a CSV, import writes a document, delete just records the call."""

    def __init__(self, exports=None, as_folder=False):
        self.exports = dict(exports or {})
        self.as_folder = as_folder
        self.calls = []

    def __call__(self, argv, stdin, timeout):
        script = stdin.decode("utf-8")
        args = list(argv[4:])
        self.calls.append((script, args))
        if "export(" in script:
            source, target = Path(args[0]), Path(args[1])
            csv = self.exports.get(source.name)
            if csv is None:
                return 0, b"{}", b""
            if self.as_folder:
                folder = target.with_suffix("")
                folder.mkdir()
                (folder / "Table 1.csv").write_text(csv, encoding="utf-8")
            else:
                target.write_bytes(csv.encode("utf-8"))
        elif "save(" in script:
            source, saved = Path(args[0]), Path(args[1])
            saved.write_bytes(b"NUMBERS:" + source.read_bytes())
        return 0, json.dumps({"ok": True}).encode("utf-8"), b""


@pytest.fixture
def folder(tmp_path):
    (tmp_path / "Budget.numbers").write_bytes(b"old")
    (tmp_path / "Tables.numbers").mkdir()  # a package-format document
    (tmp_path / "Notes.txt").write_text("not a sheet")
    (tmp_path / ".Hidden.numbers").write_bytes(b"x")
    return tmp_path


def test_list_exports_each_document_as_csv(folder):
    runner = NumbersRunner({"Budget.numbers": "\ufeffItem,Cost\r\nTea,3\r\n", "Tables.numbers": "a,b\n1,2\n\n"})
    store = numbers.NumbersStore(folder, runner=runner)
    items = store.list()
    assert [(i.id, i.record.name, i.record.text, i.record.kind) for i in items] == [
        ("Budget.numbers", "Budget", "Item,Cost\nTea,3\n", "sheet"), ("Tables.numbers", "Tables", "a,b\n1,2\n", "sheet")]
    assert all(i.version and i.record.modified is not None for i in items)
    script, args = runner.calls[0]
    assert "Application('Numbers')" in script and "as: 'CSV'" in script and "saving: 'no'" in script
    assert args[0] == str(folder / "Budget.numbers") and args[1].endswith("export.csv")
    assert not Path(args[1]).exists()  # scratch files are cleaned up


def test_list_handles_folder_exports_and_skips_failures(folder):
    runner = NumbersRunner({"Budget.numbers": "x,y\n"}, as_folder=True)
    log = []
    store = numbers.NumbersStore(folder, runner=runner, log=log.append)
    items = store.list()
    assert [i.id for i in items] == ["Budget.numbers"] and items[0].record.text == "x,y\n"
    assert any("skipping Tables.numbers" in line and "no CSV" in line for line in log)
    failing = numbers.NumbersStore(folder, runner=fake_runner([RuntimeError("Numbers got an error")]), log=log.append)
    assert failing.list() == () and any("Numbers got an error" in line for line in log)


def test_create_update_and_delete(folder):
    runner = NumbersRunner()
    store = numbers.NumbersStore(folder, runner=runner)
    name = store.create(Document("Budget", "Item,Cost\nTea,3\n", "sheet"))
    assert name == "Budget-2.numbers"
    assert (folder / name).read_bytes() == b"NUMBERS:Item,Cost\nTea,3\n"
    script, args = runner.calls[-1]
    assert "save({in: Path(target)})" in script and args[0].endswith("import.csv") and args[1].endswith("saved.numbers")
    assert store.update("Budget.numbers", Document("Budget", "a\n", "sheet")) is None
    assert (folder / "Budget.numbers").read_bytes() == b"NUMBERS:a\n"
    store.update("Tables.numbers", Document("Tables", "b\n", "sheet"))  # a package is replaced by the new file
    assert (folder / "Tables.numbers").is_file() and (folder / "Tables.numbers").read_bytes() == b"NUMBERS:b\n"
    store.delete("Budget-2.numbers")
    script, args = runner.calls[-1]
    assert "Application('Finder').delete" in script and args == [str(folder / "Budget-2.numbers")]
    assert store.create(Document("", "a\n", "sheet")) == "Untitled.numbers"


def test_bad_ids_and_failures_raise_store_errors(folder):
    store = numbers.NumbersStore(folder, runner=NumbersRunner())
    for bad in ("../Budget.numbers", "sub/Budget.numbers", "Notes.txt", ".Hidden.numbers"):
        with pytest.raises(StoreError):
            store.update(bad, Document("x", "a\n", "sheet"))
        with pytest.raises(StoreError):
            store.delete(bad)
    with pytest.raises(StoreError):
        store.delete("Missing.numbers")
    broken = numbers.NumbersStore(folder, runner=fake_runner([RuntimeError("no Numbers"), {"ok": True}]))
    with pytest.raises(StoreError):
        broken.create(Document("x", "a\n", "sheet"))
    with pytest.raises(StoreError):  # the script answered but produced no document
        broken.create(Document("x", "a\n", "sheet"))
    with pytest.raises(StoreError):
        numbers.NumbersStore(folder / "missing", runner=NumbersRunner()).list()


def test_backend_spec_and_build(folder, tmp_path):
    assert numbers.BACKEND.key == "numbers" and [s.key for s in numbers.BACKEND.settings] == ["folder"]
    runner = NumbersRunner()
    context = BuildContext(log=lambda _l: None, sync_dir=tmp_path, save_secrets=lambda _c: None, runner=runner)
    built = numbers.build(Account("a", "documents", "numbers", (("folder", str(folder)),)), {}, context)
    assert isinstance(built, numbers.NumbersStore)
    built.list()
    assert runner.calls  # the context's runner is the one used
    with pytest.raises(AccountError):
        numbers.build(Account("a", "documents", "numbers"), {}, context)
    with pytest.raises(AccountError):
        numbers.build(Account("a", "documents", "numbers", (("folder", str(folder / "nope")),)), {}, context)

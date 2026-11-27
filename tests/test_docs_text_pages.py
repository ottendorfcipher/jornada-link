from pathlib import Path

import pytest

from jornada.pim.models import Document
from jornada.sync.accounts import Account, AccountError
from jornada.sync.base import StoreError
from jornada.sync.documents.pages import BACKEND, PagesStore, build
from jornada.sync.registry import BuildContext
from jornada.webapi.applescript import fake_runner

OSASCRIPT = ("/usr/bin/osascript", "-l", "JavaScript", "-")


def exporting_runner(texts, calls):
    """Answers export scripts the way Pages would: by writing the text to the requested file."""

    def run(argv, stdin, timeout):
        calls.append((tuple(argv), stdin.decode("utf-8"), timeout))
        source, target = argv[4], argv[5]
        Path(target).write_text(texts[Path(source).name], encoding="utf-8")
        return 0, b'{"ok": true}', b""

    return run


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "Pages Docs"
    (root / "Alpha.pages").mkdir(parents=True)              # a package
    (root / "Alpha.pages" / "Index.iwa").write_bytes(b"\x00")
    (root / "Beta.pages").write_bytes(b"PK")                 # a single-file document
    (root / "notes.txt").write_text("not a pages file")
    (root / ".hidden.pages").write_bytes(b"")
    return root


def test_list_exports_each_document(folder):
    calls = []
    runner = exporting_runner({"Alpha.pages": "Alpha body\n\nsecond", "Beta.pages": "﻿Beta"}, calls)
    items = PagesStore(folder, runner=runner).list()
    assert [(i.id, i.record.name, i.record.text) for i in items] == [
        ("Alpha.pages", "Alpha", "Alpha body\n\nsecond"), ("Beta.pages", "Beta", "Beta")]
    assert all(i.version and i.record.modified is not None and i.record.kind == "text" for i in items)
    argv, script, _timeout = calls[0]
    assert argv[:4] == OSASCRIPT and argv[4] == str(folder / "Alpha.pages") and argv[5].endswith("export.txt")
    assert 'as: "unformatted text"' in script and 'close({saving: "no"})' in script
    assert not Path(argv[5]).exists()


def test_create_update_and_trash_pass_values_as_argv(folder):
    calls = []
    store = PagesStore(folder, runner=fake_runner([{"ok": True}, {"ok": True}, {"ok": True}], calls))
    text = 'Line "one"\n\tindented — café\n'
    assert store.create(Document("Alpha", text)) == "Alpha-2.pages"
    argv, script, _ = calls[0]
    assert argv[4:] == (str(folder / "Alpha-2.pages"), text)
    assert "pages.documents.push(doc)" in script and "doc.bodyText = argv[1]" in script and text not in script
    assert store.update("Beta.pages", Document("Beta", "changed")) is None
    assert calls[1][0][4:] == (str(folder / "Beta.pages"), "changed") and "doc.save()" in calls[1][1]
    store.delete("Beta.pages")
    assert calls[2][0][4:] == (str(folder / "Beta.pages"),) and 'Application("Finder").delete' in calls[2][1]
    assert (folder / "Beta.pages").exists()      # the Finder moves it to the Trash; nothing here unlinks


def test_create_makes_the_folder_and_bad_ids_are_refused(tmp_path):
    missing = tmp_path / "later"
    logs = []
    store = PagesStore(missing, runner=fake_runner([{"ok": True}]), log=logs.append)
    assert store.list() == () and any("does not exist" in line for line in logs)
    assert store.create(Document("Trip: notes/2026", "x")) == "Trip_ notes_2026.pages" and missing.is_dir()
    for bad in ("../x.pages", "sub/x.pages", "x.txt", "", ".x.pages", "nope.pages"):
        with pytest.raises(StoreError):
            store.update(bad, Document("x", "y"))
        with pytest.raises(StoreError):
            store.delete(bad)


def test_script_failures_become_store_errors(folder):
    store = PagesStore(folder, runner=fake_runner([RuntimeError("Pages got an error: not allowed")]))
    with pytest.raises(StoreError) as info:
        store.list()
    assert "not allowed" in str(info.value)
    silent = PagesStore(folder, runner=fake_runner([{"ok": True}]))       # an export that writes nothing
    with pytest.raises(StoreError):
        silent.list()


def test_build_and_spec(tmp_path):
    context = BuildContext(log=lambda _l: None, sync_dir=tmp_path, save_secrets=lambda _c: None,
                           runner=fake_runner([]))
    with pytest.raises(AccountError):
        build(Account("p", "documents", "pages"), {}, context)
    store = build(Account("p", "documents", "pages", (("path", "~/Docs/Jornada"),)), {}, context)
    assert store.folder == Path("~/Docs/Jornada").expanduser() and store.name == "pages"
    assert BACKEND.key == "pages" and BACKEND.login is None
    assert [s.key for s in BACKEND.settings] == ["path", "device_format"]
    assert BACKEND.missing_settings(Account("p", "documents", "pages"), {}) == ("path",)

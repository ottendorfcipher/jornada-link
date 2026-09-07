import os
from pathlib import Path

import pytest

from jornada.pim.models import Note
from jornada.sync.accounts import Account, AccountError
from jornada.sync.base import StoreError
from jornada.sync.notes import logseq
from jornada.sync.notes.logseq import LogseqStore, write_atomically
from jornada.sync.notes.logseq_names import (filename_to_journal_title, filename_to_page_name, has_tag,
                                             journal_title_to_filename, page_name_to_filename, with_tag)
from jornada.sync.registry import BuildContext


@pytest.fixture
def graph(tmp_path):
    root = tmp_path / "graph"
    (root / "pages").mkdir(parents=True)
    (root / "journals").mkdir()
    (root / "pages" / "Ideas.md").write_text("- one\n- two\n", encoding="utf-8")
    (root / "pages" / "work___Project%3A X.md").write_text("tags:: jornada\n- plan\n", encoding="utf-8")
    (root / "pages" / "notes.txt").write_text("not markdown", encoding="utf-8")
    (root / "journals" / "2026_09_06.md").write_text("- today #jornada\n", encoding="utf-8")
    (root / "journals" / "scratch.md").write_text("- not a journal\n", encoding="utf-8")
    return root


# -- names and tags --------------------------------------------------------------------
def test_page_name_encoding_round_trips():
    for name in ("Ideas", "a/b: c?", '100% #1 <x>|"q"*\\', "plain name", "2026-09-06", "ünïcödé (ok)"):
        assert filename_to_page_name(page_name_to_filename(name)) == name
    assert page_name_to_filename("a/b: c?") == "a___b%3A c%3F.md"
    assert page_name_to_filename("100% #1") == "100%25 %231.md"
    assert page_name_to_filename(" spaced ") == "spaced.md"
    assert filename_to_page_name("work___Project%3A X.md") == "work/Project: X"
    assert filename_to_page_name("nodotmd") == "nodotmd"
    with pytest.raises(ValueError):
        page_name_to_filename("   ")


def test_journal_names():
    assert journal_title_to_filename("2026-09-06") == "2026_09_06.md"
    assert filename_to_journal_title("2026_09_06.md") == "2026-09-06"
    assert filename_to_journal_title("Ideas.md") is None
    with pytest.raises(ValueError):
        journal_title_to_filename("Ideas")
    with pytest.raises(ValueError):
        journal_title_to_filename("2026-13-40")


def test_tag_detection_and_injection():
    assert has_tag("- hello #jornada", "jornada") and has_tag("#[[jornada]] hi", "#Jornada")
    assert has_tag("tags:: work, Jornada\n- x", "jornada") and has_tag("tags:: [[jornada]]", "jornada")
    assert not has_tag("#jornada/sub", "jornada") and not has_tag("#jornadas", "jornada")
    assert not has_tag("x#jornada", "jornada") and not has_tag("tags:: work\n- nothing", "jornada")
    assert has_tag("anything", "")
    assert with_tag("- body", "jornada") == "tags:: jornada\n- body"
    assert with_tag("tags:: a\n- body", "#jornada") == "tags:: a, jornada\n- body"
    assert with_tag("tags::\n- body", "jornada") == "tags:: jornada\n- body"
    assert with_tag("- has #jornada", "jornada") == "- has #jornada"
    assert with_tag("- body", "") == "- body"


# -- the store ---------------------------------------------------------------------------
def test_list_pages_and_journals(graph):
    lines = []
    items = LogseqStore(graph, log=lines.append).list()
    assert [item.id for item in items] == ["pages/Ideas.md", "pages/work___Project%3A X.md"]
    ideas = items[0].record
    assert ideas == Note("Ideas", "- one\n- two\n", folder="pages", modified=ideas.modified)
    assert ideas.modified is not None and items[0].version
    assert items[1].record.title == "work/Project: X"
    journals = LogseqStore(graph, kind="journals", log=lines.append).list()
    assert [(item.id, item.record.title, item.record.folder) for item in journals] == \
        [("journals/2026_09_06.md", "2026-09-06", "journals")]
    assert any("scratch.md" in line for line in lines) and not any("notes.txt" in line for line in lines)
    assert LogseqStore(graph, kind="journals").name == "logseq"


def test_tag_filter_and_tagging_of_written_pages(graph):
    tagged = LogseqStore(graph, tag="jornada")
    assert [item.id for item in tagged.list()] == ["pages/work___Project%3A X.md"]
    assert [item.id for item in LogseqStore(graph, kind="journals", tag="#jornada").list()] == ["journals/2026_09_06.md"]
    item_id = tagged.create(Note("Fresh", "- body"))
    assert (graph / "pages" / "Fresh.md").read_text(encoding="utf-8") == "tags:: jornada\n- body"
    assert item_id in [item.id for item in tagged.list()]          # stays visible to the filtered store
    tagged.update(item_id, Note("Fresh", "- edited"))
    assert (graph / "pages" / "Fresh.md").read_text(encoding="utf-8") == "tags:: jornada\n- edited"


def test_create_update_delete(graph):
    store = LogseqStore(graph)
    item_id = store.create(Note("Trip/Plan: 2026?", "- pack\r\n- go"))
    assert item_id == "pages/Trip___Plan%3A 2026%3F.md"
    target = graph / "pages" / "Trip___Plan%3A 2026%3F.md"
    assert target.read_text(encoding="utf-8") == "- pack\n- go"
    assert store.create(Note("Ideas", "- dup")) == "pages/Ideas-2.md"
    assert store.update(item_id, Note("Trip/Plan: 2026?", "- packed")) is None
    assert target.read_text(encoding="utf-8") == "- packed"
    store.delete(item_id)
    assert not target.exists()
    with pytest.raises(StoreError):
        store.delete(item_id)
    with pytest.raises(StoreError):
        store.update(item_id, Note("x", "y"))
    with pytest.raises(StoreError):
        store.create(Note("   ", "- untitled"))
    journals = LogseqStore(graph, kind="journals")
    assert journals.create(Note("2026-09-07", "- tomorrow")) == "journals/2026_09_07.md"
    with pytest.raises(StoreError) as info:
        journals.create(Note("Ideas", "- not a date"))
    assert "YYYY-MM-DD" in str(info.value)
    assert not list(graph.rglob(".jornada-*"))                    # no temp files left behind


def test_missing_folders_are_created_on_write_and_empty_on_list(tmp_path):
    root = tmp_path / "fresh"
    root.mkdir()
    store = LogseqStore(root, kind="journals")
    assert store.list() == ()
    assert store.create(Note("2026-01-01", "- new year")) == "journals/2026_01_01.md"
    assert (root / "journals" / "2026_01_01.md").read_text(encoding="utf-8") == "- new year"


def test_paths_outside_the_graph_are_refused(graph, tmp_path):
    store = LogseqStore(graph)
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    for bad in ("pages/../outside.md", "journals/2026_09_06.md", "../outside.md", "/etc/passwd", "pages/sub/x.md",
                "pages/..", "pages/x.txt", "pages\\x.md", "pages/", "Ideas.md"):
        with pytest.raises(StoreError):
            store.update(bad, Note("x", "y"))
        with pytest.raises(StoreError):
            store.delete(bad)
    (graph / "pages" / "link.md").symlink_to(outside)
    with pytest.raises(StoreError):
        store.update("pages/link.md", Note("x", "y"))
    with pytest.raises(StoreError):
        store.delete("pages/link.md")
    assert outside.read_text(encoding="utf-8") == "secret" and outside.exists()
    assert "pages/link.md" not in [item.id for item in store.list()]
    with pytest.raises(StoreError):
        LogseqStore(tmp_path / "missing")
    with pytest.raises(StoreError):
        LogseqStore(graph, kind="blocks")


def test_a_failed_write_leaves_the_page_intact(graph, monkeypatch):
    target = graph / "pages" / "Ideas.md"

    def failing_replace(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(logseq.os, "replace", failing_replace)
    with pytest.raises(StoreError) as info:
        LogseqStore(graph).update("pages/Ideas.md", Note("Ideas", "- lost"))
    assert "disk full" in str(info.value)
    assert target.read_text(encoding="utf-8") == "- one\n- two\n"
    assert not list(graph.rglob(".jornada-*"))


def test_writes_go_through_a_temp_file_and_a_rename(graph, monkeypatch):
    target = graph / "pages" / "Ideas.md"
    replaced = []
    real_replace = os.replace
    monkeypatch.setattr(logseq.os, "replace", lambda src, dst: replaced.append((src, dst)) or real_replace(src, dst))
    write_atomically(target, "- new\r\n- lines")
    assert target.read_text(encoding="utf-8") == "- new\n- lines"
    (source, destination), = replaced
    assert Path(source).parent == target.parent and Path(source).name.startswith(".jornada-") and destination == target
    with pytest.raises(StoreError):
        write_atomically(graph / "nowhere" / "x.md", "text")


def test_backend_build_and_settings(graph, tmp_path):
    context = BuildContext(log=lambda _line: None, sync_dir=tmp_path, save_secrets=lambda _c: None)
    account = Account("g", "notes", "logseq", (("graph", str(graph)), ("kind", "Journals"), ("tag", "jornada")))
    store = logseq.build(account, {}, context)
    assert store.directory == graph.resolve() / "journals"
    with pytest.raises(AccountError):
        logseq.build(Account("g", "notes", "logseq"), {}, context)
    with pytest.raises(AccountError):
        logseq.build(Account("g", "notes", "logseq", (("graph", str(graph / "nope")),)), {}, context)
    with pytest.raises(AccountError):
        logseq.build(Account("g", "notes", "logseq", (("graph", str(graph)), ("kind", "blocks"))), {}, context)
    assert logseq.BACKEND.missing_settings(Account("g", "notes", "logseq"), {}) == ("graph",)
    assert [s.key for s in logseq.BACKEND.settings] == ["graph", "kind", "tag"]

from pathlib import Path

import pytest

from jornada.pim.models import Note
from jornada.pim.timeconv import parse_iso, to_wall_clock
from jornada.sync.accounts import Account
from jornada.sync.base import StoreError
from jornada.sync.notes import apple
from jornada.sync.notes.apple import (CREATE_SCRIPT, DELETE_SCRIPT, LIST_SCRIPT, UPDATE_SCRIPT, AppleNotesStore,
                                      html_to_text, text_to_html)
from jornada.sync.notes.apple_html import html_to_lines, line_html
from jornada.sync.registry import BuildContext
from jornada.webapi.applescript import fake_runner

NOTE_ID = "x-coredata://1234-ABCD/ICNote/p42"
STAMP = "2026-09-06T10:00:00.000Z"


# -- HTML ⇄ text ------------------------------------------------------------------
def test_html_to_text_folds_blocks_breaks_and_entities():
    html = ('<div><h1>Shopping</h1></div>\n<div>Milk &amp; eggs</div>\n<div><br></div>\n'
            '<div>Tom &lt;tom@x.org&gt;&nbsp;&nbsp; &#8217;quoted&#8217;</div>\n<div>a<br>b</div>')
    assert html_to_text(html, "Shopping") == "Milk & eggs\n\nTom <tom@x.org>   ’quoted’\na\nb"
    assert html_to_lines("<p>one</p><p>two</p>") == ("one", "two")
    assert html_to_text("<div>x</div>\n\n<div>y</div>") == "x\ny"          # newlines between blocks are not content
    assert html_to_text("<div>line\n  wrapped</div>") == "line wrapped"    # newlines inside a block are spaces
    assert html_to_text("<div><b>bold</b> and <i>italic</i></div>") == "bold and italic"
    assert html_to_text("<div><br></div><div><br></div>") == "\n"
    assert html_to_text("<style>div {color: red}</style><div>seen</div>") == "seen"
    assert html_to_text("<table><tr><td>a</td><td>b</td></tr></table>") == "a\tb"
    assert html_to_text("") == "" and html_to_text("<div></div>") == ""


def test_html_to_text_lists_and_nested_divs():
    assert html_to_text("<div><div>nested</div><div><div>deeper</div></div></div>") == "nested\ndeeper"
    lists = "<ul><li>one</li><li>two<ul><li>deep</li></ul></li></ul><ol><li>first</li><li>second</li></ol>"
    assert html_to_text(lists) == "- one\n- two\n  - deep\n1. first\n2. second"
    checklist = '<ul class="Apple-checklist"><li>done</li></ul><div>after</div>'
    assert html_to_text(checklist) == "- done\nafter"


def test_html_to_text_drops_the_title_line_only_when_it_matches():
    assert html_to_text("<div><h1>Title</h1></div><div>body</div>", "Title") == "body"
    assert html_to_text("<div><h1>Title</h1></div><div>body</div>", "Other") == "Title\nbody"
    assert html_to_text("<div><h1>Title</h1></div><div>Title</div>", "Title") == "Title"   # only the first line
    assert html_to_text("<div><h1>Title</h1></div>", "Title") == ""
    assert html_to_text("<div><br></div><div>x</div>", "") == "\nx"                         # no title, nothing dropped


def test_text_to_html_and_round_trip():
    assert text_to_html("A & B", "") == "<div><h1>A &amp; B</h1></div>"
    html = text_to_html("Trip", "one <two>\n  indented  x\n\nlast\n")
    assert html == ("<div><h1>Trip</h1></div>\n<div>one &lt;two&gt;</div>\n<div>&nbsp;&nbsp;indented&nbsp;&nbsp;x</div>\n"
                    "<div><br></div>\n<div>last</div>\n<div><br></div>")
    assert html_to_text(html, "Trip") == "one <two>\n  indented  x\n\nlast\n"
    assert text_to_html("T", "a\r\nb\rc") == "<div><h1>T</h1></div>\n<div>a</div>\n<div>b</div>\n<div>c</div>"
    assert line_html(" a & b ") == "&nbsp;a &amp; b "     # leading space survives (the reader trims line ends)


# -- the store ------------------------------------------------------------------------
def test_list_sends_folder_and_account_and_maps_json_to_items():
    calls = []
    body = "<div><h1>Shopping</h1></div>\n<div>Milk</div>\n<div><br></div>\n<div>Eggs</div>"
    runner = fake_runner([[{"id": NOTE_ID, "name": "Shopping", "body": body, "modified": STAMP},
                           {"id": "p2", "name": "Empty", "body": "<div><h1>Empty</h1></div>", "modified": ""}]], calls)
    store = AppleNotesStore(folder="Jornada", account="iCloud", runner=runner)
    first, second = store.list()
    argv, stdin, _timeout = calls[0]
    assert argv[:4] == ("/usr/bin/osascript", "-l", "JavaScript", "-") and argv[4:] == ("Jornada", "iCloud")
    assert stdin == LIST_SCRIPT and "run(argv)" in stdin
    expected_modified = to_wall_clock(parse_iso(STAMP))
    assert first.id == NOTE_ID and first.version == STAMP
    assert first.record == Note("Shopping", "Milk\n\nEggs", folder="Jornada", modified=expected_modified, uid=NOTE_ID)
    assert second.record == Note("Empty", "", folder="Jornada", uid="p2") and second.version is None
    assert store.name == "apple-notes" and store.folder == "Jornada"


def test_create_update_delete_send_the_right_arguments():
    calls = []
    runner = fake_runner([{"id": NOTE_ID, "modified": STAMP}, {"id": NOTE_ID, "modified": "2026-09-07T10:00:00Z"},
                          {"id": NOTE_ID, "deleted": True}], calls)
    store = AppleNotesStore(runner=runner)
    assert store.create(Note("Shopping", "Milk\n\nBread")) == NOTE_ID
    argv, stdin, _ = calls[0]
    assert stdin == CREATE_SCRIPT and argv[4:] == ("Jornada", "", "Shopping", text_to_html("Shopping", "Milk\n\nBread"))
    assert store.update(NOTE_ID, Note("Shopping", "Milk")) == "2026-09-07T10:00:00Z"
    assert calls[1][1] == UPDATE_SCRIPT and calls[1][0][4:] == (NOTE_ID, text_to_html("Shopping", "Milk"))
    store.delete(NOTE_ID)
    assert calls[2][1] == DELETE_SCRIPT and calls[2][0][4:] == (NOTE_ID,)
    for script in (LIST_SCRIPT, CREATE_SCRIPT, UPDATE_SCRIPT, DELETE_SCRIPT):
        assert "JSON.stringify" in script and "Shopping" not in script   # user text never becomes script source


def test_failures_become_store_errors():
    runner = fake_runner([RuntimeError("Notes got an error: not allowed"), {"nope": 1}, "[1]", {"id": ""}])
    store = AppleNotesStore(runner=runner)
    with pytest.raises(StoreError) as info:
        store.list()
    assert "not allowed" in str(info.value) and "Apple Notes" in str(info.value)
    with pytest.raises(StoreError):
        store.list()                       # an object instead of a list
    with pytest.raises(StoreError):
        store.list()                       # an entry without an id
    with pytest.raises(StoreError):
        store.create(Note("x"))            # no id for the new note
    with pytest.raises(StoreError):
        store.update("", Note("x"))
    with pytest.raises(StoreError):
        store.delete("  ")


def test_deleting_a_note_that_is_already_gone_is_only_logged():
    lines = []
    store = AppleNotesStore(runner=fake_runner([{"id": NOTE_ID, "deleted": False}]), log=lines.append)
    store.delete(NOTE_ID)
    assert lines == [f"note {NOTE_ID} was already gone from Apple Notes"]


def test_backend_build_reads_settings_and_uses_the_context_runner(tmp_path):
    calls = []
    context = BuildContext(log=lambda _line: None, sync_dir=tmp_path, save_secrets=lambda _c: None,
                           runner=fake_runner([[]], calls))
    account = Account("n", "notes", "apple", (("notes_folder", "Handheld"), ("account", "iCloud")))
    store = apple.build(account, {}, context)
    assert store.list() == () and calls[0][0][4:] == ("Handheld", "iCloud")
    assert apple.build(Account("n", "notes", "apple"), {}, context).folder == "Jornada"
    assert apple.BACKEND.key == "apple" and apple.BACKEND.missing_settings(Account("n", "notes", "apple"), {}) == ()
    assert [s.key for s in apple.BACKEND.settings] == ["notes_folder", "account"]
    assert all(not s.required and not s.secret for s in apple.BACKEND.settings)

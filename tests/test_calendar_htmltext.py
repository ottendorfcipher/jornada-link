"""HTML → text for Microsoft Graph event bodies."""
from jornada.sync.calendar.htmltext import html_to_text

OUTLOOK_WEB = ('<html><head><meta http-equiv="Content-Type" content="text/html; charset=utf-8"><style type="text/css">'
               'p {margin: 0}</style></head><body><div class="BodyFragment"><div>Bring the &quot;card&quot; &amp; ID</div>'
               '<div>Room&nbsp;4 &#8212; 2nd floor</div></div></body></html>')
OUTLOOK_DESKTOP = ('<html><head><meta name="Generator" content="Microsoft Word 15"></head><body lang="EN-GB">'
                   '<div class="WordSection1"><p class="MsoNormal">first<o:p></o:p></p>'
                   '<p class="MsoNormal"><o:p>&nbsp;</o:p></p><p class="MsoNormal">after a blank line<o:p></o:p></p>'
                   '</div></body></html>')
EXCHANGE_TEXT = ('<html><head><meta http-equiv="Content-Type" content="text/html; charset=utf-8">'
                 '<meta content="text/html; charset=us-ascii"></head><body><font size="2"><span style="font-size:11pt;">'
                 '<div class="PlainText">a<br>b<br></div></span></font></body></html>')


def test_outlook_wrappers_are_stripped_and_entities_decoded():
    assert html_to_text(OUTLOOK_WEB) == 'Bring the "card" & ID\nRoom 4 — 2nd floor'
    assert html_to_text(OUTLOOK_DESKTOP) == "first\n\nafter a blank line"        # an empty paragraph survives
    assert html_to_text(EXCHANGE_TEXT) == "a\nb"                                   # a text body Exchange rewrote


def test_blocks_give_one_line_each_and_breaks_are_kept():
    assert html_to_text("<div>a<br>b<br/>c</div>") == "a\nb\nc"
    assert html_to_text("<p>one</p><p>two</p>") == "one\ntwo"
    assert html_to_text("<div>x</div>\n\n<div>y</div>") == "x\ny"                  # newlines between blocks are layout
    assert html_to_text("<div>line\n  wrapped   here</div>") == "line wrapped here"
    assert html_to_text("<div>x</div><div><br></div><div>y</div>") == "x\n\ny"
    assert html_to_text("<p>a</p><p></p><p></p><p></p><p>b</p>") == "a\n\nb"        # blank runs collapse
    assert html_to_text("<div><div>nested</div><div><div>deeper</div></div></div>") == "nested\ndeeper"
    assert html_to_text("<span>inline</span> <b>bold</b> and <i>italic</i>") == "inline bold and italic"
    assert html_to_text("<ul><li>one</li><li>two</li></ul><ol><li>three</li></ol>") == "one\ntwo\nthree"
    assert html_to_text("  <h1> Title </h1> body") == "Title\nbody"
    assert html_to_text("<p>a<p>b") == "a\nb"                                      # never closed


def test_pre_tables_and_skipped_elements():
    assert html_to_text("<div>x</div><pre>keep\nthese   lines</pre><div>y</div>") == "x\nkeep\nthese   lines\ny"
    assert html_to_text("<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>") == "a\tb\nc\td"
    assert html_to_text("<script>alert(1)</script>visible<style>p{}</style>") == "visible"
    assert html_to_text("<head><title>T</title><style>x</style></head>body") == "body"
    assert html_to_text("<html><head><meta charset=\"utf-8\"><body><div>seen</div>") == "seen"      # unclosed <head>
    assert html_to_text("</style>stray end tags</head></p>") == "stray end tags"


def test_empty_and_plain_inputs():
    assert html_to_text("") == "" and html_to_text(None) == "" and html_to_text("<div></div>") == ""
    assert html_to_text("no markup at all") == "no markup at all"
    assert html_to_text("a &lt; b &gt; c") == "a < b > c"

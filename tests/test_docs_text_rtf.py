import pytest

from jornada.sync.documents import rtf

ROUND_TRIPS = [
    "", "\n", "a", "a\n", "a\nb", "one\n\nthree\n", "tab\there\t", "a\t b",
    "braces {x} back\\slash", "caf\u00e9 \u2014 na\u00efve \u00df \u20ac \u2018quoted\u2019 \u00bfqu\u00e9?",
    "\u65e5\u672c\u8a9e text", "emoji \U0001F600 end",
]

WORD_LIKE = (
    b"{\\rtf1\\ansi\\ansicpg1252\\deff0\\deflang1033"
    b"{\\fonttbl{\\f0\\fswiss\\fcharset0 Arial;}{\\f1\\froman\\fcharset238 Times \\'e9;}}"
    b"{\\colortbl ;\\red0\\green0\\blue0;}{\\stylesheet{\\s0 Normal;}}"
    b"{\\*\\generator Riched20 10.0;}{\\info{\\title Secret}}"
    b"\\viewkind4\\uc1\\pard\\f0\\fs20 Hello\\tab world\\line next\\par\n"
    b"\\uc2\\u8212\\'97\\'97 dash \\uc1\\u26085? sun\\par\n"
    b"{\\field{\\*\\fldinst HYPERLINK \"x\"}{\\fldrslt link}} here \\{b\\} \\\\ \\~nb\\emdash\\par\n"
    b"raw \xe9 byte\\foo123 x\\bin3 abcdone\\par}"
)


@pytest.mark.parametrize("text", ROUND_TRIPS)
def test_round_trip(text):
    assert rtf.to_text(rtf.from_text(text)) == text


def test_line_endings_are_normalized():
    assert rtf.to_text(rtf.from_text("a\r\nb\rc")) == "a\nb\nc"


def test_writer_output_is_pocket_word_rtf():
    data = rtf.from_text("caf\u00e9 \u20ac \u65e5 \U0001F600 {x}\\\ty")
    assert data.startswith(b"{\\rtf1\\ansi\\ansicpg1252\\deff0{\\fonttbl{\\f0 Tahoma;}}\\f0\\fs20 ")
    assert data.endswith(b"\\par\n}")
    body = data.decode("ascii")
    assert "caf\\'e9" in body and "\\'80" in body and "\\u26085?" in body
    assert "\\u-10179?\\u-8704?" in body           # U+1F600 as a surrogate pair
    assert "\\{x\\}\\\\\\tab y" in body
    assert rtf.from_text("a\nb") == rtf.HEADER.encode("ascii") + b"a\\par\nb\\par\n}"


def test_reader_skips_tables_and_keeps_paragraph_text():
    expected = "Hello\tworld\nnext\n\u2014 dash \u65e5 sun\nlink here {b} \\ \u00a0nb\u2014\nraw \u00e9 bytexdone"
    assert rtf.to_text(WORD_LIKE) == expected


def test_reader_honours_code_pages():
    assert rtf.to_text(b"{\\rtf1\\ansi\\ansicpg1250\\deff0 \\'e8\\par}") == "\u010d"
    assert rtf.to_text(b"{\\rtf1\\mac \\'8e}") == "\u00e9"
    assert rtf.to_text(b"{\\rtf1\\ansi\\ansicpg99999 \\'e9}") == "\u00e9"      # unknown page → cp1252
    dbcs = b"".join(b"\\'%02x" % byte for byte in "\u65e5\u672c".encode("cp932"))
    assert rtf.to_text(b"{\\rtf1\\ansi\\ansicpg932 " + dbcs + b"}") == "\u65e5\u672c"


def test_reader_unicode_fallbacks():
    assert rtf.to_text(b"{\\rtf1\\uc1 \\u8364? x}") == "\u20ac x"
    assert rtf.to_text(b"{\\rtf1\\uc2 \\u8364\\'80\\'80 x}") == "\u20ac x"
    assert rtf.to_text(b"{\\rtf1\\uc0 \\u8364 x}") == "\u20acx"                # the space is the delimiter
    assert rtf.to_text(b"{\\rtf1 \\u-10179?\\u-8704? done}") == "\U0001F600 done"
    assert rtf.to_text(b"{\\rtf1 {\\uc2 \\u233??}\\u233?}") == "\u00e9\u00e9"     # \uc is per group


def test_reader_is_tolerant():
    with pytest.raises(ValueError):
        rtf.to_text(b"plain text")
    assert rtf.to_text(b"  \xef\xbb\xbf{\\rtf1 bom ok}") == "bom ok"
    assert rtf.to_text(b"{\\rtf1 truncated \\par") == "truncated "
    assert rtf.to_text(b"{\\rtf1 bad \\'zz hex}") == "bad zz hex"
    assert rtf.to_text(b"{\\rtf1 a\\\nb}") == "a\nb"                            # backslash-newline is \par
    assert rtf.to_text(b"{\\rtf1 a\\par\\par b}") == "a\n\nb"
    assert rtf.to_text(b"{\\rtf1 {\\pntext 1.\\tab}item\\par}") == "item"
    assert rtf.to_text(b"{\\rtf1 a\\cell b\\cell\\row}") == "a\tb\t"
    assert rtf.to_text(b"{\\rtf1 }}}}") == ""

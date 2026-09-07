import datetime as dt

import pytest

from jornada.webapi import vcard
from jornada.webapi.ical import Property


def crlf(text: str) -> str:
    return text.replace("\n", "\r\n")


# Pocket Outlook / Outlook 97 style export: bare parameters, quoted-printable UTF-8 with soft breaks.
V21 = crlf("""BEGIN:VCARD
VERSION:2.1
N;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:M=C3=BCller;J=C3=BCrgen;;Dr.;
FN;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:Dr. J=C3=BCrgen M=C3=
=BCller
ORG:Beispiel GmbH;Vertrieb
TITLE;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:Gesch=C3=A4ftsf=C3=BChrer
TEL;WORK;VOICE:+49 89 123456
TEL;CELL:+49 170 1234567
TEL;HOME;FAX:+49 89 654321
EMAIL;PREF;INTERNET:jm@example.de
ADR;WORK;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:;;Hauptstra=C3=9Fe 1;M=C3=BCnchen;;80331;Germany
NOTE;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:Erste Zeile=0D=0AZweite Zeile mit =C3=
=9C und=20=
Ende
BDAY:1970-05-17
X-JORNADA-OID:1234
END:VCARD
""")

# Apple Contacts / iCloud style: item groups, lower-cased type params, base64 photo.
V30 = crlf("""BEGIN:VCARD
VERSION:3.0
PRODID:-//Apple Inc.//iOS 17.4//EN
N:Doe\\;Jr.;John;Quincy;Dr.;Ph.D.
FN:Dr. John Doe
NICKNAME:JD,Johnny
ORG:ACME\\, Inc.;Sales
TITLE:Head of Sales
item1.EMAIL;type=INTERNET;type=pref:john@example.com
item1.X-ABLabel:_$!<Other>!$_
EMAIL;type=INTERNET;type=WORK:john@acme.example
TEL;type=CELL;type=VOICE;type=pref:+1 (555) 010-0100
TEL;type=WORK;type=FAX:+1 555 010 0101
ADR;type=HOME;type=pref:;;1 Infinite Loop\\, Bldg 2;Cupertino;CA;95014;USA
item2.X-ABDATE;type=pref:2010-06-05
item2.X-ABLabel:_$!<Anniversary>!$_
BDAY:1990-01-31
CATEGORIES:Friends,Work\\,Play
NOTE:Line 1\\nLine 2\\, with a comma; and a semicolon
URL;type=pref:https://example.com/john?a=1,2
PHOTO;ENCODING=b;TYPE=JPEG:/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsL
 DBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgN
 DRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/
UID:9B5E2A70-1C3F-4D8A-9E2B-5F6A7B8C9D0E
REV:2026-03-01T12:00:00Z
END:VCARD
""")

# Nextcloud / Fastmail style vCard 4.0.
V40 = crlf("""BEGIN:VCARD
VERSION:4.0
UID:urn:uuid:4fbe8971-0bc3-424c-9c26-36c3e1eff6b1
KIND:individual
N:Nakamura;Yuki;;;
FN:Yuki Nakamura
GENDER:F
TEL;TYPE="work,voice";VALUE=uri:tel:+81-3-1234-5678
TEL;TYPE=cell;PREF=1:+81 90 1234 5678
EMAIL;TYPE=work;PREF=1:yuki@example.jp
ADR;TYPE=work;LABEL="Tokyo Tower\\n4-2-8 Shibakoen":;;4-2-8 Shibakoen;Minato-ku;Tokyo;105-0011;Japan
ANNIVERSARY:20100605
BDAY:--0131
ORG:例のカンパニー;開発部
CATEGORIES:Colleague
END:VCARD
""")


# --- parsing -------------------------------------------------------------------------------

def test_vcard21_bare_params_and_quoted_printable():
    (card,) = vcard.parse(V21)
    assert card.version == "2.1"
    assert vcard.name_parts(card) == ("Müller", "Jürgen", "", "Dr.", "")
    assert vcard.formatted_name(card) == "Dr. Jürgen Müller"  # soft line break joined
    assert card.value("TITLE") == "Geschäftsführer"
    assert vcard.telephones(card) == (
        ("+49 89 123456", ("work", "voice")),
        ("+49 170 1234567", ("cell",)),
        ("+49 89 654321", ("home", "fax")),
    )
    assert vcard.emails(card) == (("jm@example.de", ("pref", "internet")),)
    assert vcard.addresses(card) == ((("", "", "Hauptstraße 1", "München", "", "80331", "Germany"), ("work",)),)
    assert card.value("NOTE") == "Erste Zeile\r\nZweite Zeile mit Ü und Ende"
    assert vcard.birthday(card) == dt.date(1970, 5, 17)
    assert vcard.org_parts(card) == ("Beispiel GmbH", "Vertrieb")
    assert card.value("X-JORNADA-OID") == "1234"
    # transport parameters are consumed, not carried into the model
    assert card.get("N").params == ()
    assert card.get("ADR").params == (("TYPE", "WORK"),)


def test_vcard30_groups_types_and_readers():
    (card,) = vcard.parse(V30)
    assert card.version == "3.0"
    assert vcard.name_parts(card) == ("Doe;Jr.", "John", "Quincy", "Dr.", "Ph.D.")
    assert vcard.formatted_name(card) == "Dr. John Doe"
    assert card.get("EMAIL").group == "item1"
    assert [p.value for p in card.get_all("X-ABLABEL")] == ["_$!<Other>!$_", "_$!<Anniversary>!$_"]
    assert vcard.emails(card) == (
        ("john@example.com", ("internet", "pref")),
        ("john@acme.example", ("internet", "work")),
    )
    assert vcard.telephones(card) == (
        ("+1 (555) 010-0100", ("cell", "voice", "pref")),
        ("+1 555 010 0101", ("work", "fax")),
    )
    assert vcard.addresses(card) == ((("", "", "1 Infinite Loop, Bldg 2", "Cupertino", "CA", "95014", "USA"), ("home", "pref")),)
    assert vcard.org_parts(card) == ("ACME, Inc.", "Sales")
    assert vcard.split_list(card.value("NICKNAME")) == ("JD", "Johnny")
    assert vcard.split_list(card.value("CATEGORIES")) == ("Friends", "Work,Play")
    assert card.value("NOTE") == "Line 1\nLine 2, with a comma; and a semicolon"
    assert card.value("URL") == "https://example.com/john?a=1,2"
    assert vcard.birthday(card) == dt.date(1990, 1, 31)
    assert vcard.anniversary(card) == dt.date(2010, 6, 5)  # Apple X-ABDATE + label
    photo = card.value("PHOTO")
    assert photo.startswith("/9j/4AAQ") and "\r" not in photo and " " not in photo
    assert card.value("UID") == "9B5E2A70-1C3F-4D8A-9E2B-5F6A7B8C9D0E"


def test_vcard40_type_lists_pref_and_dates():
    (card,) = vcard.parse(V40)
    assert card.version == "4.0"
    assert vcard.name_parts(card) == ("Nakamura", "Yuki", "", "", "")
    assert vcard.telephones(card) == (
        ("tel:+81-3-1234-5678", ("work", "voice")),
        ("+81 90 1234 5678", ("cell", "pref")),
    )
    assert vcard.emails(card) == (("yuki@example.jp", ("work", "pref")),)
    assert vcard.addresses(card)[0][0] == ("", "", "4-2-8 Shibakoen", "Minato-ku", "Tokyo", "105-0011", "Japan")
    assert card.get("ADR").param("LABEL") == "Tokyo Tower\\n4-2-8 Shibakoen"
    assert vcard.anniversary(card) == dt.date(2010, 6, 5)
    assert vcard.birthday(card) is None  # year-less --0131
    assert vcard.org_parts(card) == ("例のカンパニー", "開発部")
    assert card.value("KIND") == "individual"


def test_multiple_cards_and_nested_agent_card():
    cards = vcard.parse(V30 + V40 + V21)
    assert [vcard.formatted_name(c) for c in cards] == ["Dr. John Doe", "Yuki Nakamura", "Dr. Jürgen Müller"]
    nested = crlf("""BEGIN:VCARD
VERSION:2.1
N:Boss;Big
AGENT:
BEGIN:VCARD
VERSION:2.1
N:Assistant;Ann
TEL;WORK:+1 555
END:VCARD
TEL;HOME:+1 666
END:VCARD
""")
    (card,) = vcard.parse(nested)
    assert vcard.name_parts(card)[:2] == ("Boss", "Big")
    assert vcard.telephones(card) == (("+1 666", ("home",)),)
    assert vcard.parse("") == ()


@pytest.mark.parametrize("text", [
    "END:VCARD\n",
    "BEGIN:VCARD\nVERSION:3.0\nFN:Nope\n",
    "FN:Outside\n",
    "BEGIN:VCARD\nthis has no colon\nEND:VCARD\n",
])
def test_malformed_text_raises_vcard_error(text):
    with pytest.raises(vcard.VCardError):
        vcard.parse(text)


# --- structured values --------------------------------------------------------------------------

def test_split_and_join_structured_values():
    assert vcard.split_structured("Doe\\;Jr.;John;;;") == ("Doe;Jr.", "John", "", "", "")
    assert vcard.join_structured(("a;b", "c,d", "e\\f", "g\nh")) == "a\\;b;c\\,d;e\\\\f;g\\nh"
    parts = ("Doe;Jr.", "John", "Q,R", "", "")
    assert vcard.split_structured(vcard.join_structured(parts)) == parts
    assert vcard.split_list("A\\,B, C ,,D") == ("A,B", "C", "D")
    assert vcard.join_list((" A,B ", "", "C")) == "A\\,B,C"
    assert vcard.split_list(vcard.join_list(("x,y", "z"))) == ("x,y", "z")


def test_readers_on_sparse_cards():
    empty = vcard.VCard()
    assert empty.version == "3.0"
    assert vcard.name_parts(empty) == ("", "", "", "", "")
    assert vcard.formatted_name(empty) == ""
    assert vcard.org_parts(empty) == ()
    assert vcard.telephones(empty) == () and vcard.emails(empty) == () and vcard.addresses(empty) == ()
    assert vcard.birthday(empty) is None and vcard.anniversary(empty) is None
    (n_only,) = vcard.parse("BEGIN:VCARD\nN:Doe;John;Q.;Dr.;Jr.\nEND:VCARD\n")
    assert vcard.formatted_name(n_only) == "Dr. John Q. Doe Jr."
    (partial,) = vcard.parse("BEGIN:VCARD\nN:Solo\nADR:;;Street\nX-ANNIVERSARY:1999-12-31\nBDAY:garbage\nEND:VCARD\n")
    assert vcard.name_parts(partial) == ("Solo", "", "", "", "")
    assert vcard.addresses(partial) == ((("", "", "Street", "", "", "", ""), ()),)
    assert vcard.anniversary(partial) == dt.date(1999, 12, 31)
    assert vcard.birthday(partial) is None
    assert vcard.parse_vcard_date("19990230") is None
    assert vcard.parse_vcard_date("1999-01-31T00:00:00") == dt.date(1999, 1, 31)


# --- serialization and the builder ----------------------------------------------------------------

def test_builder_round_trip_in_30_and_40():
    built = vcard.make_vcard(
        "uid-1", family="O'Neil;Jr", given="Ada", additional="B.", prefix="Ms.", suffix="PhD",
        org=("Analytical, Engines", "R&D"), title="Engineer",
        tels=(("+44 20 7946 0000", ("work", "voice")), ("+44 7700 900000", ("cell",))),
        emails=(("ada@example.org", ("home", "pref")),),
        adrs=(((("", "", "12 St James's Sq", "London", "", "SW1Y 4LB", "UK")), ("home",)),),
        bday=dt.date(1815, 12, 10), anniversary="1835-07-08", note="First\nSecond, with; punctuation",
        categories=("Science", "History,Math"), url="https://example.org/ada?x=1,2", nickname="Countess",
        extra=(Property("X-JORNADA-OID", "77"),),
    )
    assert built.value("FN") == "Ms. Ada B. O'Neil;Jr PhD"
    for version in ("3.0", "4.0"):
        text = vcard.serialize(built, version)
        assert text.startswith("BEGIN:VCARD\r\nVERSION:" + version + "\r\n") and text.endswith("END:VCARD\r\n")
        (back,) = vcard.parse(text)
        assert back.version == version
        if version == "3.0":
            assert back.without_property("VERSION") == built
        assert vcard.name_parts(back) == ("O'Neil;Jr", "Ada", "B.", "Ms.", "PhD")
        assert vcard.org_parts(back) == ("Analytical, Engines", "R&D")
        assert vcard.telephones(back) == (("+44 20 7946 0000", ("work", "voice")), ("+44 7700 900000", ("cell",)))
        assert vcard.emails(back) == (("ada@example.org", ("home", "pref")),)
        assert vcard.addresses(back) == ((("", "", "12 St James's Sq", "London", "", "SW1Y 4LB", "UK"), ("home",)),)
        assert vcard.birthday(back) == dt.date(1815, 12, 10)
        assert vcard.anniversary(back) == dt.date(1835, 7, 8)
        assert back.value("NOTE") == "First\nSecond, with; punctuation"
        assert vcard.split_list(back.value("CATEGORIES")) == ("Science", "History,Math")
        assert back.value("URL") == "https://example.org/ada?x=1,2"
        assert back.value("X-JORNADA-OID") == "77"
    assert "TEL;TYPE=WORK,VOICE:+44 20 7946 0000" in vcard.serialize(built, "3.0")
    assert "TEL;TYPE=work,voice:+44 20 7946 0000" in vcard.serialize(built, "4.0")
    assert "NOTE:First\\nSecond\\, with\\; punctuation" in vcard.serialize(built)
    assert "ORG:Analytical\\, Engines;R&D" in vcard.serialize(built)


def test_builder_fallbacks_and_validation():
    assert vcard.make_vcard("u-2", fn="Given Name").value("FN") == "Given Name"
    assert vcard.make_vcard("u-3", org=("Org Only",)).value("FN") == "Org Only"
    assert vcard.make_vcard("u-4").value("FN") == "u-4"
    assert vcard.make_vcard("u-5", adrs=((("a", "b"), ()),)).value("ADR") == "a;b;;;;;"
    assert vcard.make_vcard("", given="Anon").get("UID") is None
    with pytest.raises(vcard.VCardError):
        vcard.make_vcard("u", tels=("+1 555",))  # not (number, types) pairs
    with pytest.raises(vcard.VCardError):
        vcard.make_vcard("u", bday="last spring")
    with pytest.raises(vcard.VCardError):
        vcard.serialize(vcard.VCard(), "5.0")


def test_serialize_folds_long_multibyte_lines():
    note = "Zusammenfassung mit Umlauten äöü und Emoji 😀 " * 6
    text = vcard.serialize(vcard.make_vcard("u", given="Ü", note=note))
    for physical in text.split("\r\n"):
        assert len(physical.encode("utf-8")) <= 75
    (back,) = vcard.parse(text)
    assert back.value("NOTE") == note


def test_vcard_with_and_without_property():
    (card,) = vcard.parse(V40)
    renamed = card.with_property(Property("fn", "Y. Nakamura"))
    assert renamed.value("FN") == "Y. Nakamura" and len(renamed.get_all("FN")) == 1
    assert card.value("FN") == "Yuki Nakamura"
    extra = card.with_property(Property("EMAIL", "second@example.jp"), replace=False)
    assert len(extra.get_all("EMAIL")) == 2
    assert card.without_property("tel").get_all("TEL") == ()
    assert card.value("NOPE", "dflt") == "dflt"


def test_quoted_printable_charset_fallbacks():
    bogus = "BEGIN:VCARD\r\nVERSION:2.1\r\nN;CHARSET=X-NO-SUCH-CHARSET;ENCODING=QUOTED-PRINTABLE:Andr=E9;;;;\r\nEND:VCARD\r\n"
    (card,) = vcard.parse(bogus)
    assert vcard.name_parts(card)[0] == "André"  # unknown charset, invalid UTF-8 -> latin-1
    latin = "BEGIN:VCARD\r\nVERSION:2.1\r\nFN;CHARSET=ISO-8859-1;ENCODING=QUOTED-PRINTABLE:Ren=E9e\r\nEND:VCARD\r\n"
    assert vcard.formatted_name(vcard.parse(latin)[0]) == "Renée"
    dangling = "BEGIN:VCARD\r\nVERSION:2.1\r\nNOTE;ENCODING=QUOTED-PRINTABLE:abc=\r\n"
    with pytest.raises(vcard.VCardError):
        vcard.parse(dangling)

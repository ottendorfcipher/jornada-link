import datetime as dt
from dataclasses import replace

import pytest

from jornada.pim.models import Address, Contact
from jornada.sync.contacts.vcardmap import address_kind, from_vcard, phone_kind, phone_number, to_vcard
from jornada.webapi import vcard


def crlf(text):
    return text.replace("\n", "\r\n")


def card_of(text):
    (card,) = vcard.parse(crlf(text))
    return card


def round_trip(contact, uid="uid-1"):
    return from_vcard(vcard.parse(vcard.serialize(to_vcard(contact, uid)))[0])


FULL = Contact(
    first_name="Ada", last_name="Lovelace", middle_name="King", title="Countess", suffix="PhD",
    full_name="Ada King Lovelace", company="Analytical", job_title="Mathematician", department="R&D", office="Room 1",
    emails=("a@x.org", "b@x.org", "c@x.org"),
    phones=(("work", "1"), ("work2", "2"), ("home", "3"), ("home2", "4"), ("mobile", "5"), ("work_fax", "6"),
            ("home_fax", "7"), ("pager", "8"), ("car", "9"), ("radio", "10"), ("assistant", "11")),
    addresses=(Address("home", "1 Rd", "Town", "ST", "123", "UK"), Address("work", "HQ", "City"),
               Address("other", "Elsewhere")),
    birthday=dt.date(1815, 12, 10), anniversary=dt.date(1835, 7, 8), spouse="William", children="Byron, Annabella",
    assistant="Charles", web_page="https://x.org", notes="line 1\nline 2, with; punctuation",
    categories=("Friends", "Math"),
)

ICLOUD = """BEGIN:VCARD
VERSION:3.0
PRODID:-//Apple Inc.//iOS 17.4//EN
N:Doe;John;Quincy;Dr.;Ph.D.
FN:Dr. John Doe
NICKNAME:JD
ORG:ACME\\, Inc.;Sales
TITLE:Head of Sales
item1.EMAIL;type=INTERNET;type=pref:john@example.com
item1.X-ABLabel:_$!<Other>!$_
EMAIL;type=INTERNET;type=WORK:john@acme.example
TEL;type=CELL;type=VOICE;type=pref:+1 (555) 010-0100
TEL;type=WORK;type=FAX:+1 555 010 0101
TEL;type=HOME;type=VOICE:+1 555 010 0102
TEL;type=HOME;type=VOICE:+1 555 010 0103
ADR;type=HOME;type=pref:;;1 Infinite Loop\\, Bldg 2;Cupertino;CA;95014;USA
item2.X-ABDATE;type=pref:2010-06-05
item2.X-ABLabel:_$!<Anniversary>!$_
BDAY:1990-01-31
CATEGORIES:Friends,Work\\,Play
NOTE:Line 1\\nLine 2
URL;type=pref:https://example.com/john
PHOTO;ENCODING=b;TYPE=JPEG:/9j/4AAQSkZJRgABAQAAAQABAAD
UID:9B5E2A70-1C3F-4D8A-9E2B-5F6A7B8C9D0E
REV:2026-03-01T12:00:00Z
END:VCARD
"""

GOOGLE_EXPORT = """BEGIN:VCARD
VERSION:3.0
FN:Yuki Nakamura
N:Nakamura;Yuki;;;
EMAIL;TYPE=INTERNET;TYPE=HOME:yuki@example.jp
EMAIL;TYPE=INTERNET;TYPE=WORK;TYPE=PREF:yuki@work.example
TEL;TYPE=CELL:+81 90 1234 5678
TEL;TYPE=WORK:+81 3 1234 5678
TEL;TYPE=WORK:+81 3 1234 5679
TEL;TYPE=WORK:+81 3 1234 5680
TEL;TYPE=PAGER:+81 3 0000
TEL;TYPE=CAR:+81 3 1111
TEL;TYPE=X-RADIO:+81 3 2222
TEL;TYPE=X-ASSISTANT:+81 3 3333
ADR;TYPE=WORK:PO 1;Floor 2;4-2-8 Shibakoen;Minato-ku;Tokyo;105-0011;Japan
ADR;TYPE=WORK:;;Second office;Osaka;;;Japan
ADR;TYPE=WORK:;;Third office;Kobe;;;Japan
X-EVOLUTION-SPOUSE:Kenji
X-EVOLUTION-ANNIVERSARY:2010-06-05
X-EVOLUTION-ASSISTANT:Aiko
X-CHILDREN:Hana
X-CHILDREN:Ren,Sora
X-ANDROID-CUSTOM:vnd.android.cursor.item/nickname;Yu;;;;;;;;;;;;;;
CATEGORIES:myContacts
END:VCARD
"""

NEXTCLOUD = """BEGIN:VCARD
VERSION:4.0
UID:urn:uuid:4fbe8971-0bc3-424c-9c26-36c3e1eff6b1
KIND:individual
N:Nakamura;Yuki;;;
FN:Yuki Nakamura
TEL;TYPE="work,voice";VALUE=uri:tel:+81-3-1234-5678
TEL;TYPE=cell;PREF=1:+81 90 1234 5678
TEL;TYPE="home,fax":+81 3 9999
EMAIL;TYPE=work;PREF=1:yuki@example.jp
ADR;TYPE=work;LABEL="Tokyo Tower\\n4-2-8 Shibakoen":;;4-2-8 Shibakoen;Minato-ku;Tokyo;105-0011;Japan
ANNIVERSARY:20100605
BDAY:--0131
ORG:例のカンパニー;開発部
CATEGORIES:Colleague
X-SPOUSE:Kenji
END:VCARD
"""

# Pocket Outlook / Outlook 97 style export: bare parameters, quoted-printable UTF-8 with a soft break.
POCKET_OUTLOOK = """BEGIN:VCARD
VERSION:2.1
N;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:M=C3=BCller;J=C3=BCrgen;;Dr.;
FN;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:Dr. J=C3=BCrgen M=C3=
=BCller
ORG:Beispiel GmbH;Vertrieb
TITLE;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:Gesch=C3=A4ftsf=C3=BChrer
TEL;WORK;VOICE:+49 89 123456
TEL;CELL:+49 170 1234567
TEL;HOME;FAX:+49 89 654321
TEL;WORK;VOICE:+49 89 123457
EMAIL;PREF;INTERNET:jm@example.de
ADR;WORK;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:;;Hauptstra=C3=9Fe 1;M=C3=BCnchen;;80331;Germany
NOTE;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:Erste Zeile=0D=0AZweite Zeile
BDAY:1970-05-17
END:VCARD
"""


# -- writing and the full round trip ---------------------------------------------------------

def test_full_round_trip_keeps_every_field():
    text = vcard.serialize(to_vcard(FULL, "uid-1"))
    assert text.startswith("BEGIN:VCARD\r\nVERSION:3.0\r\nUID:uid-1\r\n")
    for line in ("N:Lovelace;Ada;King;Countess;PhD", "FN:Ada King Lovelace", "ORG:Analytical;R&D;Room 1",
                 "TITLE:Mathematician", "TEL;TYPE=WORK,VOICE:1", "TEL;TYPE=WORK,VOICE:2", "TEL;TYPE=HOME,VOICE:3",
                 "TEL;TYPE=HOME,VOICE:4", "TEL;TYPE=CELL:5", "TEL;TYPE=WORK,FAX:6", "TEL;TYPE=HOME,FAX:7",
                 "TEL;TYPE=PAGER:8", "TEL;TYPE=CAR:9", "TEL;TYPE=X-RADIO:10", "TEL;TYPE=X-ASSISTANT:11",
                 "EMAIL;TYPE=INTERNET,PREF:a@x.org", "EMAIL;TYPE=INTERNET:b@x.org", "EMAIL;TYPE=INTERNET:c@x.org",
                 "ADR;TYPE=HOME:;;1 Rd;Town;ST;123;UK", "ADR;TYPE=WORK:;;HQ;City;;;", "ADR;TYPE=OTHER:;;Elsewhere;;;;",
                 "BDAY:18151210", "ANNIVERSARY:18350708", "X-ANNIVERSARY:18350708", "CATEGORIES:Friends,Math",
                 "NOTE:line 1\\nline 2\\, with\\; punctuation", "URL:https://x.org", "X-SPOUSE:William",
                 "X-CHILDREN:Byron\\, Annabella", "X-ASSISTANT:Charles"):
        assert f"\r\n{line}\r\n" in text, line
    assert round_trip(FULL) == replace(FULL, uid="uid-1")
    assert round_trip(replace(FULL, full_name="")) == replace(FULL, uid="uid-1")   # FN fills full_name, like the device
    (card,) = vcard.parse(vcard.serialize(to_vcard(FULL, "u"), "4.0"))
    assert from_vcard(card) == replace(FULL, uid="u")


def test_phones_are_written_in_slot_order_so_a_second_work_number_follows_the_first():
    shuffled = Contact(first_name="A", phones=(("mobile", "5"), ("work2", "2"), ("home", "3"), ("work", "1")))
    text = vcard.serialize(to_vcard(shuffled, "u"))
    tels = [line for line in text.split("\r\n") if line.startswith("TEL")]
    assert tels == ["TEL;TYPE=WORK,VOICE:1", "TEL;TYPE=WORK,VOICE:2", "TEL;TYPE=HOME,VOICE:3", "TEL;TYPE=CELL:5"]
    assert round_trip(shuffled).phones == (("work", "1"), ("work2", "2"), ("home", "3"), ("mobile", "5"))
    assert round_trip(shuffled).full_name == "A"


def test_minimal_and_empty_contacts():
    assert vcard.serialize(to_vcard(Contact(), "only-uid")).count("FN:only-uid") == 1
    company = to_vcard(Contact(company="ACME"), "u")
    assert company.value("FN") == "ACME" and company.value("ORG") == "ACME" and company.value("N") == ";;;;"
    assert to_vcard(Contact(first_name="A", department="Sales"), "u").value("ORG") == ";Sales"
    assert round_trip(Contact(company="ACME")) == Contact(full_name="ACME", company="ACME", uid="uid-1")
    assert round_trip(Contact(emails=("e@x",))) == Contact(full_name="e@x", emails=("e@x",), uid="uid-1")
    sparse = from_vcard(card_of("BEGIN:VCARD\nN:Solo\nEND:VCARD\n"))
    assert sparse == Contact(last_name="Solo", full_name="Solo")
    assert from_vcard(vcard.VCard()) == Contact()


def test_full_name_is_always_filled_like_the_device_record():
    custom = Contact(first_name="Bob", last_name="Smith", full_name="Bob 'Hawk' Smith")
    card = to_vcard(custom, "u")
    assert card.value("FN") == "Bob 'Hawk' Smith"
    assert round_trip(custom) == replace(custom, uid="uid-1")
    plain = from_vcard(card_of("BEGIN:VCARD\nN:Smith;Bob;;;\nFN:Bob Smith\nEND:VCARD\n"))
    assert plain.full_name == "Bob Smith" and plain.display_name() == "Bob Smith"
    assembled = from_vcard(card_of("BEGIN:VCARD\nN:Smith;Bob;J.;Mr.;\nEND:VCARD\n"))
    assert assembled.full_name == "Bob J. Smith"        # no FN: the assembled name, as the device would store it


# -- reading: phone and address kinds ------------------------------------------------------------

@pytest.mark.parametrize("types,kind", [
    (("cell",), "mobile"), (("cell", "voice", "pref"), "mobile"), (("home", "cell"), "mobile"),
    (("work", "fax"), "work_fax"), (("fax",), "work_fax"), (("home", "fax"), "home_fax"),
    (("pager",), "pager"), (("car",), "car"), (("x-radio",), "radio"), (("x-assistant",), "assistant"),
    (("home", "voice"), "home"), (("home",), "home"), (("work", "voice"), "work"), (("work",), "work"),
    (("voice",), "work"), ((), "work"), (("internet",), "work"),
])
def test_phone_kind_from_type_values(types, kind):
    assert phone_kind(types) == kind


def test_second_numbers_overflow_and_a_third_keeps_its_kind():
    card = card_of("BEGIN:VCARD\nN:X\nTEL;TYPE=WORK:1\nTEL;TYPE=HOME:3\nTEL;TYPE=WORK:2\nTEL;TYPE=HOME:4\n"
                   "TEL;TYPE=WORK:9\nTEL;TYPE=CELL:5\nTEL;TYPE=CELL:55\nTEL:\nEND:VCARD\n")
    assert from_vcard(card).phones == (("work", "1"), ("work", "9"), ("work2", "2"), ("home", "3"), ("home2", "4"),
                                       ("mobile", "5"), ("mobile", "55"))
    assert phone_number("tel:+81-3-1234-5678") == "+81-3-1234-5678" and phone_number(" TEL:+1 ") == "+1"
    assert phone_number("+1 555") == "+1 555"


def test_address_kinds_overflow_and_po_box_folding():
    assert [address_kind(t) for t in (("home",), ("work", "pref"), ("postal",), (), ("home", "work"))] == [
        "home", "work", "other", "other", "home"]
    card = card_of("BEGIN:VCARD\nN:X\nADR;TYPE=HOME:;;1 Rd;Town;;;\nADR;TYPE=HOME:;;2 Rd;;;;\n"
                   "ADR;TYPE=HOME:;;3 Rd;;;;\nADR:PO 1;Suite 2;Main St;City;ST;123;US\nADR;TYPE=WORK:;;;;;;\nEND:VCARD\n")
    assert from_vcard(card).addresses == (Address("home", "1 Rd", "Town"), Address("other", "2 Rd"))
    folded = from_vcard(card_of("BEGIN:VCARD\nN:X\nADR:PO 1;Suite 2;Main St;City;ST;123;US\nEND:VCARD\n"))
    assert folded.addresses == (Address("other", "PO 1\nSuite 2\nMain St", "City", "ST", "123", "US"),)


def test_emails_prefer_the_pref_one_and_stop_at_three():
    card = card_of("BEGIN:VCARD\nN:X\nEMAIL:a@x\nEMAIL;TYPE=HOME:b@x\nEMAIL;TYPE=PREF:c@x\nEMAIL:d@x\nEMAIL:a@x\nEND:VCARD\n")
    assert from_vcard(card).emails == ("c@x", "a@x", "b@x")
    written = vcard.serialize(to_vcard(Contact(emails=("a@x", "b@x", "c@x", "d@x")), "u"))
    assert written.count("EMAIL") == 3 and "EMAIL;TYPE=INTERNET,PREF:a@x" in written and "d@x" not in written


def test_children_and_the_anniversary_and_spouse_aliases():
    card = card_of("BEGIN:VCARD\nN:X\nX-CHILDREN:Tom,Anna\nX-CHILDREN:Lee; Kim\nX-EVOLUTION-ANNIVERSARY:1999-12-31\n"
                   "X-EVOLUTION-SPOUSE:Pat\nX-EVOLUTION-ASSISTANT:Sam\nEND:VCARD\n")
    contact = from_vcard(card)
    assert contact.children == "Tom, Anna, Lee, Kim" and contact.anniversary == dt.date(1999, 12, 31)
    assert contact.spouse == "Pat" and contact.assistant == "Sam"
    preferred = from_vcard(card_of("BEGIN:VCARD\nN:X\nX-ANNIVERSARY:20000101\nANNIVERSARY:20010101\nX-SPOUSE:A\n"
                                   "X-EVOLUTION-SPOUSE:B\nEND:VCARD\n"))
    assert preferred.anniversary == dt.date(2001, 1, 1) and preferred.spouse == "A"


# -- fixtures from real-world producers -------------------------------------------------------------

def test_icloud_card_with_item_groups_and_labelled_dates():
    contact = from_vcard(card_of(ICLOUD))
    assert contact == Contact(
        first_name="John", last_name="Doe", middle_name="Quincy", title="Dr.", suffix="Ph.D.", full_name="Dr. John Doe",
        company="ACME, Inc.", job_title="Head of Sales", department="Sales",
        emails=("john@example.com", "john@acme.example"),
        phones=(("home", "+1 555 010 0102"), ("home2", "+1 555 010 0103"), ("mobile", "+1 (555) 010-0100"),
                ("work_fax", "+1 555 010 0101")),
        addresses=(Address("home", "1 Infinite Loop, Bldg 2", "Cupertino", "CA", "95014", "USA"),),
        birthday=dt.date(1990, 1, 31), anniversary=dt.date(2010, 6, 5), web_page="https://example.com/john",
        notes="Line 1\nLine 2", categories=("Friends", "Work,Play"), uid="9B5E2A70-1C3F-4D8A-9E2B-5F6A7B8C9D0E",
    )
    rewritten = vcard.serialize(to_vcard(contact, contact.uid))
    for dropped in ("PHOTO", "NICKNAME", "X-ABLabel", "X-ABDATE", "REV", "PRODID", "item1."):
        assert dropped not in rewritten
    assert "UID:9B5E2A70-1C3F-4D8A-9E2B-5F6A7B8C9D0E" in rewritten and "FN:Dr. John Doe" in rewritten
    assert from_vcard(vcard.parse(rewritten)[0]) == contact


def test_google_export_card_with_evolution_properties():
    contact = from_vcard(card_of(GOOGLE_EXPORT))
    assert contact.first_name == "Yuki" and contact.last_name == "Nakamura" and contact.full_name == "Yuki Nakamura"
    assert contact.emails == ("yuki@work.example", "yuki@example.jp")
    assert contact.phones == (("work", "+81 3 1234 5678"), ("work", "+81 3 1234 5680"), ("work2", "+81 3 1234 5679"),
                              ("mobile", "+81 90 1234 5678"), ("pager", "+81 3 0000"), ("car", "+81 3 1111"),
                              ("radio", "+81 3 2222"), ("assistant", "+81 3 3333"))
    assert contact.addresses == (Address("work", "PO 1\nFloor 2\n4-2-8 Shibakoen", "Minato-ku", "Tokyo", "105-0011", "Japan"),
                                 Address("other", "Second office", "Osaka", "", "", "Japan"))
    assert (contact.spouse, contact.anniversary, contact.assistant) == ("Kenji", dt.date(2010, 6, 5), "Aiko")
    assert contact.children == "Hana, Ren, Sora" and contact.categories == ("myContacts",) and contact.uid == ""


def test_nextcloud_vcard4_with_tel_uri_pref_and_yearless_birthday():
    contact = from_vcard(card_of(NEXTCLOUD))
    assert contact.phones == (("work", "+81-3-1234-5678"), ("mobile", "+81 90 1234 5678"), ("home_fax", "+81 3 9999"))
    assert contact.emails == ("yuki@example.jp",) and contact.birthday is None
    assert contact.anniversary == dt.date(2010, 6, 5) and contact.spouse == "Kenji"
    assert (contact.company, contact.department, contact.office) == ("例のカンパニー", "開発部", "")
    assert contact.addresses == (Address("work", "4-2-8 Shibakoen", "Minato-ku", "Tokyo", "105-0011", "Japan"),)
    assert contact.uid == "urn:uuid:4fbe8971-0bc3-424c-9c26-36c3e1eff6b1" and contact.categories == ("Colleague",)


def test_pocket_outlook_vcard21_quoted_printable():
    contact = from_vcard(card_of(POCKET_OUTLOOK))
    assert (contact.first_name, contact.last_name, contact.title) == ("Jürgen", "Müller", "Dr.")
    assert contact.full_name == "Dr. Jürgen Müller" and contact.job_title == "Geschäftsführer"
    assert (contact.company, contact.department) == ("Beispiel GmbH", "Vertrieb")
    assert contact.phones == (("work", "+49 89 123456"), ("work2", "+49 89 123457"), ("mobile", "+49 170 1234567"),
                              ("home_fax", "+49 89 654321"))
    assert contact.emails == ("jm@example.de",)
    assert contact.addresses == (Address("work", "Hauptstraße 1", "München", "", "80331", "Germany"),)
    assert contact.notes == "Erste Zeile\nZweite Zeile" and contact.birthday == dt.date(1970, 5, 17)

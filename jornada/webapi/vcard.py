"""vCard 2.1 / 3.0 / 4.0 parsing, serialization and readers for a contacts sync.

Shares the content-line syntax and the :class:`~jornada.webapi.ical.Property`
type with the iCalendar codec.  Tolerates the vCard 2.1 quirks of Pocket
Outlook / old Outlook exports: bare parameters (``TEL;WORK;VOICE:``),
quoted-printable values with a CHARSET, and quoted-printable soft line breaks.

As in :mod:`jornada.webapi.ical`, text-typed values (FN, NOTE, TITLE, ...) are
stored unescaped while structured and list values (N, ADR, ORG, CATEGORIES,
NICKNAME) keep their wire form; :func:`split_structured` and
:func:`split_list` decode those losslessly.
"""
from __future__ import annotations

import datetime as _dt
import quopri
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple, Union

from . import _textprops as tp
from .ical import Property

DEFAULT_VERSION = "3.0"
SUPPORTED_VERSIONS = ("2.1", "3.0", "4.0")
N_PARTS = 5
ADR_PARTS = 7

_ANNIVERSARY_NAMES = ("ANNIVERSARY", "X-ANNIVERSARY", "X-EVOLUTION-ANNIVERSARY")
_APPLE_ANNIVERSARY_LABEL = "_$!<Anniversary>!$_"
_DATE_RE = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})(?:T.*)?$")
_NO_YEAR_RE = re.compile(r"^--\d{2}-?\d{2}")
_TRANSPORT_PARAMS = ("ENCODING", "CHARSET")

Types = Tuple[str, ...]
TypedValue = Tuple[str, Types]
Address = Tuple[Tuple[str, ...], Types]
NameParts = Tuple[str, str, str, str, str]


class VCardError(ValueError):
    """Malformed vCard text or value."""


# --- data model ------------------------------------------------------------------

@dataclass(frozen=True)
class VCard:
    """One vCard: an ordered tuple of properties."""

    properties: Tuple[Property, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "properties", tuple(self.properties))

    @property
    def version(self) -> str:
        """The VERSION value, ``"3.0"`` when absent."""
        return (self.value("VERSION") or DEFAULT_VERSION).strip()

    def get(self, name: str) -> Optional[Property]:
        wanted = name.upper()
        return next((p for p in self.properties if p.name == wanted), None)

    def get_all(self, name: str) -> Tuple[Property, ...]:
        wanted = name.upper()
        return tuple(p for p in self.properties if p.name == wanted)

    def value(self, name: str, default: Optional[str] = None) -> Optional[str]:
        prop = self.get(name)
        return default if prop is None else prop.value

    def with_property(self, prop: Property, replace: bool = True) -> "VCard":
        """Copy with ``prop`` added; by default it replaces properties of the same name."""
        if not replace or self.get(prop.name) is None:
            return VCard(self.properties + (prop,))
        first = next(i for i, p in enumerate(self.properties) if p.name == prop.name)
        kept = [p for p in self.properties if p.name != prop.name]
        kept.insert(min(first, len(kept)), prop)
        return VCard(tuple(kept))

    def without_property(self, name: str) -> "VCard":
        wanted = name.upper()
        return VCard(tuple(p for p in self.properties if p.name != wanted))


# --- parse / serialize -------------------------------------------------------------

def parse(text: str) -> Tuple[VCard, ...]:
    """Parse every ``BEGIN:VCARD`` ... ``END:VCARD`` block in ``text``.

    Nested cards (a vCard 2.1 AGENT) are skipped.  Raises :class:`VCardError`
    on unbalanced BEGIN/END, a property outside a card or a malformed line.
    """
    cards: List[VCard] = []
    current: Optional[List[Property]] = None
    depth = 0
    for line in _join_soft_breaks(tp.unfold(text)):
        parsed = _parse_line(line)
        marker = _card_marker(parsed)
        if marker == "BEGIN":
            depth += 1
            current = [] if current is None else current
        elif marker == "END":
            if current is None:
                raise VCardError("END:VCARD without a matching BEGIN:VCARD")
            depth -= 1
            if depth == 0:
                cards.append(VCard(tuple(current)))
                current = None
        elif current is None:
            raise VCardError(f"property {parsed.name} outside BEGIN:VCARD")
        elif depth == 1:
            current.append(_property_from_line(parsed))
    if current is not None:
        raise VCardError("unterminated BEGIN:VCARD")
    return tuple(cards)


def _parse_line(line: str) -> tp.ContentLine:
    try:
        return tp.parse_line(line)
    except tp.TextPropError as exc:
        raise VCardError(str(exc)) from exc


def _card_marker(parsed: tp.ContentLine) -> Optional[str]:
    if parsed.name in ("BEGIN", "END") and parsed.value.strip().upper() == "VCARD":
        return parsed.name
    return None


def _join_soft_breaks(lines: Iterable[str]) -> Tuple[str, ...]:
    """Join vCard 2.1 quoted-printable soft line breaks (a trailing ``=``)."""
    joined: List[str] = []
    pending: Optional[str] = None
    for line in lines:
        if pending is None:
            if _is_quoted_printable(line) and line.endswith("="):
                pending = line[:-1]
            else:
                joined.append(line)
            continue
        pending += line
        if pending.endswith("="):
            pending = pending[:-1]
        else:
            joined.append(pending)
            pending = None
    if pending is not None:
        joined.append(pending)
    return tuple(joined)


def _is_quoted_printable(line: str) -> bool:
    return "QUOTED-PRINTABLE" in line.split(":", 1)[0].upper()


def _property_from_line(parsed: tp.ContentLine) -> Property:
    raw = parsed.value
    encoding = (tp.param_of(parsed.params, "ENCODING") or "").upper()
    if encoding == "QUOTED-PRINTABLE":
        raw = _decode_quoted_printable(raw, tp.param_of(parsed.params, "CHARSET"))
    dropped = _TRANSPORT_PARAMS if encoding == "QUOTED-PRINTABLE" else ("CHARSET",)
    params = tuple((key, value) for key, value in parsed.params if key not in dropped)
    return Property(parsed.name, tp.decode_value(parsed.name, params, raw), params, parsed.group)


def _decode_quoted_printable(text: str, charset: Optional[str]) -> str:
    data = quopri.decodestring(text.encode("latin-1", "replace"))
    for codec in ((charset or "utf-8").strip(), "utf-8"):
        try:
            return data.decode(codec)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("latin-1")


def serialize(card: VCard, version: str = DEFAULT_VERSION) -> str:
    """Render ``card`` as vCard text of ``version`` (CRLF, folded at 75 octets).

    Version 4.0 writes ``TYPE`` parameter values lower-cased; nothing else
    changes between versions.
    """
    if version not in SUPPORTED_VERSIONS:
        raise VCardError(f"unsupported vCard version {version!r}")
    lines = ["BEGIN:VCARD", f"VERSION:{version}"]
    lines += [_render(prop, version) for prop in card.properties if prop.name != "VERSION"]
    lines.append("END:VCARD")
    return tp.CRLF.join(lines) + tp.CRLF


def _render(prop: Property, version: str) -> str:
    params = _params_for_version(prop.params, version)
    wire = tp.encode_value(prop.name, params, prop.value)
    return tp.fold(tp.format_line(prop.group, prop.name, params, wire))


def _params_for_version(params: tp.Params, version: str) -> tp.Params:
    if version != "4.0":
        return params
    return tuple((key, value.lower() if key == "TYPE" else value) for key, value in params)


# --- structured values -------------------------------------------------------------

def split_structured(value: str) -> Tuple[str, ...]:
    """Split a ``;``-structured value (N, ADR, ORG) into unescaped parts."""
    return tuple(tp.unescape(part) for part in tp.split_unescaped(value, ";"))


def join_structured(parts: Iterable[str]) -> str:
    """Escape and join parts with ``;`` (inverse of :func:`split_structured`)."""
    return ";".join(tp.escape(part) for part in parts)


def split_list(value: str) -> Tuple[str, ...]:
    """Split a comma list (CATEGORIES, NICKNAME) into stripped, unescaped items."""
    items = (tp.unescape(part).strip() for part in tp.split_unescaped(value, ","))
    return tuple(item for item in items if item)


def join_list(items: Iterable[str]) -> str:
    """Escape and join items with ``,`` (inverse of :func:`split_list`)."""
    return ",".join(tp.escape(item.strip()) for item in items if item.strip())


# --- readers -----------------------------------------------------------------------------

def name_parts(card: VCard) -> NameParts:
    """``(family, given, additional, prefixes, suffixes)`` from N; missing parts are ``""``."""
    value = card.value("N") or ""
    parts = tuple(part.strip() for part in split_structured(value)) if value else ()
    padded = (parts + ("",) * N_PARTS)[:N_PARTS]
    return padded[0], padded[1], padded[2], padded[3], padded[4]


def formatted_name(card: VCard) -> str:
    """FN, or a display name assembled from N."""
    formatted = (card.value("FN") or "").strip()
    return formatted or _display_name(name_parts(card))


def _display_name(parts: Sequence[str]) -> str:
    family, given, additional, prefix, suffix = parts
    return " ".join(part for part in (prefix, given, additional, family, suffix) if part)


def org_parts(card: VCard) -> Tuple[str, ...]:
    """ORG split into organisation and units; ``()`` when absent."""
    value = card.value("ORG")
    return tuple(part.strip() for part in split_structured(value)) if value else ()


def telephones(card: VCard) -> Tuple[TypedValue, ...]:
    """``((number, (type, ...)), ...)`` with types lower-cased (``pref`` for PREF)."""
    return tuple((p.value.strip(), types_of(p)) for p in card.get_all("TEL") if p.value.strip())


def emails(card: VCard) -> Tuple[TypedValue, ...]:
    """``((address, (type, ...)), ...)`` with types lower-cased."""
    return tuple((p.value.strip(), types_of(p)) for p in card.get_all("EMAIL") if p.value.strip())


def addresses(card: VCard) -> Tuple[Address, ...]:
    """``((seven ADR parts, (type, ...)), ...)``; parts are padded/truncated to seven."""
    return tuple((_seven(split_structured(p.value)), types_of(p)) for p in card.get_all("ADR"))


def _seven(parts: Sequence[str]) -> Tuple[str, ...]:
    return (tuple(str(part) for part in parts) + ("",) * ADR_PARTS)[:ADR_PARTS]


def types_of(prop: Property) -> Types:
    """Lower-cased TYPE values (2.1 bare, 3.0 lists, 4.0 style), plus ``pref`` for a PREF parameter."""
    values = [value.lower() for value in prop.param_values("TYPE")]
    if prop.param("PREF") is not None:
        values.append("pref")
    unique: List[str] = []
    for value in values:
        if value not in unique:
            unique.append(value)
    return tuple(unique)


def birthday(card: VCard) -> Optional[_dt.date]:
    """BDAY as a date; ``None`` when absent, year-less (``--0131``) or unparseable."""
    return parse_vcard_date(card.value("BDAY"))


def anniversary(card: VCard) -> Optional[_dt.date]:
    """ANNIVERSARY, X-ANNIVERSARY, X-EVOLUTION-ANNIVERSARY or an Apple X-ABDATE labelled Anniversary."""
    for name in _ANNIVERSARY_NAMES:
        found = parse_vcard_date(card.value(name))
        if found is not None:
            return found
    return _apple_anniversary(card)


def _apple_anniversary(card: VCard) -> Optional[_dt.date]:
    labelled = {p.group for p in card.get_all("X-ABLABEL") if p.group and p.value == _APPLE_ANNIVERSARY_LABEL}
    for prop in card.get_all("X-ABDATE"):
        if prop.group in labelled:
            return parse_vcard_date(prop.value)
    return None


def parse_vcard_date(text: Optional[str]) -> Optional[_dt.date]:
    """``19990131``, ``1999-01-31`` or ``1999-01-31T...`` → date; anything else → None."""
    if not text or _NO_YEAR_RE.match(text.strip()):
        return None
    match = _DATE_RE.match(text.strip())
    if not match:
        return None
    try:
        return _dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


# --- builder ---------------------------------------------------------------------------------

def make_vcard(
    uid: str,
    family: str = "",
    given: str = "",
    additional: str = "",
    prefix: str = "",
    suffix: str = "",
    fn: Optional[str] = None,
    org: Sequence[str] = (),
    title: Optional[str] = None,
    tels: Sequence[TypedValue] = (),
    emails: Sequence[TypedValue] = (),
    adrs: Sequence[Address] = (),
    bday: Union[_dt.date, str, None] = None,
    anniversary: Union[_dt.date, str, None] = None,
    note: Optional[str] = None,
    categories: Sequence[str] = (),
    url: Optional[str] = None,
    nickname: Optional[str] = None,
    extra: Sequence[Property] = (),
) -> VCard:
    """Build a vCard; FN is derived from the name (then ORG, then ``uid``) when not given."""
    parts = (family, given, additional, prefix, suffix)
    org_list = [org] if isinstance(org, str) else list(org)
    props: List[Property] = [Property("UID", uid)] if uid else []
    props.append(Property("N", join_structured(parts)))
    props.append(Property("FN", fn or _display_name(parts) or (org_list[0] if org_list else uid)))
    if org_list:
        props.append(Property("ORG", join_structured(org_list)))
    props += _optional_text(("TITLE", title), ("NOTE", note), ("URL", url))
    if nickname:
        props.append(Property("NICKNAME", join_list((nickname,))))
    props += [_typed("TEL", number, types) for number, types in _typed_entries(tels, "tels")]
    props += [_typed("EMAIL", address, types) for address, types in _typed_entries(emails, "emails")]
    props += [_typed("ADR", join_structured(_seven(adr)), types) for adr, types in _typed_entries(adrs, "adrs")]
    props += [Property(name, _date_text(value, name)) for name, value in (("BDAY", bday), ("ANNIVERSARY", anniversary)) if value]
    if categories:
        props.append(Property("CATEGORIES", join_list(categories)))
    props += list(extra)
    return VCard(tuple(props))


def _optional_text(*pairs: Tuple[str, Optional[str]]) -> List[Property]:
    return [Property(name, text) for name, text in pairs if text]


def _typed(name: str, value: str, types: Iterable[str]) -> Property:
    joined = ",".join(t.strip().upper() for t in types if t.strip())
    return Property(name, value, ((("TYPE", joined),) if joined else ()))


def _typed_entries(entries: Sequence, what: str) -> List[Tuple]:
    normalized: List[Tuple] = []
    for entry in entries:
        try:
            value, types = entry
        except (TypeError, ValueError) as exc:
            raise VCardError(f"{what} entries must be (value, types) pairs, got {entry!r}") from exc
        normalized.append((value, tuple(types)))
    return normalized


def _date_text(value: Union[_dt.date, str], name: str) -> str:
    if isinstance(value, _dt.date):
        return value.strftime("%Y%m%d")
    if isinstance(value, str) and parse_vcard_date(value) is not None:
        return value.strip()
    raise VCardError(f"{name} must be a date or an ISO 8601 date string, got {value!r}")

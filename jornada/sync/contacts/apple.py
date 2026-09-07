"""Apple Contacts backend: the people of one Contacts.app group ⇄ the device's contacts, through JXA.

Every operation is one JavaScript-for-Automation script run by ``osascript``. The
scripts are constants: the group name, ids and the contact's fields (one JSON
document) travel as ``argv`` entries and are never interpolated into script
source. Only the people in the group are listed (``group=*`` means everyone);
the group is created when missing.

Contacts.app has no office, categories, anniversary, spouse, children or assistant
fields, so those do not travel. Its labels become the device's kinds
(``_$!<Work>!$_`` → work, a second work number → work2, ``_$!<Mobile>!$_`` →
mobile, …) and the device's kinds become labels on the way back. An update
replaces a person's e-mails, phones, addresses and URLs with the device's
(keeping the label of an e-mail address that was already there); reading a
person's note needs Automation permission for Contacts notes — without it the
note is skipped and reported.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional, Tuple

from ...pim.models import Contact
from ...webapi.applescript import AppleScriptError, Runner, run_jxa
from ..accounts import Account
from ..base import Item, StoreError
from ..registry import BackendSpec, BuildContext, SettingSpec
from .common import (MAX_EMAILS, Log, address, assign_addresses, assign_phone_kinds, iso_date, limit_emails,
                     objects_of, slot_order, string_of, text, with_full_name)

SERVICE = "Apple Contacts"
DEFAULT_GROUP = "Jornada"
GROUP_SETTING = "group"
EVERYONE = "*"
LABEL_WORK = "_$!<Work>!$_"
LABEL_HOME = "_$!<Home>!$_"
LABEL_MOBILE = "_$!<Mobile>!$_"
LABEL_WORK_FAX = "_$!<WorkFAX>!$_"
LABEL_HOME_FAX = "_$!<HomeFAX>!$_"
LABEL_PAGER = "_$!<Pager>!$_"
LABEL_CAR = "_$!<Car>!$_"
LABEL_OTHER = "_$!<Other>!$_"
LABEL_HOME_PAGE = "_$!<HomePage>!$_"
PHONE_LABELS = {"work": LABEL_WORK, "work2": LABEL_WORK, "home": LABEL_HOME, "home2": LABEL_HOME,
                "mobile": LABEL_MOBILE, "work_fax": LABEL_WORK_FAX, "home_fax": LABEL_HOME_FAX, "pager": LABEL_PAGER,
                "car": LABEL_CAR, "radio": "Radio", "assistant": "Assistant"}
PHONE_KINDS_BY_LABEL = {"work": "work", "main": "work", "home": "home", "mobile": "mobile", "iphone": "mobile",
                        "workfax": "work_fax", "homefax": "home_fax", "pager": "pager", "car": "car",
                        "radio": "radio", "assistant": "assistant"}
ADDRESS_LABELS = {"home": LABEL_HOME, "work": LABEL_WORK, "other": LABEL_OTHER}
_LABEL_WRAPPER = re.compile(r"^_\$!<(.*)>!\$_$")

_PRELUDE = """
function contactsApp() { return Application("Contacts"); }
function groupNamed(Contacts, name) {
  if (!name || name === "*") { return null; }
  const group = Contacts.groups.byName(name);
  if (group.exists()) { return group; }
  Contacts.groups.push(Contacts.Group({name: name}));
  Contacts.save();
  return Contacts.groups.byName(name);
}
function peopleIn(Contacts, group) { return group ? group.people : Contacts.people; }
function textOf(value) { return (value === null || value === undefined) ? "" : String(value); }
function stamp(value) { return value ? new Date(value).toISOString() : ""; }
function pad(number) { return (number < 10 ? "0" : "") + number; }
function localDay(value) {
  if (!value) { return ""; }
  const day = new Date(value);
  return day.getFullYear() + "-" + pad(day.getMonth() + 1) + "-" + pad(day.getDate());
}
function dayValue(iso) {
  if (!iso) { return null; }
  const parts = iso.split("-").map(Number);
  return new Date(parts[0], parts[1] - 1, parts[2]);
}
function tryGet(getter) { try { return getter(); } catch (error) { return null; } }
function labelled(elements) {
  const labels = elements.label(), values = elements.value();
  return values.map(function (value, i) { return {label: textOf(labels[i]), value: textOf(value)}; });
}
function addressesOf(person) {
  const items = person.addresses;
  const labels = items.label(), streets = items.street(), cities = items.city(), states = items.state();
  const zips = items.zip(), countries = items.country();
  return labels.map(function (label, i) {
    return {label: textOf(label), street: textOf(streets[i]), city: textOf(cities[i]), state: textOf(states[i]),
            zip: textOf(zips[i]), country: textOf(countries[i])};
  });
}
function applyScalars(person, fields) {
  person.firstName = fields.firstName;
  person.middleName = fields.middleName;
  person.lastName = fields.lastName;
  person.title = fields.title;
  person.suffix = fields.suffix;
  person.organization = fields.organization;
  person.jobTitle = fields.jobTitle;
  person.department = fields.department;
  try { person.birthDate = dayValue(fields.birthday); } catch (error) { if (fields.birthday) { throw error; } }
  try { person.note = fields.note; return false; } catch (error) { return true; }
}
function replaceDetails(Contacts, person, fields) {
  const emailLabels = {};
  person.emails().forEach(function (email) { emailLabels[textOf(email.value())] = textOf(email.label()); });
  [person.emails(), person.phones(), person.addresses(), person.urls()].forEach(function (items) {
    items.forEach(function (item) { Contacts.delete(item); });
  });
  fields.emails.forEach(function (email) {
    person.emails.push(Contacts.Email({label: emailLabels[email.value] || email.label, value: email.value}));
  });
  fields.phones.forEach(function (phone) {
    person.phones.push(Contacts.Phone({label: phone.label, value: phone.value}));
  });
  fields.addresses.forEach(function (a) {
    person.addresses.push(Contacts.Address({label: a.label, street: a.street, city: a.city, state: a.state,
                                            zip: a.zip, country: a.country}));
  });
  if (fields.url) { person.urls.push(Contacts.Url({label: fields.urlLabel, value: fields.url})); }
}
function addToGroup(Contacts, person, group) {
  if (!group) { return; }
  try { Contacts.add(person, {to: group}); } catch (error) { /* already a member */ }
}
"""

LIST_SCRIPT = _PRELUDE + """
function run(argv) {
  const Contacts = contactsApp();
  const people = peopleIn(Contacts, groupNamed(Contacts, argv[0]));
  const ids = people.id();
  const first = people.firstName(), middle = people.middleName(), last = people.lastName();
  const titles = people.title(), suffixes = people.suffix(), orgs = people.organization();
  const jobs = people.jobTitle(), departments = people.department(), birthdays = people.birthDate();
  const modified = people.modificationDate();
  const notes = tryGet(function () { return people.note(); }) || [];
  return JSON.stringify(ids.map(function (id, i) {
    const person = Contacts.people.byId(id);
    return {id: id, firstName: textOf(first[i]), middleName: textOf(middle[i]), lastName: textOf(last[i]),
            title: textOf(titles[i]), suffix: textOf(suffixes[i]), organization: textOf(orgs[i]),
            jobTitle: textOf(jobs[i]), department: textOf(departments[i]), birthday: localDay(birthdays[i]),
            note: textOf(notes[i]), modified: stamp(modified[i]), emails: labelled(person.emails),
            phones: labelled(person.phones), addresses: addressesOf(person),
            url: textOf(person.urls.value()[0])};
  }));
}
"""

CREATE_SCRIPT = _PRELUDE + """
function run(argv) {
  const Contacts = contactsApp();
  const group = groupNamed(Contacts, argv[0]);
  const fields = JSON.parse(argv[1]);
  const person = Contacts.Person({firstName: fields.firstName, lastName: fields.lastName});
  Contacts.people.push(person);
  const noteSkipped = applyScalars(person, fields);
  replaceDetails(Contacts, person, fields);
  addToGroup(Contacts, person, group);
  Contacts.save();
  return JSON.stringify({id: person.id(), modified: stamp(person.modificationDate()), noteSkipped: noteSkipped});
}
"""

UPDATE_SCRIPT = _PRELUDE + """
function run(argv) {
  const Contacts = contactsApp();
  const group = groupNamed(Contacts, argv[0]);
  const person = Contacts.people.byId(argv[1]);
  if (!person.exists()) { throw new Error("the contact is no longer in Contacts"); }
  const fields = JSON.parse(argv[2]);
  const noteSkipped = applyScalars(person, fields);
  replaceDetails(Contacts, person, fields);
  addToGroup(Contacts, person, group);
  Contacts.save();
  return JSON.stringify({id: person.id(), modified: stamp(person.modificationDate()), noteSkipped: noteSkipped});
}
"""

DELETE_SCRIPT = _PRELUDE + """
function run(argv) {
  const Contacts = contactsApp();
  const person = Contacts.people.byId(argv[0]);
  const existed = person.exists();
  if (existed) { Contacts.delete(person); Contacts.save(); }
  return JSON.stringify({id: argv[0], deleted: existed});
}
"""


# -- labels ⇄ kinds ------------------------------------------------------------------
def label_key(label: Any) -> str:
    """``_$!<WorkFAX>!$_`` → ``workfax``; a custom label → its lower-cased text."""
    cleaned = text(label)
    match = _LABEL_WRAPPER.match(cleaned)
    return (match.group(1) if match else cleaned).casefold()


def phone_kind(label: Any) -> str:
    """A Contacts phone label → the device's kind (unknown labels count as work)."""
    return PHONE_KINDS_BY_LABEL.get(label_key(label), "work")


def phone_label(kind: str) -> str:
    return PHONE_LABELS.get(kind, LABEL_WORK)


def address_kind(label: Any) -> str:
    key = label_key(label)
    return key if key in ("home", "work") else "other"


def address_label(kind: str) -> str:
    return ADDRESS_LABELS.get(kind, LABEL_OTHER)


# -- JSON ⇄ Contact -------------------------------------------------------------------
def person_of(entry: Dict[str, Any]) -> Contact:
    """One entry of the LIST script's JSON → Contact."""
    phones = assign_phone_kinds((phone_kind(p.get("label")), p.get("value")) for p in objects_of(entry, "phones"))
    addresses = (address(address_kind(a.get("label")), a.get("street"), a.get("city"), a.get("state"), a.get("zip"),
                         a.get("country")) for a in objects_of(entry, "addresses"))
    contact = Contact(
        first_name=string_of(entry, "firstName"), last_name=string_of(entry, "lastName"),
        middle_name=string_of(entry, "middleName"), title=string_of(entry, "title"), suffix=string_of(entry, "suffix"),
        company=string_of(entry, "organization"), job_title=string_of(entry, "jobTitle"),
        department=string_of(entry, "department"),
        emails=limit_emails(e.get("value") for e in objects_of(entry, "emails")),
        phones=slot_order(phones),
        addresses=assign_addresses(addresses),
        birthday=iso_date(entry.get("birthday")),
        web_page=string_of(entry, "url"),
        notes=string_of(entry, "note").replace("\r\n", "\n"),
        uid=string_of(entry, "id"),
    )
    return with_full_name(contact)      # Contacts composes its display name; the record carries the same


def fields_of(record: Contact) -> Dict[str, Any]:
    """The JSON document the create/update scripts apply to a person."""
    item = record.normalized()
    return {
        "firstName": item.first_name, "middleName": item.middle_name, "lastName": item.last_name,
        "title": item.title, "suffix": item.suffix, "organization": item.company,
        "jobTitle": item.job_title, "department": item.department, "note": item.notes,
        "birthday": item.birthday.isoformat() if item.birthday else "",
        "emails": [{"label": LABEL_OTHER, "value": email} for email in item.emails[:MAX_EMAILS]],
        "phones": [{"label": phone_label(kind), "value": number} for kind, number in slot_order(item.phones)],
        "addresses": [{"label": address_label(a.kind), "street": a.street, "city": a.city, "state": a.state,
                       "zip": a.postal_code, "country": a.country} for a in assign_addresses(item.addresses)],
        "url": item.web_page, "urlLabel": LABEL_HOME_PAGE,
    }


def _id_of(answer: Any, what: str) -> str:
    if not isinstance(answer, dict) or not isinstance(answer.get("id"), str) or not answer["id"]:
        raise StoreError(f"{SERVICE} did not report the id of the contact it should {what}")
    return answer["id"]


def _version_of(answer: Any) -> Optional[str]:
    modified = answer.get("modified") if isinstance(answer, dict) else None
    return modified if isinstance(modified, str) and modified else None


def _check_id(item_id: str) -> None:
    if not isinstance(item_id, str) or not item_id.strip():
        raise StoreError(f"{SERVICE} needs a contact id to update or delete")


# -- the store ------------------------------------------------------------------------
class AppleContactsStore:
    """Store protocol over the people of one Contacts.app group (created when missing)."""

    name = "apple-contacts"

    def __init__(self, group: str = DEFAULT_GROUP, runner: Optional[Runner] = None,
                 log: Log = lambda _line: None) -> None:
        self._group = group.strip() or DEFAULT_GROUP
        self._runner = runner
        self._log = log

    @property
    def group(self) -> str:
        return self._group

    def _run(self, script: str, args: Tuple[str, ...], what: str) -> Any:
        try:
            return run_jxa(script, args, runner=self._runner)
        except AppleScriptError as exc:
            raise StoreError(f"{SERVICE} could not {what}: {exc}") from exc

    def _written(self, answer: Any, record: Contact) -> Any:
        """The answer of a create/update, after reporting a note Contacts refused to store."""
        if isinstance(answer, dict) and answer.get("noteSkipped") is True and record.notes.strip():
            self._log(f"{SERVICE}: the note of {record.display_name()!r} was not written "
                      "(Contacts notes need Automation permission)")
        return answer

    def list(self) -> Tuple[Item, ...]:
        what = "list everyone" if self._group == EVERYONE else f"list the group {self._group!r}"
        answer = self._run(LIST_SCRIPT, (self._group,), what)
        if not isinstance(answer, list):
            raise StoreError(f"{SERVICE} returned an unexpected listing")
        return tuple(self._item(entry) for entry in answer)

    def _item(self, entry: Any) -> Item:
        person_id = _id_of(entry, "list")
        return Item(id=person_id, record=person_of(entry), version=_version_of(entry))

    def create(self, record: Contact) -> str:
        payload = json.dumps(fields_of(record), ensure_ascii=False)
        what = f"create the contact {record.display_name()!r}"
        answer = self._written(self._run(CREATE_SCRIPT, (self._group, payload), what), record)
        return _id_of(answer, "create")

    def update(self, item_id: str, record: Contact) -> Optional[str]:
        _check_id(item_id)
        payload = json.dumps(fields_of(record), ensure_ascii=False)
        what = f"update the contact {record.display_name()!r}"
        answer = self._written(self._run(UPDATE_SCRIPT, (self._group, item_id, payload), what), record)
        return _version_of(answer)

    def delete(self, item_id: str) -> None:
        _check_id(item_id)
        answer = self._run(DELETE_SCRIPT, (item_id,), "delete the contact")
        if isinstance(answer, dict) and answer.get("deleted") is False:
            self._log(f"contact {item_id} was already gone from {SERVICE}")


# -- backend spec ---------------------------------------------------------------------
def build(account: Account, secrets: Dict[str, Any], context: BuildContext) -> AppleContactsStore:
    del secrets  # Contacts.app needs no credentials
    return AppleContactsStore(group=account.setting(GROUP_SETTING, DEFAULT_GROUP) or DEFAULT_GROUP,
                              runner=context.runner, log=context.log)


BACKEND = BackendSpec(
    key="apple",
    title="Apple Contacts (Contacts.app)",
    settings=(
        SettingSpec(GROUP_SETTING, f"Contacts group to sync (default {DEFAULT_GROUP}; created if missing; "
                                   f"{EVERYONE} means every contact)", required=False, default=DEFAULT_GROUP),
    ),
    build=build,
    notes="Drives Contacts.app through osascript; macOS asks for Automation permission on the first run (notes "
          "need a separate permission and are skipped without it). Office, categories, anniversary, spouse, "
          "children and assistant have no place in Contacts and do not travel; an update replaces a person's "
          "e-mails, phones, addresses and web page with the device's.",
)

__all__ = ["AppleContactsStore", "BACKEND", "build", "person_of", "fields_of", "phone_kind", "phone_label",
           "address_kind", "address_label", "label_key", "LIST_SCRIPT", "CREATE_SCRIPT", "UPDATE_SCRIPT",
           "DELETE_SCRIPT", "DEFAULT_GROUP", "EVERYONE"]

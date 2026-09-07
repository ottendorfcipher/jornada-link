import Foundation

/// Contacts Database ⇄ `Contact` (jornada/pim/contacts.py).
public enum ContactCodec {
    /// A second work/home number spills into work2/home2.
    static let overflow = ["work": "work2", "home": "home2"]

    /// The plain text fields in Python's `_TEXT_FIELDS` order.
    static func textFields(of contact: Contact) -> [(value: String, propId: UInt16)] {
        [(contact.firstName, PimIds.contactFirstName), (contact.lastName, PimIds.contactLastName),
         (contact.middleName, PimIds.contactMiddleName), (contact.title, PimIds.contactTitle),
         (contact.suffix, PimIds.contactSuffix), (contact.company, PimIds.contactCompany),
         (contact.jobTitle, PimIds.contactJobTitle), (contact.department, PimIds.contactDepartment),
         (contact.office, PimIds.contactOffice), (contact.spouse, PimIds.contactSpouse),
         (contact.children, PimIds.contactChildren), (contact.assistant, PimIds.contactAssistant),
         (contact.webPage, PimIds.contactWebPage)]
    }

    static func phones(of record: Record) -> [PhoneNumber] {
        PimIds.contactPhoneSlots.compactMap { slot in
            let number = PimCodecSupport.string(record, slot.propId)
            return number.isEmpty ? nil : PhoneNumber(slot.kind, number)
        }
    }

    static func addresses(of record: Record) -> [Address] {
        PimIds.contactAddressSlots.compactMap { slot in
            let parts = slot.propIds.map { PimCodecSupport.string(record, $0) }
            let address = Address(kind: slot.kind, street: parts[0], city: parts[1], state: parts[2],
                                  postalCode: parts[3], country: parts[4])
            return address.isEmpty ? nil : address
        }
    }

    public static func decode(_ record: Record) -> Contact {
        let text = { (propId: UInt16) in PimCodecSupport.string(record, propId) }
        return Contact(
            firstName: text(PimIds.contactFirstName), lastName: text(PimIds.contactLastName),
            middleName: text(PimIds.contactMiddleName), title: text(PimIds.contactTitle),
            suffix: text(PimIds.contactSuffix), fullName: text(PimIds.contactFullName),
            company: text(PimIds.contactCompany), jobTitle: text(PimIds.contactJobTitle),
            department: text(PimIds.contactDepartment), office: text(PimIds.contactOffice),
            emails: PimIds.contactEmailSlots.map(text).filter { !$0.isEmpty },
            phones: phones(of: record), addresses: addresses(of: record),
            birthday: PimCodecSupport.date(record, PimIds.contactBirthday),
            anniversary: PimCodecSupport.date(record, PimIds.contactAnniversary),
            spouse: text(PimIds.contactSpouse), children: text(PimIds.contactChildren),
            assistant: text(PimIds.contactAssistant), webPage: text(PimIds.contactWebPage),
            notes: PimCodecSupport.notes(record),
            categories: PimCodecSupport.splitCategories(record.value(PimIds.contactCategories)))
    }

    /// Assign numbers to device slots; unknown kinds go to work, a second work/home to work2/home2.
    static func phoneSlots(_ phones: [PhoneNumber]) -> [String: String] {
        phones.reduce(into: [String: String]()) { assigned, phone in
            let wanted = PimIds.phoneSlot(for: phone.kind) == nil ? "work" : phone.kind
            let spill = overflow[wanted]
            let slot = assigned[wanted] != nil && spill != nil && assigned[spill!] == nil ? spill! : wanted
            if assigned[slot] == nil { assigned[slot] = phone.number }
        }
    }

    /// Properties for CeWriteRecordProps; `existing` lets cleared fields be deleted.
    public static func encode(_ contact: Contact, existing: Record? = nil) -> [PropVal] {
        let item = contact.normalized()
        let put = { (propId: UInt16, value: String) in PimCodecSupport.stringProps(propId, value, existing: existing) }
        let emails = item.emails.prefix(3) + Array(repeating: "", count: 3)
        let assigned = phoneSlots(item.phones)
        let byKind = Dictionary(item.addresses.map { ($0.kind, $0) }, uniquingKeysWith: { _, last in last })
        let props = textFields(of: item).flatMap { put($0.propId, $0.value) }
            + put(PimIds.contactFullName, item.displayName())
            + zip(PimIds.contactEmailSlots, emails).flatMap { put($0, $1) }
            + PimIds.contactPhoneSlots.flatMap { put($0.propId, assigned[$0.kind] ?? "") }
            + PimIds.contactAddressSlots.flatMap { slot in
                zip(slot.propIds, (byKind[slot.kind] ?? Address(kind: slot.kind)).parts).flatMap { put($0, $1) }
            }
            + PimCodecSupport.dateProps(PimIds.contactBirthday, item.birthday, existing: existing)
            + PimCodecSupport.dateProps(PimIds.contactAnniversary, item.anniversary, existing: existing)
            + PimCodecSupport.notesProps(PimIds.contactNote, item.notes, existing: existing)
            + PimCodecSupport.categoriesProps(item.categories, existing: existing)
        return props.isEmpty ? [.string(PimIds.contactFullName, "(unnamed)")] : props
    }

    public static func isReadOnly(_ contact: Contact) -> Bool { false }
}

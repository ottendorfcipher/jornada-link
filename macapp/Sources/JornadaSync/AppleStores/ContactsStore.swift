import Contacts
import Foundation
import JornadaCore

/// The Mac's Contacts as a `SyncStore`: one group, or every contact of the
/// default account. Notes need the com.apple.developer.contacts.notes
/// entitlement; when reading them fails the store carries on without notes.
final class ContactsStore: SyncStore, @unchecked Sendable {
    let name = "contacts"
    private let store: CNContactStore
    private let groupIdentifier: String?
    private var notesAvailable = true

    static let keys: [CNKeyDescriptor] = [
        CNContactGivenNameKey, CNContactMiddleNameKey, CNContactFamilyNameKey, CNContactNamePrefixKey,
        CNContactNameSuffixKey, CNContactOrganizationNameKey, CNContactJobTitleKey, CNContactDepartmentNameKey,
        CNContactEmailAddressesKey, CNContactPhoneNumbersKey, CNContactPostalAddressesKey, CNContactBirthdayKey,
        CNContactDatesKey, CNContactUrlAddressesKey, CNContactRelationsKey,
    ] as [CNKeyDescriptor]

    init(store: CNContactStore, groupIdentifier: String?) {
        self.store = store
        self.groupIdentifier = groupIdentifier
    }

    private var fetchKeys: [CNKeyDescriptor] {
        notesAvailable ? Self.keys + [CNContactNoteKey as CNKeyDescriptor] : Self.keys
    }

    private func predicate() -> NSPredicate {
        if let groupIdentifier { return CNContact.predicateForContactsInGroup(withIdentifier: groupIdentifier) }
        return CNContact.predicateForContactsInContainer(withIdentifier: store.defaultContainerIdentifier())
    }

    // MARK: - SyncStore

    func list() throws -> [SyncItem] {
        do {
            return try enumerate()
        } catch where notesAvailable {
            notesAvailable = false   // the notes entitlement is missing: retry without the note key
            return try enumerate()
        }
    }

    private func enumerate() throws -> [SyncItem] {
        let request = CNContactFetchRequest(keysToFetch: fetchKeys)
        request.predicate = predicate()
        var items: [SyncItem] = []
        try store.enumerateContacts(with: request) { contact, _ in
            items.append(SyncItem(id: contact.identifier, record: ContactMapping.record(from: contact)))
        }
        return items
    }

    func create(_ record: any SyncRecord) throws -> String {
        let contact: Contact = try expectRecord(record, store: "Contacts")
        let mutable = CNMutableContact()
        ContactMapping.apply(contact, to: mutable, notes: notesAvailable)
        let request = CNSaveRequest()
        request.add(mutable, toContainerWithIdentifier: nil)
        if let group = try group() { request.addMember(mutable, to: group) }
        try store.execute(request)
        return mutable.identifier
    }

    func update(id: String, record: any SyncRecord) throws -> String? {
        let contact: Contact = try expectRecord(record, store: "Contacts")
        let mutable = try mutableContact(id, keys: fetchKeys)
        ContactMapping.apply(contact, to: mutable, notes: notesAvailable)
        let request = CNSaveRequest()
        request.update(mutable)
        try store.execute(request)
        return nil
    }

    func delete(id: String) throws {
        let request = CNSaveRequest()
        request.delete(try mutableContact(id, keys: [CNContactIdentifierKey as CNKeyDescriptor]))
        try store.execute(request)
    }

    private func mutableContact(_ id: String, keys: [CNKeyDescriptor]) throws -> CNMutableContact {
        guard let mutable = try store.unifiedContact(withIdentifier: id, keysToFetch: keys).mutableCopy() as? CNMutableContact else {
            throw StoreError("the contact no longer exists")
        }
        return mutable
    }

    private func group() throws -> CNGroup? {
        guard let groupIdentifier else { return nil }
        guard let group = try store.groups(matching: CNGroup.predicateForGroups(withIdentifiers: [groupIdentifier])).first else {
            throw StoreError("the chosen contact group no longer exists")
        }
        return group
    }
}

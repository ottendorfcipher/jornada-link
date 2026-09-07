import Contacts
import Foundation
import JornadaCore

/// `CNContact` ⇄ `Contact`. Phone labels map to the device's slot kinds
/// (work/home/mobile/fax/pager; car, radio and assistant are custom labels),
/// postal address labels to home/work/other, the anniversary to a labelled date,
/// spouse/children/assistant to related names, the first URL to the web page.
enum ContactMapping {
    static let emailLabels = [CNLabelHome, CNLabelOther, CNLabelOther]

    static func record(from contact: CNContact) -> Contact {
        Contact(firstName: contact.givenName, lastName: contact.familyName, middleName: contact.middleName,
                title: contact.namePrefix, suffix: contact.nameSuffix, company: contact.organizationName,
                jobTitle: contact.jobTitle, department: contact.departmentName,
                emails: contact.emailAddresses.prefix(3).map { String($0.value) },
                phones: contact.phoneNumbers.map { PhoneNumber(phoneKind(label: $0.label), $0.value.stringValue) },
                addresses: contact.postalAddresses.map(address(from:)),
                birthday: DeviceTime.date(from: contact.birthday),
                anniversary: contact.dates.first { $0.label == CNLabelDateAnniversary }
                    .flatMap { DeviceTime.date(from: $0.value as DateComponents) },
                spouse: relations(of: contact, label: CNLabelContactRelationSpouse).first ?? "",
                children: relations(of: contact, label: CNLabelContactRelationChild).joined(separator: ", "),
                assistant: relations(of: contact, label: CNLabelContactRelationAssistant).first ?? "",
                webPage: contact.urlAddresses.first.map { String($0.value) } ?? "",
                notes: contact.isKeyAvailable(CNContactNoteKey) ? contact.note : "")
    }

    static func relations(of contact: CNContact, label: String) -> [String] {
        contact.contactRelations.filter { $0.label == label }.map(\.value.name)
    }

    static func address(from labeled: CNLabeledValue<CNPostalAddress>) -> Address {
        let value = labeled.value
        return Address(kind: addressKind(label: labeled.label), street: value.street, city: value.city,
                       state: value.state, postalCode: value.postalCode, country: value.country)
    }

    static func phoneKind(label: String?) -> String {
        switch label {
        case CNLabelHome: return "home"
        case CNLabelPhoneNumberMobile, CNLabelPhoneNumberiPhone: return "mobile"
        case CNLabelPhoneNumberWorkFax, CNLabelPhoneNumberOtherFax: return "work_fax"
        case CNLabelPhoneNumberHomeFax: return "home_fax"
        case CNLabelPhoneNumberPager: return "pager"
        case "car", "radio", "assistant": return label ?? "work"
        default: return "work"
        }
    }

    static func phoneLabel(kind: String) -> String {
        switch kind {
        case "home", "home2": return CNLabelHome
        case "mobile": return CNLabelPhoneNumberMobile
        case "work_fax": return CNLabelPhoneNumberWorkFax
        case "home_fax": return CNLabelPhoneNumberHomeFax
        case "pager": return CNLabelPhoneNumberPager
        case "car", "radio", "assistant": return kind
        default: return CNLabelWork
        }
    }

    static func addressKind(label: String?) -> String {
        switch label {
        case CNLabelHome: return "home"
        case CNLabelWork: return "work"
        default: return "other"
        }
    }

    static func addressLabel(kind: String) -> String {
        switch kind {
        case "home": return CNLabelHome
        case "work": return CNLabelWork
        default: return CNLabelOther
        }
    }

    static func postalAddress(_ address: Address) -> CNPostalAddress {
        let value = CNMutablePostalAddress()
        value.street = address.street
        value.city = address.city
        value.state = address.state
        value.postalCode = address.postalCode
        value.country = address.country
        return value
    }

    static func dateComponents(_ day: NaiveDate) -> NSDateComponents {
        DateComponents(year: day.year, month: day.month, day: day.day) as NSDateComponents
    }

    static func relation(_ label: String, _ name: String) -> CNLabeledValue<CNContactRelation> {
        CNLabeledValue(label: label, value: CNContactRelation(name: name))
    }

    /// Write the record's fields; keys the record does not model are left as they are.
    static func apply(_ contact: Contact, to target: CNMutableContact, notes: Bool) {
        let record = contact.normalized()
        target.givenName = record.firstName
        target.familyName = record.lastName
        target.middleName = record.middleName
        target.namePrefix = record.title
        target.nameSuffix = record.suffix
        target.organizationName = record.company
        target.jobTitle = record.jobTitle
        target.departmentName = record.department
        target.emailAddresses = record.emails.enumerated().map { index, email in
            CNLabeledValue(label: emailLabels[min(index, emailLabels.count - 1)], value: email as NSString)
        }
        target.phoneNumbers = record.phones.map {
            CNLabeledValue(label: phoneLabel(kind: $0.kind), value: CNPhoneNumber(stringValue: $0.number))
        }
        target.postalAddresses = record.addresses.map {
            CNLabeledValue(label: addressLabel(kind: $0.kind), value: postalAddress($0))
        }
        target.birthday = record.birthday.map { DateComponents(year: $0.year, month: $0.month, day: $0.day) }
        target.dates = target.dates.filter { $0.label != CNLabelDateAnniversary }
            + (record.anniversary.map { [CNLabeledValue(label: CNLabelDateAnniversary, value: dateComponents($0))] } ?? [])
        let managed = [CNLabelContactRelationSpouse, CNLabelContactRelationChild, CNLabelContactRelationAssistant]
        let children = record.children.split(separator: ",").map { PimText.clean(String($0)) }.filter { !$0.isEmpty }
        target.contactRelations = target.contactRelations.filter { !managed.contains($0.label ?? "") }
            + (record.spouse.isEmpty ? [] : [relation(CNLabelContactRelationSpouse, record.spouse)])
            + children.map { relation(CNLabelContactRelationChild, $0) }
            + (record.assistant.isEmpty ? [] : [relation(CNLabelContactRelationAssistant, record.assistant)])
        target.urlAddresses = record.webPage.isEmpty ? []
            : [CNLabeledValue(label: CNLabelURLAddressHomePage, value: record.webPage as NSString)]
        if notes { target.note = record.notes }
    }
}

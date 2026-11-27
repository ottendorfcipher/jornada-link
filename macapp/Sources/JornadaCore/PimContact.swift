import Foundation

/// The neutral contact record and its postal address (jornada/pim/models.py).

public struct Address: Hashable, Sendable {
    public let kind: String
    public let street: String
    public let city: String
    public let state: String
    public let postalCode: String
    public let country: String

    public init(kind: String = "home", street: String = "", city: String = "", state: String = "",
                postalCode: String = "", country: String = "") {
        self.kind = kind
        self.street = street
        self.city = city
        self.state = state
        self.postalCode = postalCode
        self.country = country
    }

    public init(dict: [String: Any]) {
        self.init(kind: dict["kind"] as? String ?? "home", street: DictField.string(dict, "street"),
                  city: DictField.string(dict, "city"), state: DictField.string(dict, "state"),
                  postalCode: DictField.string(dict, "postal_code"), country: DictField.string(dict, "country"))
    }

    /// True when every part is the empty string (Python truthiness: " " is not empty).
    public var isEmpty: Bool { [street, city, state, postalCode, country].allSatisfy(\.isEmpty) }

    public var parts: [String] { [street, city, state, postalCode, country] }

    public func toDict() -> [String: Any] {
        ["kind": kind, "street": street, "city": city, "state": state, "postal_code": postalCode, "country": country]
    }
}

public struct PhoneNumber: Hashable, Sendable {
    public let kind: String
    public let number: String

    public init(_ kind: String, _ number: String) {
        self.kind = kind
        self.number = number
    }
}

public struct Contact: PimRecord {
    public let firstName: String
    public let lastName: String
    public let middleName: String
    public let title: String
    public let suffix: String
    public let fullName: String
    public let company: String
    public let jobTitle: String
    public let department: String
    public let office: String
    public let emails: [String]
    public let phones: [PhoneNumber]
    public let addresses: [Address]
    public let birthday: NaiveDate?
    public let anniversary: NaiveDate?
    public let spouse: String
    public let children: String
    public let assistant: String
    public let webPage: String
    public let notes: String
    public let categories: [String]
    public let uid: String

    public init(firstName: String = "", lastName: String = "", middleName: String = "", title: String = "",
                suffix: String = "", fullName: String = "", company: String = "", jobTitle: String = "",
                department: String = "", office: String = "", emails: [String] = [], phones: [PhoneNumber] = [],
                addresses: [Address] = [], birthday: NaiveDate? = nil, anniversary: NaiveDate? = nil,
                spouse: String = "", children: String = "", assistant: String = "", webPage: String = "",
                notes: String = "", categories: [String] = [], uid: String = "") {
        self.firstName = firstName
        self.lastName = lastName
        self.middleName = middleName
        self.title = title
        self.suffix = suffix
        self.fullName = fullName
        self.company = company
        self.jobTitle = jobTitle
        self.department = department
        self.office = office
        self.emails = emails
        self.phones = phones
        self.addresses = addresses
        self.birthday = birthday
        self.anniversary = anniversary
        self.spouse = spouse
        self.children = children
        self.assistant = assistant
        self.webPage = webPage
        self.notes = notes
        self.categories = categories
        self.uid = uid
    }

    public init?(dict: [String: Any]) {
        let phones = (dict["phones"] as? [Any] ?? []).compactMap { entry -> PhoneNumber? in
            guard let pair = entry as? [Any], pair.count == 2 else { return nil }
            return PhoneNumber(String(describing: pair[0]), String(describing: pair[1]))
        }
        let addresses = (dict["addresses"] as? [Any] ?? []).compactMap { ($0 as? [String: Any]).map(Address.init(dict:)) }
        self.init(firstName: DictField.string(dict, "first_name"), lastName: DictField.string(dict, "last_name"),
                  middleName: DictField.string(dict, "middle_name"), title: DictField.string(dict, "title"),
                  suffix: DictField.string(dict, "suffix"), fullName: DictField.string(dict, "full_name"),
                  company: DictField.string(dict, "company"), jobTitle: DictField.string(dict, "job_title"),
                  department: DictField.string(dict, "department"), office: DictField.string(dict, "office"),
                  emails: DictField.strings(dict, "emails"), phones: phones, addresses: addresses,
                  birthday: DictField.date(dict, "birthday"), anniversary: DictField.date(dict, "anniversary"),
                  spouse: DictField.string(dict, "spouse"), children: DictField.string(dict, "children"),
                  assistant: DictField.string(dict, "assistant"), webPage: DictField.string(dict, "web_page"),
                  notes: DictField.string(dict, "notes"), categories: DictField.strings(dict, "categories"),
                  uid: DictField.string(dict, "uid"))
    }

    public func toDict() -> [String: Any] {
        ["first_name": firstName, "last_name": lastName, "middle_name": middleName, "title": title,
         "suffix": suffix, "full_name": fullName, "company": company, "job_title": jobTitle,
         "department": department, "office": office, "emails": emails,
         "phones": phones.map { [$0.kind, $0.number] }, "addresses": addresses.map { $0.toDict() },
         "birthday": DictField.optional(birthday?.iso), "anniversary": DictField.optional(anniversary?.iso),
         "spouse": spouse, "children": children, "assistant": assistant, "web_page": webPage, "notes": notes,
         "categories": categories, "uid": uid]
    }

    /// The name shown in listings: full name, else the name parts, else company, else the first email.
    public func displayName() -> String {
        let full = PimText.clean(fullName)
        if !full.isEmpty { return full }
        let joined = [firstName, middleName, lastName].map(PimText.clean).filter { !$0.isEmpty }.joined(separator: " ")
        if !joined.isEmpty { return joined }
        let company = PimText.clean(self.company)
        return company.isEmpty ? (emails.first ?? "") : company
    }

    public func normalized() -> Contact {
        let phones = self.phones.compactMap { phone -> PhoneNumber? in
            let number = PimText.clean(phone.number)
            guard !number.isEmpty else { return nil }
            return PhoneNumber(PimVocabulary.phoneKinds.contains(phone.kind) ? phone.kind : "work", number)
        }
        return Contact(firstName: PimText.clean(firstName), lastName: PimText.clean(lastName),
                       middleName: PimText.clean(middleName), title: PimText.clean(title),
                       suffix: PimText.clean(suffix), fullName: PimText.clean(fullName),
                       company: PimText.clean(company), jobTitle: PimText.clean(jobTitle),
                       department: PimText.clean(department), office: PimText.clean(office),
                       emails: PimText.cleanList(emails), phones: phones, addresses: addresses.filter { !$0.isEmpty },
                       birthday: birthday, anniversary: anniversary, spouse: PimText.clean(spouse),
                       children: PimText.clean(children), assistant: PimText.clean(assistant),
                       webPage: PimText.clean(webPage), notes: PimText.normalizedNotes(notes),
                       categories: PimText.cleanList(categories), uid: uid)
    }

    public var matchKey: String {
        let me = normalized()
        let email = me.emails.first.map(PimText.casefold) ?? ""
        return "contact|\(PimText.casefold(me.displayName()))|\(email)"
    }

    public var label: String { PimText.prefix(displayName(), 60) }
}

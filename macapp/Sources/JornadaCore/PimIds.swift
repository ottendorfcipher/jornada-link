import Foundation

/// Pocket Outlook property identifiers and enumerations (jornada/pim/ids.py).
/// The identifiers are the high word of a CEPROPID (the low word is the CEVT
/// type); they were established by SynCE's librra against real devices.
public enum PimIds {
    // MARK: databases
    public static let appointmentsDatabase = "Appointments Database"
    public static let contactsDatabase = "Contacts Database"
    public static let tasksDatabase = "Tasks Database"

    // MARK: shared by appointments and tasks
    public static let notes: UInt16 = 0x0017            // blob: 8-bit text, CRLF line ends
    public static let subject: UInt16 = 0x0037          // string
    public static let sensitivity: UInt16 = 0x0004      // i2: sensitivity*
    public static let importance: UInt16 = 0x0026       // i4: importance*
    public static let categories: UInt16 = 0x4005       // string: "a,b,c" (contacts use the same id)
    public static let reminderMinutes: UInt16 = 0x4501  // i4: minutes before start
    public static let reminderEnabled: UInt16 = 0x4503  // i2: 0/1
    public static let reminderSound: UInt16 = 0x4509    // string: wave file name
    public static let reminderOptions: UInt16 = 0x450A  // i4: reminder* flags
    public static let unknown0002: UInt16 = 0x0002      // i4: librra writes 0 with every appointment
    public static let unknown0003: UInt16 = 0x0003

    public static let sensitivityPublic: Int = 0
    public static let sensitivityPrivate: Int = 1
    public static let importanceHigh: Int = 1
    public static let importanceNormal: Int = 2
    public static let importanceLow: Int = 3
    public static let reminderLed: Int = 1
    public static let reminderVibrate: Int = 2
    public static let reminderDialog: Int = 4
    public static let reminderSoundFlag: Int = 8
    public static let reminderRepeat: Int = 16
    public static let defaultReminderSound = "Alarm1.wav"
    public static let defaultReminderOptions = reminderLed | reminderDialog | reminderSoundFlag

    // MARK: appointments
    public static let apptLocation: UInt16 = 0x4208          // string
    public static let apptStart: UInt16 = 0x420D             // filetime (device wall-clock)
    public static let apptDuration: UInt16 = 0x4213          // i4: minutes
    public static let apptType: UInt16 = 0x4215              // i4: apptType*
    public static let apptOccurrence: UInt16 = 0x4223        // i2: occurrence*
    public static let apptBusyStatus: UInt16 = 0x000F        // i2: busy*
    public static let apptRecurrenceTimezone: UInt16 = 0x0001  // blob (104 bytes)
    public static let apptRecurrencePattern: UInt16 = 0x4015   // blob
    public static let apptAttendees: UInt16 = 0x0030         // blob
    public static let apptUnique: UInt16 = 0x0067            // blob
    public static let apptAttendeeNotified: UInt16 = 0x0064  // filetime

    public static let apptTypeAllDay: Int = 1
    public static let apptTypeNormal: Int = 2
    public static let occurrenceOnce: Int = 0
    public static let occurrenceRepeated: Int = 1
    public static let busyFree: Int = 0
    public static let busyTentative: Int = 1
    public static let busyBusy: Int = 2
    public static let busyOutOfOffice: Int = 3

    // MARK: tasks
    public static let taskStart: UInt16 = 0x4104      // filetime: local midnight
    public static let taskDue: UInt16 = 0x4105        // filetime: local midnight
    public static let taskCompleted: UInt16 = 0x410F  // filetime when completed (devices also use an i2 flag)

    // MARK: contacts
    public static let contactNote: UInt16 = 0x0017
    public static let contactSuffix: UInt16 = 0x3A05
    public static let contactFirstName: UInt16 = 0x3A06
    public static let contactWorkTel: UInt16 = 0x3A08
    public static let contactHomeTel: UInt16 = 0x3A09
    public static let contactLastName: UInt16 = 0x3A11
    public static let contactCompany: UInt16 = 0x3A16
    public static let contactJobTitle: UInt16 = 0x3A17
    public static let contactDepartment: UInt16 = 0x3A18
    public static let contactOffice: UInt16 = 0x3A19
    public static let contactMobileTel: UInt16 = 0x3A1C
    public static let contactRadioTel: UInt16 = 0x3A1D
    public static let contactCarTel: UInt16 = 0x3A1E
    public static let contactWorkFax: UInt16 = 0x3A24
    public static let contactHomeFax: UInt16 = 0x3A25
    public static let contactHome2Tel: UInt16 = 0x3A2F
    public static let contactBirthday: UInt16 = 0x4001      // filetime
    public static let contactAssistant: UInt16 = 0x4002
    public static let contactAnniversary: UInt16 = 0x4003   // filetime
    public static let contactAssistantTel: UInt16 = 0x4004
    public static let contactCategories: UInt16 = 0x4005
    public static let contactChildren: UInt16 = 0x4006
    public static let contactWork2Tel: UInt16 = 0x4007
    public static let contactWebPage: UInt16 = 0x4008
    public static let contactPager: UInt16 = 0x4009
    public static let contactSpouse: UInt16 = 0x400A
    public static let contactFullName: UInt16 = 0x4013
    public static let contactTitle: UInt16 = 0x4023         // honorific (Mr, Dr)
    public static let contactMiddleName: UInt16 = 0x4024
    public static let contactHomeStreet: UInt16 = 0x4040
    public static let contactHomeCity: UInt16 = 0x4041
    public static let contactHomeState: UInt16 = 0x4042
    public static let contactHomePostalCode: UInt16 = 0x4043
    public static let contactHomeCountry: UInt16 = 0x4044
    public static let contactWorkStreet: UInt16 = 0x4045
    public static let contactWorkCity: UInt16 = 0x4046
    public static let contactWorkState: UInt16 = 0x4047
    public static let contactWorkPostalCode: UInt16 = 0x4048
    public static let contactWorkCountry: UInt16 = 0x4049
    public static let contactOtherStreet: UInt16 = 0x404A
    public static let contactOtherCity: UInt16 = 0x404B
    public static let contactOtherState: UInt16 = 0x404C
    public static let contactOtherPostalCode: UInt16 = 0x404D
    public static let contactOtherCountry: UInt16 = 0x404E
    public static let contactEmail: UInt16 = 0x4083
    public static let contactEmail2: UInt16 = 0x4093
    public static let contactEmail3: UInt16 = 0x40A3

    public static let contactEmailSlots: [UInt16] = [contactEmail, contactEmail2, contactEmail3]

    /// Phone kinds in the order Python's `CONTACT_PHONE_SLOTS` dict lists them.
    public static let contactPhoneSlots: [(kind: String, propId: UInt16)] = [
        ("work", contactWorkTel), ("work2", contactWork2Tel), ("home", contactHomeTel),
        ("home2", contactHome2Tel), ("mobile", contactMobileTel), ("work_fax", contactWorkFax),
        ("home_fax", contactHomeFax), ("pager", contactPager), ("car", contactCarTel),
        ("radio", contactRadioTel), ("assistant", contactAssistantTel),
    ]

    /// Address kinds with their street/city/state/postal code/country ids.
    public static let contactAddressSlots: [(kind: String, propIds: [UInt16])] = [
        ("home", [contactHomeStreet, contactHomeCity, contactHomeState, contactHomePostalCode, contactHomeCountry]),
        ("work", [contactWorkStreet, contactWorkCity, contactWorkState, contactWorkPostalCode, contactWorkCountry]),
        ("other", [contactOtherStreet, contactOtherCity, contactOtherState, contactOtherPostalCode, contactOtherCountry]),
    ]

    public static func phoneSlot(for kind: String) -> UInt16? {
        contactPhoneSlots.first { $0.kind == kind }?.propId
    }
}

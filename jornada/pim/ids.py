"""Pocket Outlook property identifiers and enumerations (Windows CE 2.x / 3.x).

The identifiers are the high word of a CEPROPID (the low word is the CEVT value
type). They were established by the SynCE project (librra ``*_ids.h``) against
real devices; the enumeration values come from the same source. Property types
are the ones librra writes: strings are LPWSTR, notes are a BLOB of 8-bit text
(see :mod:`jornada.pim.notes_blob`), dates are FILETIME.
"""
from __future__ import annotations

# --- databases ---------------------------------------------------------------
DB_APPOINTMENTS = "Appointments Database"
DB_CONTACTS = "Contacts Database"
DB_TASKS = "Tasks Database"

# --- shared by appointments and tasks ------------------------------------------
NOTES = 0x0017            # blob: 8-bit text, CRLF line ends
SUBJECT = 0x0037          # string
SENSITIVITY = 0x0004      # i2: SENSITIVITY_*
IMPORTANCE = 0x0026       # i4: IMPORTANCE_*
CATEGORIES = 0x4005       # string: "a,b,c" (contacts use the same id)
REMINDER_MINUTES = 0x4501  # i4: minutes before start
REMINDER_ENABLED = 0x4503  # i2: 0/1
REMINDER_SOUND = 0x4509    # string: wave file name
REMINDER_OPTIONS = 0x450A  # i4: REMINDER_* flags
UNKNOWN_0002 = 0x0002      # i4: librra writes 0 with every appointment
UNKNOWN_0003 = 0x0003

SENSITIVITY_PUBLIC = 0
SENSITIVITY_PRIVATE = 1
IMPORTANCE_HIGH = 1
IMPORTANCE_NORMAL = 2
IMPORTANCE_LOW = 3
REMINDER_LED = 1
REMINDER_VIBRATE = 2
REMINDER_DIALOG = 4
REMINDER_SOUND_FLAG = 8
REMINDER_REPEAT = 16
DEFAULT_REMINDER_SOUND = "Alarm1.wav"
DEFAULT_REMINDER_OPTIONS = REMINDER_LED | REMINDER_DIALOG | REMINDER_SOUND_FLAG

# --- appointments --------------------------------------------------------------
APPT_LOCATION = 0x4208          # string
APPT_START = 0x420D             # filetime (device wall-clock)
APPT_DURATION = 0x4213          # i4: minutes
APPT_TYPE = 0x4215              # i4: APPT_TYPE_*
APPT_OCCURRENCE = 0x4223        # i2: OCCURRENCE_*
APPT_BUSY_STATUS = 0x000F       # i2: BUSY_*
APPT_RECURRENCE_TIMEZONE = 0x0001  # blob (104 bytes)
APPT_RECURRENCE_PATTERN = 0x4015   # blob
APPT_ATTENDEES = 0x0030         # blob
APPT_UNIQUE = 0x0067            # blob
APPT_ATTENDEE_NOTIFIED = 0x0064  # filetime

APPT_TYPE_ALL_DAY = 1
APPT_TYPE_NORMAL = 2
OCCURRENCE_ONCE = 0
OCCURRENCE_REPEATED = 1
BUSY_FREE = 0
BUSY_TENTATIVE = 1
BUSY_BUSY = 2
BUSY_OUT_OF_OFFICE = 3

# --- tasks -----------------------------------------------------------------------
TASK_START = 0x4104      # filetime: local midnight
TASK_DUE = 0x4105        # filetime: local midnight
TASK_COMPLETED = 0x410F  # filetime when completed (devices also use an i2 flag)

# --- contacts --------------------------------------------------------------------
CONTACT_NOTE = 0x0017
CONTACT_SUFFIX = 0x3A05
CONTACT_FIRST_NAME = 0x3A06
CONTACT_WORK_TEL = 0x3A08
CONTACT_HOME_TEL = 0x3A09
CONTACT_LAST_NAME = 0x3A11
CONTACT_COMPANY = 0x3A16
CONTACT_JOB_TITLE = 0x3A17
CONTACT_DEPARTMENT = 0x3A18
CONTACT_OFFICE = 0x3A19
CONTACT_MOBILE_TEL = 0x3A1C
CONTACT_RADIO_TEL = 0x3A1D
CONTACT_CAR_TEL = 0x3A1E
CONTACT_WORK_FAX = 0x3A24
CONTACT_HOME_FAX = 0x3A25
CONTACT_HOME2_TEL = 0x3A2F
CONTACT_BIRTHDAY = 0x4001      # filetime
CONTACT_ASSISTANT = 0x4002
CONTACT_ANNIVERSARY = 0x4003   # filetime
CONTACT_ASSISTANT_TEL = 0x4004
CONTACT_CATEGORIES = 0x4005
CONTACT_CHILDREN = 0x4006
CONTACT_WORK2_TEL = 0x4007
CONTACT_WEB_PAGE = 0x4008
CONTACT_PAGER = 0x4009
CONTACT_SPOUSE = 0x400A
CONTACT_FULL_NAME = 0x4013
CONTACT_TITLE = 0x4023         # honorific (Mr, Dr)
CONTACT_MIDDLE_NAME = 0x4024
CONTACT_HOME_STREET = 0x4040
CONTACT_HOME_CITY = 0x4041
CONTACT_HOME_STATE = 0x4042
CONTACT_HOME_POSTAL_CODE = 0x4043
CONTACT_HOME_COUNTRY = 0x4044
CONTACT_WORK_STREET = 0x4045
CONTACT_WORK_CITY = 0x4046
CONTACT_WORK_STATE = 0x4047
CONTACT_WORK_POSTAL_CODE = 0x4048
CONTACT_WORK_COUNTRY = 0x4049
CONTACT_OTHER_STREET = 0x404A
CONTACT_OTHER_CITY = 0x404B
CONTACT_OTHER_STATE = 0x404C
CONTACT_OTHER_POSTAL_CODE = 0x404D
CONTACT_OTHER_COUNTRY = 0x404E
CONTACT_EMAIL = 0x4083
CONTACT_EMAIL2 = 0x4093
CONTACT_EMAIL3 = 0x40A3

CONTACT_EMAIL_SLOTS = (CONTACT_EMAIL, CONTACT_EMAIL2, CONTACT_EMAIL3)
CONTACT_PHONE_SLOTS = {
    "work": CONTACT_WORK_TEL, "work2": CONTACT_WORK2_TEL, "home": CONTACT_HOME_TEL,
    "home2": CONTACT_HOME2_TEL, "mobile": CONTACT_MOBILE_TEL, "work_fax": CONTACT_WORK_FAX,
    "home_fax": CONTACT_HOME_FAX, "pager": CONTACT_PAGER, "car": CONTACT_CAR_TEL,
    "radio": CONTACT_RADIO_TEL, "assistant": CONTACT_ASSISTANT_TEL,
}
CONTACT_ADDRESS_SLOTS = {
    "home": (CONTACT_HOME_STREET, CONTACT_HOME_CITY, CONTACT_HOME_STATE, CONTACT_HOME_POSTAL_CODE, CONTACT_HOME_COUNTRY),
    "work": (CONTACT_WORK_STREET, CONTACT_WORK_CITY, CONTACT_WORK_STATE, CONTACT_WORK_POSTAL_CODE, CONTACT_WORK_COUNTRY),
    "other": (CONTACT_OTHER_STREET, CONTACT_OTHER_CITY, CONTACT_OTHER_STATE, CONTACT_OTHER_POSTAL_CODE, CONTACT_OTHER_COUNTRY),
}

# USB, the dock, and the Jornada 680e

This note records what we could establish about connecting an **SH-3 Jornada
(680/680e/690/690e)** over the **USB-B jack of the HP F1822A dock**, and what
jornada-link's *USB link* module does about it. Short version: the jack is
electrically inert for those models, because the handheld has no USB silicon —
no host-side driver can change that. What *can* be done is (a) pick and
diagnose the right USB-serial adapter automatically, (b) retrofit a bridge into
the dock so the 680e does come up the dock's USB cable, and (c) later, a native
USB link for the StrongARM 720/728 that the same dock was designed for.

## The hardware, with sources

| Fact | Source |
|---|---|
| The Jornada 680/680e/690/690e use the Hitachi **SH7709A (SH-3) at 133 MHz**, Windows CE 2.11 / H/PC Pro 3.0; the 710/720/728 use the **Intel StrongARM SA-1110 at 206 MHz**, Windows CE 3.0 / H/PC 2000. | [Wikipedia: HP Jornada](https://en.wikipedia.org/wiki/HP_Jornada) |
| HPC:Factor's 680e specification sheet lists **USB: not supported (host or client)**; RS-232 and IrDA SIR only. | [HPC:Factor 680e specs](https://www.hpcfactor.com/hardware/devices/61/Hewlett_Packard/Jornada_680e) |
| The SH7709A's companion chip is the **HD64461**, whose peripherals are the LCD controller, PCMCIA, timers, UART, I²C, GPIO and the modem AFE — **no USB block**. The SH7709A itself has SCI/SCIF/IrDA serial ports and no USB. | [Hitachi SH7709 chip-set announcement](https://www.hitachi.com/New/cnews/E/1997/970619B.html), [CPU Shack summary](https://www.cpushack.com/CIC/embed/announce/HitachiSH7709.html) |
| The SA-1110 has an **integrated USB Device Controller (UDC)** on serial port 0 — that is what makes the 720/728 enumerate. | [Intel SA-1110 specification update](https://bitsavers.org/components/intel/ARM/SA-111x/278259-022_SA-1110_Specification_Update_200104.pdf) |
| The 680/690 cradle "lacks the USB client connection"; the 710/720/728 cradle has it. | [HPC:Factor: Cradle and USB connection to Jornada](https://www.hpcfactor.com/forums/forums/thread-view.asp?tid=11375&start=1) |
| The F1822A dock is sold as fitting the **680/690/710/720/728** and has both a DB-9 and a USB-B jack. | [eBay listing, F1822A#ABA](https://www.ebay.com/itm/HP-Jornada-600-700-Series-Dock-Kit-for-680-690-710-720-728-F1822A-ABA-pp-/274435490691) |
| The Jornada 720 cradle is **passive**: it routes the connector's RS-232 lines to the DB-9 and the USB pins (+5 V, D−, D+, shield) to the USB-B jack. The handheld's signals are already at RS-232 levels (about −5 V idle). | [lowlevel.cz: Jornada 720 dock and cable pinout](https://www.lowlevel.cz/log/pivot/entry.php?id=41) |
| The same 10-pin connector family: "pins 1-4 are USB and most … of the remainder for serial, with pin 4 shared" (Jornada 54x cable notes); "the USB cradle only populates pins 1-4". | [Bev Howard: Serial Port Secrets](https://bevhoward.com/serial.htm) |
| The 720 pinout page "works with the 680": a 680 owner traced the dock's 10 pins to the serial cable and a DB-9. | [faintshadows: The HP Jornada 680](http://faintshadows.xyz/vintage/2024/08/22/Jornada-Revisit.html), [H]ardForum thread](https://hardforum.com/threads/possible-to-fabricate-a-serial-cable-for-an-hp-jornada-680.902533/) |
| On Windows, a USB-docked 720 appears as **"Windows CE USB Devices"** served by `wceusbsh.sys`; the matching `.inf` lists HP vendor `03F0` products `1016, 1116, 1216, 2016, …, 5216` ("HP USB Sync"). Linux's `ipaq` USB-serial driver carries the same IDs. | [HPC:Factor: USB Sync with Jornada 720](https://www.hpcfactor.com/forums/forums/thread-view.asp?tid=13333&start=0), [wceusbsh.inf](https://contents.driverguide.com/content.php?id=237126&path=wceusbsh.inf), [Linux drivers/usb/serial/ipaq.c](https://raw.githubusercontent.com/torvalds/linux/master/drivers/usb/serial/ipaq.c) |
| Windows CE 2.11 can drive USB **host** ports on machines that have them (the community `232usb` driver ships SH builds), but that is a host stack for peripherals, not a device/function stack for syncing. | [HPC-Factor/232usb](https://github.com/HPC-Factor/232usb) |

Putting it together: the dock's USB-B jack is four wires to four connector pins.
On a 720/728 those pins reach the SA-1110's UDC and Windows CE 3.0's USB Sync
function. On a 680e they reach nothing — there is no USB device controller in
the SH7709A, in the HD64461, or elsewhere on the board (the SH-3 Jornada **540**
Pocket PCs did sync over USB, but they carry separate USB silicon on the board;
the 680/690 do not). A "driver" for the 680e's dock USB jack therefore cannot
exist in software on either end.

### Connector notes

The handheld's 10-pin connector carries RS-232-level serial (TD, RD, RTS, CTS,
DTR, DSR, CD, GND) on eight pins, and the cradle wiring adds USB +5 V, D−, D+
with the shield as ground. The two public pin tables number the connector from
opposite ends (one looks into the handheld's port, the other at the cradle from
below), so **verify with a meter** before wiring anything: the −5 V idle level
on TD identifies the serial pins, and the cradle's USB-B jack identifies the USB
ones. The HP serial sync cable (F1258A, DB-9 female) and the dock's DB-9 present
the same signals; a 680 owner reported that the DB-9 had to be **female** for
Carrier Detect to be right when using a USB-serial adapter.

## What the USB link module does

Three small pieces, in both languages (`jornada/usb_*.py`,
`macapp/Sources/JornadaCore/Usb*.swift`), verified by `tests/test_usb_*.py` and
the `SelfTest usb-profiles` parity check:

1. **Driver profiles** (`usb_profiles`): a table of the USB vendor/product IDs
   that can carry a link — FTDI, Prolific, Silicon Labs and WCH bridges (with
   which macOS driver serves them), class-compliant CDC-ACM devices, and the
   Windows CE **USB Sync** function devices (HP `03F0:xx16`, Compaq, Microsoft,
   HTC). A bridge whose USB product or serial string contains `JORNADA` or
   `JDOCK` is treated as a **dock bridge** (see the retrofit below). Alongside it,
   a **handheld matrix** says for each model that fits the dock whether its USB
   pins are live (SH-3 rows: no; SA-1110 rows: yes).
2. **Registry enumeration** (`usb_registry`): the I/O Registry, read with
   `/usr/sbin/ioreg` (Python) or IOKit (Swift). For every USB device it reports
   the interfaces and every `/dev/cu.*` node below it, **with the driver that
   created the node and whether this user can open it**. One adapter can expose
   two nodes — Apple's built-in `AppleUSBFTDI` and a vendor DriverKit extension
   — and the vendor one may be mode 0644, which is exactly the kind of thing that
   makes "Connect" fail mysteriously.
3. **The doctor** (`usb_doctor`): ranks the serial nodes (pinned port first,
   then a dock bridge, then a node owned by Apple's driver, then anything this
   user can open), and explains what it sees: the inert-jack situation for an
   SH-3 handheld, a Windows CE USB Sync device that macOS has no driver for, an
   adapter with no node (driver missing), duplicate nodes, permission problems.

The link uses the doctor's pick: `PppController.serialDevice()` in the app and
`bin/jornada-ppp` (via `jornada usb pick`) both consult it before falling back
to the first `/dev/cu.usbserial-*`. The pin file `~/.jornada-link/serial`
(`jornada usb pin PATH`) still overrides; the app's USB/Serial pane only reports.

```bash
./bin/jornada usb                      # status: adapter, port, and any real problem
./bin/jornada usb --json               # the same, machine-readable
./bin/jornada usb pick                 # the chosen port only (bin/jornada-ppp calls this)
./bin/jornada usb pin /dev/cu.usbserial-XXXX ; ./bin/jornada usb unpin
```

The app's **USB** pane shows the same picture live (polled every few seconds
while open), lets you choose the docked model, and pins a port with one click.

## Making the 680e come up the dock's USB cable: the dock-bridge retrofit

Because the dock is passive and the handheld's serial lines are already
RS-232 levels, the only way to make the dock's USB-B jack useful for a 680e is
to put the USB-serial adapter **inside the dock**:

- An **FT231X / FT232R** module plus an **RS-232 transceiver** (a MAX3232 or the
  transceiver already on a DB-9 FTDI cable) between the connector's serial pins
  (TD, RD, RTS, CTS, DTR, DSR, CD, GND) and the dock's USB-B jack. Disconnect
  the jack's original four wires to the USB pins first (they only matter for a
  720/728; a switch keeps both uses).
- Program the FTDI EEPROM's **product string** to something containing
  `Jornada` (for example `Jornada Dock Bridge`) or the serial number to start
  with `JDOCK`. FTDI's FT_Prog (Windows) or `ftdi_eeprom` (libftdi) do this.
  The doctor then reports the adapter as a **dock bridge**, ranks it above any
  other adapter, and, with an SH-3 handheld connected, says so in green.
- Nothing changes on the Jornada: PC Link still talks RS-232 to what it thinks
  is a PC serial port. The Mac sees an FTDI device and macOS's built-in driver
  provides the `/dev/cu.usbserial-*` node; jornada-link works unchanged.

The 680e's throughput stays that of its serial port (115200 bps), which is a
property of the SH-3's UART, not of the cable.

## Roadmap: a native USB link for the 720/728

The same dock, with a 720/728, enumerates a Windows CE USB Sync device
(vendor `03F0`) that macOS has no driver for. The profile table already
recognises it and the doctor explains it. The next step is a userspace driver
in `JornadaCore` built on Apple's **IOUSBHost** framework (no kernel extension,
Apple frameworks only): claim the vendor-specific interface, run the bulk IN/OUT
pipes, and present the byte stream to `pppd` through a pseudo-terminal so the
existing PPP → dccm → RAPI stack runs unchanged. Open questions to settle on
hardware: whether the CE 3.0 function needs the `wceusbsh`-style connect
preamble, how DTR/RTS are signalled (Linux's `ipaq` driver is the reference),
and the pty path validation in the root PPP helper, which today accepts only
`/dev/cu.*` nodes by design.

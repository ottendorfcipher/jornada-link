# jornada-link

Talk to an **HP Jornada 680e** (Windows CE 2.11 / H/PC Pro) from macOS over a
USB-serial (FTDI) cable: file listing, copy in both directions, delete/move/
mkdir, launch programs, battery/storage status — with **nothing installed on
the Jornada**. It speaks the device's own ActiveSync-era protocols (PPP over
serial, then the "dccm" keep-alive on TCP 5679 and RAPI on TCP 990), ported
to Python from the SynCE project's `dccm` and `librapi2` sources.

```
Jornada 680e ──HP serial sync cable──► FTDI FT232R ──USB──► Mac
      \_ PPP 192.168.131.201 ◄──────────────────► 192.168.131.102 _/
```

## Quick start

1. **Bring up the link** (needs root for `pppd`; also starts the dccm
   listener as your user):

   ```bash
   sudo ~/Desktop/jornada-link/bin/jornada-ppp
   ```

   It auto-picks `/dev/cu.usbserial-*` and uses 19200 baud (the Jornada's
   factory "PC Connection" rate). Explicit form:
   `sudo bin/jornada-ppp /dev/cu.usbserial-BG00T191 19200`.

2. **On the Jornada:** Start ▸ Programs ▸ Communication ▸ **PC Link**
   (or double-click a *Direct Connection* in Remote Networking). The Mac side
   prints the `CLIENT`/`CLIENTSERVER` handshake, PPP negotiation, then the
   dccm log shows `Talking to 'Jornada' ... RAPI available`.

3. **Use it** (second terminal, no root):

   ```bash
   bin/jornada status                       # OS version, storage, battery
   bin/jornada ls '\'                       # root of the object store
   bin/jornada ls '\My Documents'
   bin/jornada get '\My Documents\notes.pwd' ~/Desktop/notes.pwd
   bin/jornada put ~/Desktop/photo.bmp '\My Documents\'
   bin/jornada mkdir '\My Documents\Mac'
   bin/jornada mv '\My Documents\a.txt' '\My Documents\Mac\a.txt'
   bin/jornada rm '\My Documents\old.txt'
   bin/jornada run '\Windows\pword.exe' '\My Documents\notes.pwd'
   ```

   Device paths use backslashes — single-quote them in the shell.

### Faster transfers

19200 baud is ~1.9 KB/s. On the Jornada open Control Panel ▸ Communications ▸
**PC Connection** and pick *Serial Port @ 115200* (if offered), then run
`sudo bin/jornada-ppp /dev/cu.usbserial-XXXX 115200`.

### Device password

If the Jornada has a power-on password, both halves need it. Export
`JORNADA_PASSWORD=...` and run `sudo -E bin/jornada-ppp` (the wrapper hands it
to the dccm listener); the CLI commands read the same variable, or take
`--password`.

## Plan B: plain HTTP (device ▸ Mac downloads only)

Once PPP is up, any TCP works. Put files in `share/` and run

```bash
python3 -m http.server --bind 192.168.131.102 8000 --directory share
```

then open `http://192.168.131.102:8000/` in Pocket Internet Explorer on the
Jornada. Useful if RAPI misbehaves; it cannot copy files *from* the device.

## Troubleshooting

* `bin/jornada probe /dev/cu.usbserial-XXXX 19200 8` — sniff the line without
  root. You should see `CLIENT` while PC Link is trying; at the wrong baud it
  looks like `E0 00 E0 00 …`.
* pppd prints LCP/IPCP negotiation (`debug`) to the terminal; the dccm
  listener logs to `~/.jornada-link/dccm.log`; the live session is recorded in
  `~/.jornada-link/connection.json`.
* "cannot reach the Jornada's RAPI port": PPP isn't up, dccm isn't running,
  or PC Link wasn't started on the device *after* `jornada-ppp`.
* The device drops after ~15 s: dccm wasn't listening (it must answer the
  device's info packet with `0x12345678`).
* Apple's `pppd` insists that `/etc/ppp/options` exists; the wrapper creates
  an empty one.
* Extra pppd options: `PPP_EXTRA="lcp-echo-interval 10 lcp-echo-failure 6" sudo -E bin/jornada-ppp`.

## Layout

```
bin/jornada         CLI launcher (dccm, status, ls, get, put, rm, mkdir, rmdir, mv, run, probe)
bin/jornada-ppp     sudo wrapper: dccm listener + pppd + chat handshake
jornada/wire.py     RAPI marshalling primitives
jornada/transport.py  framed socket I/O
jornada/dccm.py     port-5679 listener (device keep-alive / info packet / password)
jornada/rapi.py     RAPI client: files, directories, processes, system info
jornada/info.py     device info packet codec
jornada/password.py password XOR/UTF-16 encoding
jornada/state.py    connection.json helpers
jornada/serial_probe.py  raw serial sniffer (termios, no pyserial)
tests/              pytest suite incl. an in-memory fake Jornada (RAPI server + ActiveSync client)
```

Run the tests with `python3 -m pytest -q tests`.

## Protocol notes (from SynCE)

* Serial handshake: device repeats `CLIENT`; host answers `CLIENTSERVER`
  (no CR) and starts PPP. pppd options: `<dev> <baud> 192.168.131.102:192.168.131.201
  ms-dns 192.168.131.102 noauth local nodefaultroute` (SynCE adds `crtscts`;
  the FTDI link here works without flow control).
* dccm (TCP 5679, device→host): 4-byte LE header. `0` = empty, `0x12345678` =
  ping reply, `< 512` = info packet of that length (OS version @4, build @6,
  CPU @8, partner ids @0x10/0x14, UTF-16 string offsets @0x18/0x1C/0x20 for
  name/class/hardware), otherwise a password challenge whose low byte is the
  XOR key. Host answers the info packet with `0x12345678` and repeats it every
  5 s; three unanswered pings = hang-up.
* RAPI (TCP 990, host→device): `u32 length` + `u32 command` + args. Reply:
  `u32 result_1` (1 ⇒ an HRESULT follows), `u32 last_error`, `u32 return`,
  outputs. Strings are `1, nchars+1, UTF-16LE+NUL`; "optional" buffers are
  `1, size, has_data, [data]`. Commands: FindAllFiles 0x09, CreateFile 0x05,
  ReadFile 0x06, WriteFile 0x07, CloseHandle 0x08, CreateDirectory 0x17,
  RemoveDirectory 0x18, CreateProcess 0x19, MoveFile 0x1A, DeleteFile 0x1C,
  GetFileAttributes 0x03, GetStoreInformation 0x29, GetVersionEx 0x3B,
  GetSystemPowerStatusEx 0x41.

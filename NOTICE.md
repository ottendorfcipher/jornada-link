# Notices and attribution

`jornada-link` is an independent, clean-room reimplementation of the
Windows CE "ActiveSync"-era wire protocols (the dccm connection notifier on
TCP 5679 and the RAPI remote API on TCP 990) in Python and Swift. It contains
**no code** from any of the projects below; it was written from protocol
knowledge and interoperates with unmodified 1990s hardware.

## Protocol references

The protocol behavior was learned from the documentation and source of the
**SynCE** project (<https://github.com/synce>, historically
<http://synce.sourceforge.net>), which is licensed under the GNU
GPL/LGPL. SynCE is not a dependency of this project and none of its source is
included or redistributed here. We gratefully acknowledge the SynCE authors'
reverse-engineering work, without which this project would not exist.

Where wire constants match SynCE (command opcodes, the `0x12345678` ping word,
the password XOR scheme), that is because they describe the same fixed,
decades-old on-device protocol — facts of interoperability, not creative
expression.

## Trademarks

"HP", "Jornada", "Windows CE", "ActiveSync", and related marks belong to their
respective owners. This project is not affiliated with, endorsed by, or
sponsored by Hewlett-Packard, HP Inc., or Microsoft. It is an independent
interoperability tool for hardware its owners already possess.

# OpenSchool — Project Rules

See `DESIGN.md` for the full system architecture and the decisions behind it.

## Development rules (non-negotiable)

1. **Every feature has tests.** No feature lands without tests covering it.
2. **Run the full suite on every change.** `make test` must pass before any
   change is considered done — not just the tests near what you touched.
3. **Human-readable everything.** Comments, file names, and variable/function
   names must read clearly to a person. No cryptic abbreviations.
4. **Short, concise commit messages.** One readable line describing the change.

## Architecture summary

OpenSchool netboots heterogeneous (x86_64 / arm64) UEFI machines into a
locked-down, immutable Debian desktop with FreeIPA identity and Kerberos-secured
NFS home directories, plus DNS-blocklist internet filtering. Boot policy lives in
the standalone **tftpjail** TFTP server. Full detail in `DESIGN.md`.

## Security posture

- Boot profile decides *which image/network policy* a machine gets — never *who
  you are*. Identity and the admin/teacher/headmaster/student levels come from
  FreeIPA/Kerberos at the OS layer.
- Keep secrets out of boot assets. Default-deny in tftpjail.
- Lockdown is structural: read-only squashfs root + tmpfs overlay means nothing
  on the machine that isn't in the image.

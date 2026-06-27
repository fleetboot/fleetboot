"""Real-world full-PXE-chain test against a VirtualBox UEFI guest.

Excluded from `make test` AND from `make functional-test` because it spawns
a VirtualBox VM and binds the privileged UDP/69 port. Run via
`make vbox-functional-test`.
"""

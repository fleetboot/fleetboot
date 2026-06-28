"""Dev scaffolding — long-running fleetboot+tftpjail for interactive use.

`run_server.py` brings up both control-plane services with a persistent
registry (build/dev/machines.sqlite) and the dashboard enabled.
`boot_dev_vm.py` enrols a generated MAC and starts a transient libvirt
QEMU UEFI VM that will appear on the dashboard as it boots.
"""

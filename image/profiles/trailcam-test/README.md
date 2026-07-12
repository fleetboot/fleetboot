# `trailcam-test` profile

Throwaway test bench for the TrailCam Go download client (see the
`trailcamdownloader` repo). Parents: `ssh-debug`.

## What ships
- **Bluetooth**: `bluez` + `bluetoothd` enabled, rfkill unblocked at boot.
- **WiFi**: `wpasupplicant`, `iw`, `wireless-regdb`, `isc-dhcp-client`,
  plus nonfree firmware (`firmware-realtek/atheros/misc-nonfree/iwlwifi`)
  for the USB dongles.
- **Python venv** at `/opt/tcam/venv` with `bleak` + `pycryptodome`.
- **Scripts** in `/opt/tcam/`: `wake.py`, `arlink_crypto.py`, `join-cam.sh`.
- **Diag**: `usbutils`, `pciutils`, `tcpdump`, `iputils-ping`.
- **SSH**: root login by key (key baked into overlay; boot-time key fetch
  disabled so it can't be clobbered).

## Use
```sh
ssh -i ~/.ssh/tcam_optiplex root@<machine-ip>
lsusb                                   # confirm both dongles enumerated
/opt/tcam/venv/bin/python /opt/tcam/wake.py --scan
/opt/tcam/venv/bin/python /opt/tcam/wake.py          # wake + get AP creds
/opt/tcam/join-cam.sh <SSID> <PWD>      # join cam AP on the wlan dongle only
```

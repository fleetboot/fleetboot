#!/usr/bin/env python3
"""
Wake a TrailCam Go (com.xlink.trailcamgo, XLink/arlink) WiFi trail camera over BLE and
recover the WiFi soft-AP credentials it hands back. Reimplements DevConnectDialog's
wake sequence from the decompiled app. See PROTOCOL.md.

Requires: pip install bleak   (Linux: BlueZ)

Usage:
    python3 wake.py                 # scan and wake the first camera found
    python3 wake.py --mac AA:BB:..  # target a specific BLE MAC
    python3 wake.py --scan          # just list candidate devices and exit
"""
import argparse
import asyncio
import json
import struct

from bleak import BleakClient, BleakScanner

SERVICE_UUID = "00008801-0000-1000-8000-00805f9b34fb"
WRITE_UUID   = "00000002-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID  = "00000003-0000-1000-8000-00805f9b34fb"

MAGIC = b"\x13\x57"
MSG_ID_NETWORK_START = 1
WAKE_CMD = bytes([0x13, 0x57, 0x01, 0, 0, 0, 0, 0])  # magic, msgId=1, len=0

RET_MEANING = {0: "success", -1: "battery low", -2: "wifi failed",
               -4: "powered off", -100: "unknown error"}


def parse_frames(buf: bytearray):
    """Yield (msg_id, payload_bytes) for every complete 13 57 frame in buf, and
    return the number of bytes consumed so the caller can trim the buffer."""
    i, consumed = 0, 0
    while len(buf) - i >= 8:
        if buf[i] != 0x13 or buf[i + 1] != 0x57:
            i += 1
            consumed = i
            continue
        msg_id = struct.unpack_from("<H", buf, i + 2)[0]
        length = struct.unpack_from("<I", buf, i + 4)[0]
        if 8 + length > len(buf) - i:
            break  # body not fully arrived yet
        payload = bytes(buf[i + 8:i + 8 + length])
        yield msg_id, payload
        i += 8 + length
        consumed = i
    return consumed


async def find_camera(mac=None, timeout=12.0):
    print(f"Scanning for camera (service {SERVICE_UUID[:8]}...)...")
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    hits = []
    for dev, adv in devices.values():
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        is_cam = SERVICE_UUID.lower() in uuids
        if mac and dev.address.lower() == mac.lower():
            return dev
        if is_cam or (dev.name and "cam" in dev.name.lower()):
            hits.append((dev, is_cam))
    hits.sort(key=lambda h: not h[1])  # service-advertising devices first
    for dev, is_cam in hits:
        tag = " [advertises camera service]" if is_cam else ""
        print(f"  {dev.address}  {dev.name or '(no name)'}{tag}")
    return hits[0][0] if hits else None


async def wake(mac=None, scan_only=False):
    dev = await find_camera(mac)
    if scan_only:
        return
    if not dev:
        print("No camera found. Make sure the camera slider is in SETUP/ON.")
        return

    print(f"\nConnecting to {dev.address} ({dev.name})...")
    result = {}
    buf = bytearray()
    got = asyncio.Event()

    def on_notify(_handle, data: bytearray):
        buf.extend(data)
        for msg_id, payload in parse_frames(buf):
            if msg_id == MSG_ID_NETWORK_START:
                try:
                    result.update(json.loads(payload.decode("utf-8", "replace")))
                except json.JSONDecodeError:
                    result["_raw"] = payload.hex()
                got.set()
        _retrim(buf)  # drop complete leading frames so partials can reassemble

    async with BleakClient(dev) as client:
        await client.start_notify(NOTIFY_UUID, on_notify)
        print(f"Writing wake command: {WAKE_CMD.hex(' ')}")
        # Char 0x0002 is write-WITH-response only (no write-without-response
        # property). A no-response Write Command PDU is silently ignored by
        # the camera; it must be a Write Request. Confirmed on real hardware
        # (TC100): response=False -> no reply; response=True -> AP creds returned.
        await client.write_gatt_char(WRITE_UUID, WAKE_CMD, response=True)
        print("Waiting for camera to bring up WiFi (up to 45s)...")
        try:
            await asyncio.wait_for(got.wait(), timeout=45)
        except asyncio.TimeoutError:
            print("Timed out waiting for WiFi info notification.")
            return
        await client.stop_notify(NOTIFY_UUID)

    ret = result.get("ret", 0)
    print(f"\nCamera responded: ret={ret} ({RET_MEANING.get(ret, '?')})")
    if ret == 0 and result.get("ssid"):
        print(f"  WiFi SSID : {result['ssid']}")
        print(f"  WiFi PASS : {result.get('pwd')}")
        print(f"  BSSID     : {result.get('bssid')}")
        print("\nNext: join that SSID, then talk to the camera over WiFi (arlink). "
              "Camera is the AP gateway (e.g. 192.168.43.1). See PROTOCOL.md.")
    else:
        print(f"  Full payload: {result}")


def _retrim(buf: bytearray):
    """Drop all complete leading 13 57 frames from buf in place."""
    i = 0
    while len(buf) - i >= 8 and buf[i] == 0x13 and buf[i + 1] == 0x57:
        length = struct.unpack_from("<I", buf, i + 4)[0]
        if 8 + length > len(buf) - i:
            break
        i += 8 + length
    del buf[:i]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Wake a TrailCam Go camera over BLE.")
    ap.add_argument("--mac", help="target BLE MAC address")
    ap.add_argument("--scan", action="store_true", help="list candidates and exit")
    args = ap.parse_args()
    asyncio.run(wake(mac=args.mac, scan_only=args.scan))

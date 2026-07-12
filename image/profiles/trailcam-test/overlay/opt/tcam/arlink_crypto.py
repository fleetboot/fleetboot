"""
arlink command-payload crypto for TrailCam Go cameras (XLink/arlink, libArLink.so).

Recovered by static RE of libArLink.so (see PROTOCOL.md):
  cipher  = AES-128-CBC
  key     = b"xs38nul7cqf7m1va"   (hardcoded @ VA 0x1346c)
  iv      = 16 zero bytes
  padding = zero-pad plaintext up to a 16-byte multiple

Used by ArLinkApi.sendCommand() to encrypt the JSON {"cmdId":...} messages, and
on the decrypt side for responses / login / file-download frames (same key).

Requires: pycryptodome  (pip install pycryptodome)
"""
from Crypto.Cipher import AES

KEY = b"xs38nul7cqf7m1va"   # 16 bytes -> AES-128
IV = b"\x00" * 16


def _pad_zero(data: bytes) -> bytes:
    if len(data) % 16:
        data += b"\x00" * (16 - (len(data) % 16))
    return data


def encrypt_cmd(plaintext: bytes) -> bytes:
    """Encrypt an arlink command payload (e.g. a JSON cmd string, as bytes)."""
    return AES.new(KEY, AES.MODE_CBC, IV).encrypt(_pad_zero(plaintext))


def decrypt_cmd(ciphertext: bytes) -> bytes:
    """Decrypt an arlink payload. Trailing zero padding is stripped; callers that
    need the exact length should use the length field from the frame header."""
    if len(ciphertext) % 16:
        raise ValueError(f"ciphertext not a multiple of 16: {len(ciphertext)}")
    return AES.new(KEY, AES.MODE_CBC, IV).decrypt(ciphertext).rstrip(b"\x00")


if __name__ == "__main__":
    import json
    # Round-trip self-test (validates the implementation without camera data).
    msg = json.dumps({"cmdId": 768}).encode()  # GET_MEDIA_LIST
    ct = encrypt_cmd(msg)
    assert len(ct) % 16 == 0
    assert decrypt_cmd(ct) == msg, "round-trip failed"
    # Known-answer vector so a future reimpl can be checked byte-for-byte:
    ka = encrypt_cmd(b"{\"cmdId\":0}")
    print("round-trip OK")
    print("key      :", KEY.decode())
    print("sample pt:", msg.decode())
    print("sample ct:", ct.hex())
    print('KAT enc("{\\"cmdId\\":0}") =', ka.hex())

"""End-to-end image smoke test: boot the built squashfs in QEMU UEFI and
assert the reporter calls home. Driven by `make image-smoke`.

The building blocks here (stub server, QEMU command) are unit-tested under
`make test`; the orchestrator `run_image_smoke.py` is the slow end-to-end
runner.
"""

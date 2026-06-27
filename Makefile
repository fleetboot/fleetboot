# Fleetboot — single entry point for the test gate.
# Per CLAUDE.md, the full suite must pass before any change is considered done.

PYTHON ?= python3
DEBOS  ?= debos

# Where debos drops the squashfs, kernel, and initrd.
BUILD_DIR ?= build
RECIPE    ?= image/fleetboot-base.yaml

# Default image architecture. Override with `make image ARCH=arm64`.
ARCH ?= amd64

.PHONY: test
test:
	$(PYTHON) -m pytest -q

# Cross-project end-to-end: spin up fleetboot + tftpjail and drive the
# real boot-policy wire (TFTP + HTTP). Requires tftpjail checked out at
# ../tftpjail. Not part of `make test`.
.PHONY: functional-test
functional-test:
	$(PYTHON) -m pytest tests/functional -v -o addopts=

.PHONY: lint
lint:
	$(PYTHON) -m compileall -q fleetboot tests

# Build the example squashfs (slow — not part of the test gate).
# Produces $(BUILD_DIR)/fleetboot-$(ARCH).squashfs plus kernel and initrd.
#
# Debos runs inside fakemachine (a lightweight QEMU VM) for build isolation.
# fakemachine only bind-mounts paths under the recipe's parent directory, so
# we stage the reporter Python package into image/ before the build. The
# staged copy is rebuilt every run and removed by `make clean`.
.PHONY: image
image: stage-fleetboot-package
	mkdir -p $(BUILD_DIR)
	$(DEBOS) \
	  --artifactdir=$(BUILD_DIR) \
	  --template-var=architecture:$(ARCH) \
	  $(RECIPE)

.PHONY: stage-fleetboot-package
stage-fleetboot-package:
	rm -rf image/fleetboot_pkg
	cp -r fleetboot image/fleetboot_pkg

# Boot the built image in a QEMU UEFI guest and assert it reports network_up
# to a stub server. Slow — explicit, not run by `make test`.
.PHONY: image-smoke
image-smoke:
	$(PYTHON) -m tests.smoke.run_image_smoke \
	  --build-dir=$(BUILD_DIR) \
	  --arch=$(ARCH)

# Wipe build artifacts and staged inputs.
.PHONY: clean
clean:
	rm -rf $(BUILD_DIR) image/fleetboot_pkg

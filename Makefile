# Fleetboot — single entry point for the test gate.
# Per CLAUDE.md, the full suite must pass before any change is considered done.

PYTHON ?= python3
DEBOS  ?= debos

# Where debos drops the squashfs, kernel, and initrd.
BUILD_DIR ?= build
RECIPE    ?= image/fleetboot-base.yaml

# Default image architecture. Override with `make image ARCH=arm64`.
ARCH ?= amd64

# Default profile. Override with `make image PROFILE=school`. Profiles
# live under image/profiles/<name>/.
PROFILE ?= default

.PHONY: test
test:
	$(PYTHON) -m pytest -q

# Cross-project end-to-end: spin up fleetboot + tftpjail and drive the
# real boot-policy wire (TFTP + HTTP). Requires tftpjail checked out at
# ../tftpjail. Not part of `make test`.
.PHONY: functional-test
functional-test:
	$(PYTHON) -m pytest tests/functional -v -o addopts=

# Full PXE chain through a real VBox UEFI guest. Slow (UEFI cold boot +
# DHCP timing), needs VBoxManage, sg vboxusers for /dev/vboxdrv access,
# and Python granted cap_net_bind_service for UDP/69. Not part of
# `make test` or `make functional-test`.
.PHONY: vbox-functional-test
vbox-functional-test:
	sg vboxusers -c '$(PYTHON) -m pytest tests/vbox_functional -v -o addopts='

# Full PXE chain through a QEMU UEFI guest on a libvirt-managed isolated
# bridge. The preferred functional path: no VBox, KVM accel, no module
# dance. Requires libvirt-daemon-system, libvirt-clients, qemu-system-x86,
# and matt in the `libvirt` group. Python needs cap_net_bind_service.
.PHONY: qemu-functional-test
qemu-functional-test:
	$(PYTHON) -m pytest tests/qemu_functional -v -o addopts=

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
	  --fakemachine-backend=qemu \
	  --artifactdir=$(BUILD_DIR) \
	  --template-var=architecture:$(ARCH) \
	  --template-var=profile:$(PROFILE) \
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

# Build the chainload GRUB EFI binary that VBox / real UEFI fetches over
# TFTP as the very first boot stage. Its embedded config fetches the per-MAC
# config from tftpjail and chainloads it. Cheap to (re)build; we ship it
# alongside vmlinuz / initrd.img in build/.
.PHONY: grub-binary
grub-binary: $(BUILD_DIR)/grubnetx64.efi

$(BUILD_DIR)/grubnetx64.efi: image/grub-embedded.cfg
	mkdir -p $(BUILD_DIR)
	grub-mkimage \
	  --format=x86_64-efi \
	  --output=$@ \
	  --prefix='(tftp,$$pxe_default_server)/' \
	  --config=$< \
	  efinet tftp http normal linux configfile \
	  smbios search echo serial terminal net regexp

# Wipe build artifacts and staged inputs.
.PHONY: clean
clean:
	rm -rf $(BUILD_DIR) image/fleetboot_pkg

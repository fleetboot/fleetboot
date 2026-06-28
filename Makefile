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

# Long-running dev server: fleetboot + tftpjail with the dashboard, talking
# to a persistent registry at build/dev/machines.sqlite. Listens on
# 0.0.0.0:8080. Browser: http://localhost:8080/dashboard.
.PHONY: run-server
run-server:
	$(PYTHON) -m tests.dev.run_server

# Boot a transient QEMU UEFI VM that registers against the running dev
# server, so it appears on the dashboard as it ticks through boot states.
# Run alongside `make run-server`.
.PHONY: boot-dev-vm
boot-dev-vm:
	sg libvirt -c 'cd $(CURDIR) && $(PYTHON) -m tests.dev.boot_dev_vm $(ARGS)'

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
# Each squashfs gets a unique BUILD_VERSION stamp written into
# /etc/fleetboot/build-version inside the image, and into a sidecar at
# $(BUILD_DIR)/fleetboot-$(PROFILE)-$(ARCH).version. The dashboard reads
# the sidecar to know "latest"; the booted machine reports the in-image
# value back via /status. Mismatch => orange row, time to reboot.
BUILD_VERSION := $(shell date -u +%Y-%m-%dT%H:%M:%SZ)

.PHONY: image
image: stage-fleetboot-package
	mkdir -p $(BUILD_DIR)
	$(DEBOS) \
	  --memory=4Gb \
	  --scratchsize=8Gb \
	  --artifactdir=$(BUILD_DIR) \
	  --template-var=architecture:$(ARCH) \
	  --template-var=profile:$(PROFILE) \
	  --template-var=build_version:$(BUILD_VERSION) \
	  $(RECIPE)
	echo "$(BUILD_VERSION)" > $(BUILD_DIR)/fleetboot-$(PROFILE)-$(ARCH).version

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

# Stage Debian's signed shim + signed grub binaries for Secure Boot PXE.
#
# DHCP advertises `shimx64.efi.signed` as the bootfile. UEFI loads shim
# (trusted via Microsoft's UEFI CA in firmware), shim chainloads
# `grubx64.efi` from the same TFTP path, and the signed grub looks for
# `grub/grub.cfg` next — which we serve from `image/signed-boot/`.
# That initial grub.cfg then `configfile`s to tftpjail's per-MAC dynamic
# config exactly as our self-built `grubnetx64.efi` does today.
#
# Requires `shim-signed` and `grub-efi-amd64-signed` on the build host.
SHIM_SOURCE  ?= /usr/lib/shim/shimx64.efi.signed
GRUB_SOURCE  ?= /usr/lib/grub/x86_64-efi-signed/grubnetx64.efi.signed

.PHONY: signed-boot-assets
signed-boot-assets: $(BUILD_DIR)/shimx64.efi.signed \
                    $(BUILD_DIR)/grubx64.efi \
                    $(BUILD_DIR)/grub/grub.cfg

$(BUILD_DIR)/shimx64.efi.signed: $(SHIM_SOURCE)
	mkdir -p $(BUILD_DIR)
	cp $< $@

# Shim looks for the chained loader as `grubx64.efi` in the same dir, so
# we rename the signed network grub to that filename when staging.
$(BUILD_DIR)/grubx64.efi: $(GRUB_SOURCE)
	mkdir -p $(BUILD_DIR)
	cp $< $@

$(BUILD_DIR)/grub/grub.cfg: image/signed-boot/initial-grub.cfg
	mkdir -p $(BUILD_DIR)/grub
	cp $< $@

# Wipe build artifacts and staged inputs.
.PHONY: clean
clean:
	rm -rf $(BUILD_DIR) image/fleetboot_pkg

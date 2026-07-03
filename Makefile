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

# Debian release the image is based on. Override with
# `make image SUITE=bookworm`. The recipe has the same default.
SUITE ?= trixie

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

# Bring up fleetboot + tftpjail via docker compose. This is the
# same command a fresh user runs — production and local dev share
# one path (see docker-compose.yml + .env.example). Volumes bind
# `build/` and `image/profiles/` into both containers so make image
# on the host, and profile edits from the dashboard, are picked up
# without a container restart.
#
#   cp .env.example .env  # first time only
#   make run-server
#
# Foreground with logs. Ctrl-C to stop; `make down` to tear down.
.PHONY: run-server
run-server:
	docker compose up --build

.PHONY: down
down:
	docker compose down

# Legacy: single-process dev harness that predates the docker-compose
# path. Kept for the vbox / qemu functional tests that already know
# how to talk to it. Prefer `make run-server` for interactive dev.
.PHONY: run-server-inprocess
run-server-inprocess:
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

# Leave one CPU for the host so `make image` doesn't peg every core
# while the dev server + dashboard are still responsive. Falls back to
# 1 CPU on a single-core host (or where nproc is missing).
IMAGE_CPUS := $(shell n=$$(nproc 2>/dev/null || echo 2); if [ "$$n" -gt 1 ]; then echo $$((n - 1)); else echo 1; fi)

.PHONY: image
image: stage-fleetboot-package resolve-profile
	mkdir -p $(BUILD_DIR)
	$(DEBOS) \
	  --memory=4Gb \
	  --scratchsize=8Gb \
	  --cpus=$(IMAGE_CPUS) \
	  --artifactdir=$(BUILD_DIR) \
	  --template-var=architecture:$(ARCH) \
	  --template-var=profile:$(PROFILE) \
	  --template-var=suite:$(SUITE) \
	  --template-var=build_version:$(BUILD_VERSION) \
	  $(RECIPE)
	echo "$(BUILD_VERSION)" > $(BUILD_DIR)/fleetboot-$(PROFILE)-$(ARCH).version

.PHONY: stage-fleetboot-package
stage-fleetboot-package:
	rm -rf image/fleetboot_pkg
	cp -r fleetboot image/fleetboot_pkg

# Resolve the profile inheritance chain into image/profiles_resolved/
# so debos can pick up a single staged directory. Fast (Python, fs ops).
.PHONY: resolve-profile
resolve-profile:
	$(PYTHON) scripts/resolve-profile.py $(PROFILE)

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
grub-binary: $(BUILD_DIR)/fleetboot-x64-uefi $(BUILD_DIR)/fleetboot-x86-bios

# BIOS PXE bootfile. Older x86 hardware (pre-2012ish OptiPlexes etc.)
# only does legacy BIOS PXE — UEFI binaries won't execute. The same
# embedded.cfg works (`$grub_cpu` evaluates to `i386` and
# `$grub_platform` to `pc` in BIOS GRUB, so the per-MAC config request
# becomes `/jail/<mac>/i386/pc`). tftpjail's identity parser accepts
# that shape; the renderer maps the machine's registered architecture
# (e.g. x86_64) to the right squashfs.
#
# DHCP needs to advertise this filename to ARCH=0 (Intel x86PC) clients
# and fleetboot-x64-uefi to ARCH=7/9 (EFI x64) clients — see admin
# guide's "DHCP — pointing clients at fleetboot" section.
$(BUILD_DIR)/fleetboot-x86-bios: image/grub-embedded.cfg
	mkdir -p $(BUILD_DIR)
	grub-mkimage \
	  --format=i386-pc-pxe \
	  --output=$@ \
	  --prefix='(tftp,$$pxe_default_server)/' \
	  --config=$< \
	  pxe tftp http normal linux configfile \
	  smbios search echo serial terminal net regexp cat

# Bootfile name is fleetboot-branded so a `tcpdump tftp` capture or a
# next-server inspect makes it obvious which fleet this client is asking
# for. arm64 will get its own fleetboot-arm64-uefi.efi target.
#
# We also drop a `.efi` symlink alongside. Some UEFI PXE ROMs (and many
# admin habits) expect the extension; tftpjail follows the symlink and
# serves the same bytes, so DHCP can advertise either name.
$(BUILD_DIR)/fleetboot-x64-uefi: image/grub-embedded.cfg
	mkdir -p $(BUILD_DIR)
	grub-mkimage \
	  --format=x86_64-efi \
	  --output=$@ \
	  --prefix='(tftp,$$pxe_default_server)/' \
	  --config=$< \
	  efinet tftp http normal linux configfile \
	  smbios search echo serial terminal net regexp cat
	ln -sf fleetboot-x64-uefi $(BUILD_DIR)/fleetboot-x64-uefi.efi

# Stage Debian's signed shim + signed grub binaries for Secure Boot PXE.
#
# DHCP advertises `fleetboot-x64-uefi-signed` as the bootfile. UEFI
# loads shim (trusted via Microsoft's UEFI CA in firmware), shim
# chainloads `grubx64.efi` from the same TFTP path (this filename is
# baked into shim so we keep it), and the signed grub looks for
# `grub/grub.cfg` next — which we serve from `image/signed-boot/`.
# That initial grub.cfg then `configfile`s to tftpjail's per-MAC dynamic
# config exactly as our self-built `fleetboot-x64-uefi` does today.
#
# Requires `shim-signed` and `grub-efi-amd64-signed` on the build host.
SHIM_SOURCE  ?= /usr/lib/shim/shimx64.efi.signed
GRUB_SOURCE  ?= /usr/lib/grub/x86_64-efi-signed/grubnetx64.efi.signed

.PHONY: signed-boot-assets
signed-boot-assets: $(BUILD_DIR)/fleetboot-x64-uefi-signed \
                    $(BUILD_DIR)/grubx64.efi \
                    $(BUILD_DIR)/grub/grub.cfg

$(BUILD_DIR)/fleetboot-x64-uefi-signed: $(SHIM_SOURCE)
	mkdir -p $(BUILD_DIR)
	cp $< $@
	ln -sf fleetboot-x64-uefi-signed $(BUILD_DIR)/fleetboot-x64-uefi-signed.efi

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
	rm -rf $(BUILD_DIR) image/fleetboot_pkg image/profiles_resolved

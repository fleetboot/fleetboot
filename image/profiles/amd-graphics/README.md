# `amd-graphics` profile

Mix-in profile that adds the userspace Mesa stack and AMD GPU firmware.
The `amdgpu` kernel driver itself ships with `linux-image` by default;
this profile fills in everything around it so a discrete or integrated
Radeon card lights up properly.

Designed to **stack with a desktop profile**, not stand alone:

```
image/profiles/lab-amd/parent
    xfce-desktop
    amd-graphics
```

That gives you "XFCE desktop on machines that have AMD GPUs".

The resolver de-duplicates shared ancestors, so if `xfce-desktop`
already inherits `amd-graphics` in its own parent chain, the
`amd-graphics` line above is redundant but harmless — the resolver
notices and includes it exactly once.

```sh
make image PROFILE=lab-amd
```

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

That gives you "XFCE desktop on machines that have AMD GPUs". Combine
with `school` instead for a LibreWolf-bundled student image:

```
image/profiles/school-amd/parent
    school
    amd-graphics
```

The resolver de-duplicates the chain, so `school -> xfce-desktop` plus
`amd-graphics` ends up as `xfce-desktop -> amd-graphics -> school -> ...`
without `xfce-desktop` showing up twice.

```sh
make image PROFILE=school-amd
```

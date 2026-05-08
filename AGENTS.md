# docker-pkg-build — Agent Guidelines

## Purpose

`docker-pkg-build` is a toolset that wraps `sbuild` and `gbp` inside Docker containers to build
Debian packages for ARM64 targets with a one-liner, without requiring the user to understand
chroots, schroot, or sbuild internals.

## Repository Layout

```
docker_deb_build.py        # Main entry-point script (the "one-liner" wrapper)
create_data_tar.py         # Helper used internally
color_logger.py            # Logging utilities
Dockerfiles/
  Dockerfile.<os>.<distro>         # One Dockerfile per target distro
  base-packages.txt                # Packages installed in the Docker image layer
  extra-packages.txt               # Packages installed inside the build chroot
  keyrings/
    qsc-deb-releases.asc           # Qualcomm APT repo PGP public key
  sources/
    <distro>/qsc-deb-releases.sources  # Pre-configured Qualcomm APT source entry per distro
```

## Supported Distros

| Suite     | OS     | sbuild backend | Chroot format |
|-----------|--------|----------------|---------------|
| noble     | Ubuntu | schroot        | `/srv/chroot/noble` (sbuild-createchroot) |
| questing  | Ubuntu | schroot        | `/srv/chroot/questing` (sbuild-createchroot) |
| resolute  | Ubuntu | unshare        | `/root/.cache/sbuild/resolute-arm64.tar` (mmdebstrap) |
| trixie    | Debian | schroot        | `/srv/chroot/trixie` (sbuild-createchroot) |
| sid       | Debian | unshare        | `/root/.cache/sbuild/sid-arm64.tar` (mmdebstrap) |

## Key Design Decisions

- **schroot vs. unshare**: Newer sbuild versions (resolute, sid) default to the unshare backend,
  which expects a tarball at `/root/.cache/sbuild/<distro>-<arch>.tar`, not a `/srv/chroot/`
  directory. Dockerfiles must use `mmdebstrap --format=tar` for these distros.
- **CA certificates in chroot**: The chroot tarball must include `ca-certificates` and `openssl`
  so that HTTPS APT repositories work inside the chroot at build time.
- **Qualcomm APT key**: All chroots have the Qualcomm PGP key pre-installed at
  `/etc/apt/keyrings/qsc-deb-releases.asc` and a matching `.sources` file so that packages from
  `qartifactory-edge.qualcomm.com` are available without `trusted=yes` or `--extra-repo`.

## Common Commands

```bash
# Build a package for a specific distro
docker_deb_build.py -s <source-dir> -o <output-dir> -d <distro>

# Rebuild the Docker images (after changing a Dockerfile)
docker_deb_build.py -d <distro> --rebuild

# Pass an additional APT repo at build time
docker_deb_build.py -s <source-dir> -o <output-dir> -d <distro> \
  -e "deb [arch=arm64 signed-by=/etc/apt/keyrings/qsc-deb-releases.asc] https://... <suite> main"
```

## When Editing Dockerfiles

- Changes to `base-packages.txt` or `extra-packages.txt` affect **all** distros.
- Changes to `keyrings/` or `sources/` affect **all** distros — check every `.sources` file if
  updating suite names.
- After any Dockerfile change, the corresponding Docker image must be rebuilt with `--rebuild`.
- **resolute and sid**: any change to the chroot content requires updating the `mmdebstrap`
  `--customize-hook` or `--include` flags — not a post-build `cp` into `/srv/chroot/`.

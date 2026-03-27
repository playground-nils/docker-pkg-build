#!/usr/bin/env python3

# Copyright (c) Qualcomm Technologies, Inc. and/or its subsidiaries.
#
# SPDX-License-Identifier: BSD-3-Clause-Clear

"""
docker_deb_build.py

Helper script to build a debian package using the container from the Dockerfile in the docker/ folder.
"""

import os
import sys
import argparse
import subprocess
import traceback
import platform
import shutil
import urllib.request
import glob
import grp
import pwd
import getpass

from color_logger import logger

# Docker image name template
# suite_name: 'noble', 'questing', 'sid'
# Example: ghcr.io/qualcomm-linux/pkg-builder:noble
DOCKER_IMAGE_NAME_FMT = "ghcr.io/qualcomm-linux/pkg-builder:{suite_name}"

def _discover_available_distros() -> list:
    """
    Identify supported debian-based distros.
    Search for Dockerfiles that derive from a debian or ubuntu
    base image, and use their names to create the list of supported
    distributions for this build tool.
    """
    import re, os
    docker_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Dockerfiles")
    distros = set()
    if os.path.isdir(docker_dir):
        for entry in os.listdir(docker_dir):
            parts = entry.split(".")
            if parts[0] and parts[0] == "Dockerfile":
                distro = ".".join(parts[2:])
                dockerfile_path = os.path.join(docker_dir, entry)
                try:
                    with open(dockerfile_path, "r", errors="ignore") as f:
                        for line in f:
                            line = line.strip().lower()
                            if line.startswith("from"):
                                # Check for known debian‑based base images
                                if any(keyword in line for keyword in ("debian", "ubuntu")):
                                    distros.add(distro)
                                    break
                except Exception:
                    # If the Dockerfile cannot be read, skip it
                    continue
    return sorted(distros)


def parse_arguments() -> argparse.Namespace:
    """
    Parse command line arguments for the script.

    Returns:
        argparse.Namespace: Parsed arguments.
    """
    parser = argparse.ArgumentParser(description="Build a debian package inside a docker container.")

    parser.add_argument("-n", "--no-update-check",
                        default=False,
                        required=False,
                        action='store_true',
                        help="Bypass the remote update check and allow running with an out-of-date repo.")

    parser.add_argument("-s", "--source-dir",
                        required=False,
                        default=None,
                        help="Path to the source directory containing the debian package source.")

    parser.add_argument("-o", "--output-dir",
                        required=False,
                        default=None,
                        help="Path to the output directory for the built package.")

    parser.add_argument("-d", "--distro",
                        type=str,
                        choices=['noble', 'questing', 'resolute', 'trixie', 'sid'],
                        default=None,
                        help="The target distribution for the package build (or rebuild if --rebuild is used). If not specified with --rebuild, all distros will be rebuilt.")

    parser.add_argument("-l", "--run-lintian",
                        action='store_true',
                        help="Run lintian on the package.")

    parser.add_argument("-e", "--extra-repo",
                        type=str,
                        action='append',
                        default=[],
                        help="Additional APT repository to include. Can be specified multiple times. Example: 'deb [arch=arm64 trusted=yes] http://pkg.qualcomm.com noble/stable main'")
                        
    parser.add_argument("-p", "--extra-package",
                        type=str,
                        action='append',
                        default=[],
                        help="Additional .deb file or directory to install inside the build chroot. Can be specified multiple times.")

    parser.add_argument("-r", "--rebuild",
                        action='store_true',
                        help="Force rebuild of the Docker image and exit.")

    parser.add_argument("-k", "--skip-gbp",
                        action='store_true',
                        default=False,
                        help="When building with Quilt format, skip GBP (Git Build Package) for the creation of the .orig tarball and manually handle the source package creation.")

    args = parser.parse_args()

    # Validate argument combinations
    if args.rebuild:
        # In rebuild mode, source-dir and output-dir should not be specified
        if args.source_dir is not None:
            raise Exception("--source-dir cannot be used with --rebuild mode")
        if args.output_dir is not None:
            raise Exception("--output-dir cannot be used with --rebuild mode")
        if args.run_lintian:
            raise Exception("--run-lintian cannot be used with --rebuild mode")
        if args.extra_repo:
            raise Exception("--extra-repo cannot be used with --rebuild mode")
        if args.extra_package:
            raise Exception("--extra-package cannot be used with --rebuild mode")
        if args.skip_gbp:
            raise Exception("--skip-gbp cannot be used with --rebuild mode")

    else:
        # In build mode, apply defaults for source-dir, output-dir, and distro if not specified
        if args.source_dir is None:
            args.source_dir = "."
        if args.output_dir is None:
            args.output_dir = ".."
        if args.distro is None:
            raise Exception("--distro is required in build mode (when --rebuild is not used)")

    return args

def check_docker_dependencies(timeout: int = 20) -> bool:
    """
    Verify docker CLI presence, daemon accessibility, and user permission to talk to the daemon.
    """

    # 1) docker binary present
    if shutil.which("docker") is None:
        raise Exception("docker CLI not found. Install Docker: https://docs.docker.com/get-docker/")

    # 2) try contacting the daemon
    try:
        p = subprocess.run(["docker", "info"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           check=True, timeout=timeout)
        logger.info("Docker CLI and daemon reachable.")
        return True
        
    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"").decode(errors="ignore") + (e.stdout or b"").decode(errors="ignore")
        err_l = err.lower()
        sock = "/var/run/docker.sock"

        # permission issue -> check group on the socket
        if "permission denied" in err_l or "access denied" in err_l or "cannot connect to the docker daemon" in err_l:
            if os.path.exists(sock):
                st = os.stat(sock)
                try:
                    sock_group = grp.getgrgid(st.st_gid).gr_name
                except KeyError:
                    sock_group = f"gid:{st.st_gid}"
                user = getpass.getuser()
                # gather groups for user
                user_groups = [g.gr_name for g in grp.getgrall() if user in g.gr_mem]
                primary_gid = pwd.getpwnam(user).pw_gid
                try:
                    primary_group = grp.getgrgid(primary_gid).gr_name
                    user_groups.append(primary_group)
                except KeyError:
                    pass

                if sock_group not in user_groups:
                    raise Exception(
                        f"Permission denied accessing Docker socket ({sock}). Current user '{user}' is not in the socket group '{sock_group}'.\n"
                        f"Add the user to the group: \"sudo usermod -aG {sock_group} $USER\"  (then re-login) or run the script with sudo.\n"
                        f"Also, to avoid having to do a complete logout/login, you can run: \"newgrp {sock_group}\" which will start a new shell with the new group applied."
                    )
                else:
                    # user is in group but still cannot connect -> daemon likely stopped
                    raise Exception(
                        "Docker socket exists and group membership OK, but 'docker info' failed. Is the Docker daemon running?\n"
                        "Try: sudo systemctl start docker  (or check your platform's docker service)."
                    )
            else:
                raise Exception(
                    "Cannot contact Docker daemon and /var/run/docker.sock does not exist. Is the Docker engine installed and running?\n"
                    "Try: sudo systemctl start docker"
                )
        else:
            # generic failure
            raise Exception(f"Failed to contact Docker daemon: {err.strip() or e}")

    except subprocess.TimeoutExpired:
        raise Exception("Timed out while trying to contact the Docker daemon. Is it running?")

def build_docker_image(distro: str) -> bool:
    """
    Build a Docker image from the local Dockerfile.

    Args:
        distro (str): The distribution (e.g., 'noble', 'questing').

    Returns:
        bool: True if the build succeeded, False otherwise.

    Raises:
        Exception: If the build fails or times out.
    """

    this_script_dir = os.path.dirname(os.path.abspath(__file__))
    docker_dir = os.path.normpath(os.path.join(this_script_dir, 'Dockerfiles'))
    context_dir = docker_dir
    # Find the Dockerfile matching the pattern Dockerfile.*.{distro}
    pattern = f"Dockerfile.*.{distro}"
    matches = glob.glob(os.path.join(docker_dir, pattern))
    if not matches:
        raise Exception(f"No Dockerfile found matching pattern: {pattern} in {docker_dir}")
    dockerfile_name = os.path.basename(matches[0])
    dockerfile_path = os.path.join(docker_dir, dockerfile_name)

    image_name = DOCKER_IMAGE_NAME_FMT.format(suite_name=distro)

    logger.debug(f"Building docker image '{image_name}' from Dockerfile: {dockerfile_path}")
    
    if not os.path.exists(dockerfile_path):
        logger.error(f"No local Dockerfile found for distro '{distro}' at expected path: {dockerfile_path}. Cannot build image '{image_name}'.")
        return False

    build_cmd = ["docker", "build", "-t", image_name, "-f", dockerfile_path, context_dir]

    logger.debug(f"Running: {' '.join(build_cmd)}")

    # Stream build output live so the user sees progress
    try:
        proc = subprocess.Popen(build_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1, text=True)
        try:
            for line in proc.stdout:
                # print to terminal immediately
                sys.stdout.write(line)
                sys.stdout.flush()

            rc = proc.wait()

        except KeyboardInterrupt:
            proc.terminate()
            proc.wait()
            raise

        if rc != 0:
            raise Exception(f"Failed to build docker image from {dockerfile_path} (exit {rc}).")

        logger.info(f"Successfully built image '{image_name}'.")
        return True
    except subprocess.TimeoutExpired:
        proc.kill()
        raise Exception(f"Timed out while building docker image from {dockerfile_path}.")

def rebuild_docker_images(distro: str = None) -> None:
    """
    Force rebuild of the containers from Dockerfiles folder.
    If distro is specified, rebuild only that specific distro. Otherwise, rebuild all distros.
    """

    docker_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Dockerfiles')

    if distro:
        # Rebuild only the specified distro
        dockerfile_glob = os.path.join(docker_dir, f'Dockerfile.*.{distro}')
        dockerfiles = sorted(glob.glob(dockerfile_glob))
        if not dockerfiles:
            raise Exception(f"No Dockerfile found for distro={distro}")
        logger.info(f"Rebuilding docker image for {distro}: {dockerfiles}")
    else:
        # Rebuild all available debian-based distros
        dockerfiles = []
        for distro in _discover_available_distros():
            dockerfile_glob = os.path.join(docker_dir, f'Dockerfile.*.*{distro}')
            dockerfiles.extend(glob.glob(dockerfile_glob))
        dockerfiles = sorted(dockerfiles)
        logger.info(f"Rebuilding all docker images: {dockerfiles}")

    for dockerfile in dockerfiles:
        suite_name = os.path.basename(dockerfile).split('.')[-1]
        image_name = DOCKER_IMAGE_NAME_FMT.format(suite_name=suite_name)

        logger.debug(f"Rebuilding Docker image '{image_name}' from {dockerfile}...")

        # Delete/purge the current image if it exists
        try:
            subprocess.run(["docker", "image", "rm", "-f", image_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            logger.info(f"Deleted existing image '{image_name}'.")
        except subprocess.CalledProcessError:
            logger.debug(f"No existing image '{image_name}' to delete.")

        build_docker_image(suite_name)

def is_git_repo(source_dir: str) -> bool:
    """
    Return True if the source directory is a git repository.
    """
    return os.path.exists(os.path.join(source_dir, '.git'))

def make_source_pkg_cmd(sbuild_cmd: str) -> str:
    """
    Return a shell command (run inside the container, cwd=/workspace) that:

      1. Reads the package name and version from debian/changelog.
      2. Creates an upstream orig tarball from the source tree, excluding the
         debian/ directory and the .git history (both are not part of upstream).
      3. Runs dpkg-source -b to produce the .dsc + debian tarball.
      4. Passes the resulting .dsc to sbuild for the actual binary build.

    All intermediate files (orig tarball, .dsc, debian.tar.*) are written to
    /workspace (the parent of /workspace/src) so they don't pollute the source
    tree and are accessible to sbuild.

    This path is taken for 3.0 (quilt) packages that are NOT managed by gbp
    (i.e. no debian/gbp.conf).  gbp is still used for proper gbp projects.
    """
    return (
        "set -e; "
        "cd /workspace; "
        # Read package metadata from debian/changelog
        "PKG=$(dpkg-parsechangelog -l /workspace/src/debian/changelog -S Source); "
        "VER=$(dpkg-parsechangelog -l /workspace/src/debian/changelog -S Version); "
        # Strip the debian revision (everything after the last '-') to get the upstream version.
        # e.g. '1-1' -> '1',  '6.12.0-1' -> '6.12.0'
        "UPSTREAM_VER=$(echo \"$VER\" | sed 's/-[^-]*$//'); "
        "ORIG_TAR=\"${PKG}_${UPSTREAM_VER}.orig.tar.gz\"; "
        "echo \"[source-pkg] Package : $PKG  Version: $VER  Upstream: $UPSTREAM_VER\"; "
        # Create the orig tarball — exclude debian/ (packaging overlay) and .git/
        # (version-control history is not part of the upstream source).
        "echo \"[source-pkg] Creating orig tarball: $ORIG_TAR (may take a while for large trees)\"; "
        "tar czf \"$ORIG_TAR\" --exclude=./debian --exclude=./.git -C /workspace/src .; "
        # Build the source package (.dsc + debian.tar.*)
        "echo \"[source-pkg] Running dpkg-source -b ...\"; "
        "dpkg-source -b /workspace/src; "
        # Locate the generated .dsc
        "DSC_FILE=$(ls /workspace/${PKG}_${VER}.dsc 2>/dev/null | head -1); "
        "[ -n \"$DSC_FILE\" ] || { echo \"ERROR: .dsc not found after dpkg-source -b\"; exit 1; }; "
        "echo \"[source-pkg] Source package ready: $DSC_FILE\"; "
        # Hand off to sbuild
        f"{sbuild_cmd} \"$DSC_FILE\""
    )


def build_package_in_docker(image_name: str, source_dir: str, output_dir: str, distro: str, run_lintian: bool, extra_repo: str, extra_package: str, skip_gbp: bool) -> bool:
    """
    Build the debian package inside the given docker image.
    source_dir: path to the debian package source (mounted into the container)
    output_dir: path to the output directory for the built package (mounted into the container)
    distro: target distribution string (e.g. 'noble')
    run_lintian: whether to run lintian on the built package
    extra_repo: list of additional APT repositories to include
    Returns True on success, False on failure.
    """

    # Register the name of the newest build log in the output_dir in case there are leftovers from a previous build
    # So that we can identify if this run produced a newer build log. Sbuild produces .build files with timestamps,
    # and one of them is a symlink to the latest build log.
    build_log_files = glob.glob(os.path.join(output_dir or '.', '*.build'))
    prev_build_log = next((os.readlink(p) for p in build_log_files if os.path.islink(p)), None)
    logger.debug(f"Previous build log: {prev_build_log}")

    # Build the gbp command
    # The --git-builder value is a single string passed to gbp
    extra_repo_option = " ".join(f"--extra-repository='{repo}'" for repo in extra_repo) if extra_repo else ""
    extra_package_option = " ".join(f"--extra-package='{pkg}'" for pkg in extra_package) if extra_package else ""
    lintian_option = '--no-run-lintian' if not run_lintian else ""
    # --no-clean-source: skip dpkg-buildpackage --clean on host (avoids build-dep check outside chroot)
    sbuild_cmd = f"sbuild --no-clean-source --build-dir=/workspace/output --host=arm64 --build=arm64 --dist={distro} {lintian_option} {extra_repo_option} {extra_package_option}"

    # Ensure git inside the container treats the mounted checkout as safe
    git_safe_cmd = "git config --global --add safe.directory /workspace/src"
    gbp_cmd = f"{git_safe_cmd} && gbp buildpackage --git-no-pristine-tar --git-ignore-branch --git-builder=\"{sbuild_cmd}\""

    # Decide which build command to run based on debian/source/format in the source tree.
    # Prefer 'native' -> run sbuild directly. If the source format uses 'quilt', use gbp.
    format_file = os.path.join(source_dir, 'debian', 'source', 'format')
    if not os.path.exists(format_file):
        raise Exception(f"Missing {format_file}: cannot determine source format (native/quilt). Is the source dir correctly pointing to a debian package source tree?")

    try:
        with open(format_file, 'r', errors='ignore') as f:
            fmt = f.read().lower()
    except Exception as e:
        raise Exception(f"Failed to read {format_file}: {e}")

    if 'native' in fmt:
        # Native package: run sbuild directly in the source directory.
        build_cmd = sbuild_cmd
        logger.debug("Source format: native — using sbuild directly")
    elif 'quilt' in fmt:
        if not is_git_repo(source_dir):
            logger.warning(f"Source format is quilt but {source_dir} is not a git repository. This typically means the source tree was copied without the .git history, which is required for quilt format.")
            logger.warning(f"The .dsc generation will be performed manually via dpkg-source, but this may lead to issues with the build if the debian/patches/ directory relies on git history (e.g. for patch naming or series generation). Consider using a git repository with gbp for better support of quilt format.")
            build_cmd = make_source_pkg_cmd(sbuild_cmd)

        else: # git repository present
            if skip_gbp:
                # Non-gbp quilt project (e.g. kernel injected with a debian/ overlay):
                # generate the orig tarball and .dsc via dpkg-source, then pass the
                # .dsc to sbuild so it can build the quilt package correctly.
                build_cmd = make_source_pkg_cmd(sbuild_cmd)
                logger.warning("Skipping gbp buildpackage for quilt source format as per --skip-gbp. Manually generating source package with dpkg-source may lead to issues if the debian/patches/ directory relies on git history. Consider using gbp for better support of quilt format.")

            else:
                # gbp-managed quilt project: use gbp buildpackage to create the orig
                # tarball from git history and drive sbuild.
                build_cmd = gbp_cmd
                logger.debug("Source format: quilt + gbp.conf — using gbp buildpackage")

    else:
        raise Exception(f"Unsupported debian/source/format in {format_file}. Expected to contain 'native' or 'quilt', got: {fmt!r}")

    # Prepare volume mounts for extra packages (files or directories)
    extra_mounts = []
    for pkg_path in extra_package:
        abs_path = os.path.abspath(pkg_path)
        if not os.path.exists(abs_path):
            logger.warning(f"Extra package path does not exist and will be ignored: {pkg_path}")
            continue
        # Mount at the same absolute path inside the container
        extra_mounts.extend(['-v', f"{abs_path}:{abs_path}:Z"])

    docker_cmd = [
        'docker', 'run', '--rm', '--privileged', "-t",
        '-v', f"{source_dir}:/workspace/src:Z",
        '-v', f"{output_dir}:/workspace/output:Z",
        # Insert any extra package mounts
        *extra_mounts,
        '-w', '/workspace/src',
        image_name, 'bash', '-c', build_cmd
    ]

    logger.debug(f"Running build inside container: {' '.join(docker_cmd[:])}")

    try:
        # Run and stream output live
        res = subprocess.run(docker_cmd, check=False)
    except KeyboardInterrupt:
        raise

    if res.returncode == 0:
        logger.info("✅ Successfully built package")
    else:
        logger.error("❌ Build failed")


    build_log_files = glob.glob(os.path.join(output_dir or '.', '*.build'))
    new_build_log = next((os.readlink(p) for p in build_log_files if os.path.islink(p)), None)

    if new_build_log == prev_build_log:
        logger.debug("ℹ️ No new sbuild log produced during this run.")
    else:
        logger.debug(f"ℹ️ New sbuild log available at: {os.path.join(output_dir, new_build_log)}")

    return res.returncode == 0

def check_if_repo_up_to_date() -> None:
    """
    Check if the local docker_deb_build repository is up to date with the remote.
    """

    REMOTE = "https://github.com/qualcomm-linux/docker-pkg-build.git"

    # Find the repo root
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    while not os.path.isdir(os.path.join(repo_dir, ".git")):
        parent = os.path.dirname(repo_dir)
        if parent == repo_dir:
            logger.warning("Not inside a git repository; cannot check for updates.")
            return
        repo_dir = parent

    # Get local HEAD commit hash
    local_head = subprocess.check_output([
        "git", "rev-parse", "HEAD"
    ], cwd=repo_dir, text=True).strip()

    # Fetch remote HEAD commit hash (default branch)
    remote_head = subprocess.check_output([
        "git", "ls-remote", REMOTE, "HEAD"
    ], cwd=repo_dir, text=True).strip().split()[0]

    if local_head != remote_head:
        logger.critical("!"*80)
        logger.critical("Your local docker_deb_build repo is NOT UP TO DATE with the remote!")
        logger.critical(f"  Local HEAD : {local_head}")
        logger.critical(f"  Remote HEAD: {remote_head}")
        logger.critical(f"Please pull the latest changes from {REMOTE}.")
        logger.critical(f"Then re-run this script with --rebuild to rebuild the docker images if needed.")
        logger.warning("To bypass this check, supply the --no-update-check argument.")
        logger.critical("!"*80)
        sys.exit(2)

    else:
        logger.info("The docker-pkg-build repo is up to date with the remote.")


def main() -> None:
    """
    Main entry point of the script.
    - Parses arguments
    - Checks if this repo is up to date
    - Handles containers rebuild
    - Docker preflight checks
    - Builds the package inside the container
    """

    args = parse_arguments()

    logger.debug(f"Print of the arguments: {args}")

    # Determine if the docker-pkg-build repo is up to date with remote
    if not args.no_update_check:
        check_if_repo_up_to_date()

    # Only ARM64 hosts are supported.
    if platform.machine() != "aarch64":
        raise Exception(f"Unsupported host architecture: {platform.machine()}. Only ARM64 (aarch64) hosts are supported.")
    logger.debug("Host architecture: arm64")

    # Verify Docker is available and the current user can talk to the daemon
    check_docker_dependencies()

    # If --rebuild is specified, force rebuild of the docker images and exit
    if args.rebuild:
        rebuild_docker_images(args.distro)
        sys.exit(0)

    # Make sure source and output dirs are absolute paths
    if not os.path.isabs(args.source_dir):
        args.source_dir = os.path.abspath(args.source_dir)
    if not os.path.isabs(args.output_dir):
        args.output_dir = os.path.abspath(args.output_dir)

    logger.debug(f"The source dir is {args.source_dir}")
    logger.debug(f"The output dir is {args.output_dir}")

    image_name = DOCKER_IMAGE_NAME_FMT.format(suite_name=args.distro)

    image_exist = subprocess.run(["docker", "image", "inspect", image_name],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 check=False,
                                 timeout=10)
    if image_exist.returncode != 0:
        logger.warning(f"Docker image '{image_name}' is not present locally.")
        build_docker_image(args.distro)
    else:
        logger.info(f"Docker image '{image_name}' is present locally.")

    ret = build_package_in_docker(image_name, args.source_dir, args.output_dir, args.distro, args.run_lintian, args.extra_repo, args.extra_package, args.skip_gbp)

    if ret:
        sys.exit(0)
    else:
        sys.exit(1)

if __name__ == "__main__":

    try:
        main()

    except Exception as e:
        logger.critical(f"Uncaught exception : {e}")

        traceback.print_exc()

        sys.exit(1)

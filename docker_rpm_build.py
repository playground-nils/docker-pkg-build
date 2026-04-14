#!/usr/bin/env python3

# Copyright (c) Qualcomm Technologies, Inc. and/or its subsidiaries.
#
# SPDX-License-Identifier: BSD-3-Clause-Clear

"""
docker_rpm_build.py

Helper script to build an RPM package inside a Fedora-based Docker container.
Supports Fedora Rawhide (default) and can be extended to other Fedora,
CentOS, or RHEL versions by providing the appropriate Docker image.
The script mirrors the architectural choices of the original Debian-builder
while replacing Debian-specific tooling (sbuild/gbp) with RPM-centric
commands (rpmbuild, mock, dnf).
"""

import os
import sys
import argparse
import subprocess
import traceback
import platform
import shutil
import glob
import grp
import pwd
import getpass

from color_logger import logger

def _normalize_distro(distro: str) -> str:
    """
    Normalize various distro input forms to the canonical form used in Dockerfile names.
    Supports inputs like:
        rawhide
        fedora/rawhide, fedora-rawhide, fedora.rawhide
        fedora44, fedora/44, fedora-44
        centos8, centos/8, centos-8
        rhel8, rhel/8, rhel-8
    """
    if not distro:
        return distro

    d = distro.lower()
    # Replace separators with dot
    d = d.replace("/", ".").replace("-", ".")
    # Handle standalone "rawhide"
    if d.startswith("rawhide"):
        d = f"fedora.{d}"
    # Handle cases where the base name is concatenated with the version (e.g., fedora44)
    for base in ("fedora", "centos", "rhel"):
        if d.startswith(base) and not d[len(base):].startswith("."):
            suffix = d[len(base) :]
            d = f"{base}.{suffix}"
    return d

# ----------------------------------------------------------------------
# Docker image naming
# ----------------------------------------------------------------------
# Example image name: ghcr.io/qualcomm-linux/pkg-builder:arm64-rawhide
# The architecture part (amd64/arm64) is derived from the host.
# The distro part can be any supported tag (rawhide, fedora44, centos8, rhel8, ...)
DOCKER_IMAGE_NAME_FMT = "ghcr.io/qualcomm-linux/pkg-builder:{suite_name}"

def _discover_available_distros() -> list:
    """
    Identify supported RPM-based distros.
    Search for Dockerfiles that derive from a fedora, centos, redhat, or
    rhel base image, and use their names to create the list of supported
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
                                # Check for known RPM-based base images
                                if any(keyword in line for keyword in ("fedora", "centos", "redhat", "rhel")):
                                    distros.add(distro)
                                    break
                except Exception:
                    # If the Dockerfile cannot be read, skip it
                    continue
    return sorted(distros)


# ----------------------------------------------------------------------
# Argument parsing
# ----------------------------------------------------------------------
def parse_arguments() -> argparse.Namespace:
    """
    Parse command line arguments for the script.
    """
    parser = argparse.ArgumentParser(description="Build an RPM package inside a docker container.")

    parser.add_argument("-n", "--no-update-check",
        default=False,
        required=False,
        action='store_true',
        help="Bypass the remote update check and allow running with an out-of-date repo.")

    parser.add_argument("-s", "--source-dir",
        required=False,
        default=None,
        help="Path to the source directory containing the .spec file and sources.")

    parser.add_argument("-o", "--output-dir",
        required=False,
        default=None,
        help="Path to the output directory for the built RPMs.")

    parser.add_argument("-d", "--distro",
            type=str,
            # choices are handled dynamically after normalization
            default=None,
            help="The target distribution for the package build. Defaults to Fedora Rawhide.")

    parser.add_argument("-e", "--extra-repo",
        type=str,
        action='append',
        default=[],
        help="Additional YUM/DNF repository file to mount inside the container.")

    parser.add_argument("-p", "--extra-package",
        type=str,
        action="append",
        default=[],
        help="Additional RPM file or directory to install inside the build chroot.")

    parser.add_argument("-r", "--rebuild",
        action="store_true",
        help="Force rebuild of the Docker image and exit.")

    args = parser.parse_args()

    # Normalize distro argument to match Dockerfile naming conventions
    args.distro = _normalize_distro(args.distro)
    # Validate that the normalized distro is supported
    available_distros = _discover_available_distros()
    if args.distro and args.distro not in available_distros:
        # TODO I don't think throwing exceptions at the user is friendly
        raise argparse.ArgumentTypeError(f"Unsupported distro: {args.distro} (supported: {available_distros})")

    # Apply sensible defaults when not rebuilding
    if not args.rebuild:
        if args.source_dir is None:
            args.source_dir = "."
        if args.output_dir is None:
            args.output_dir = ".."

    return args

# ----------------------------------------------------------------------
# Docker pre-flight checks
# ----------------------------------------------------------------------
def check_docker_dependencies(timeout: int = 20) -> bool:
    """
    Verify Docker CLI presence, daemon accessibility, and user permission.
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

# ----------------------------------------------------------------------
# Docker image build / rebuild helpers
# ----------------------------------------------------------------------
def build_docker_image(distro: str) -> bool:
    """
    Build a Docker image for the given distro.
    Looks for a Dockerfile named ``Dockerfile.{arch}.{distro}`` inside
    the ``Dockerfiles`` directory.

    Args:
        distro (str): The distribution (e.g., 'fedora', 'centos').

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
        dockerfiles = [os.path.join(docker_dir, f'Dockerfile.*.{distro}')]
    else:
        # Rebuild all available RPM-based distros
        dockerfiles = []
        for distro in _discover_available_distros():
            dockerfile_glob = os.path.join(docker_dir, f'Dockerfile.*.{distro}')
            dockerfiles.extend(glob.glob(dockerfile_glob))
        dockerfiles = sorted(dockerfiles)
        logger.info(f"Rebuilding all docker images: {dockerfiles}")

    if not dockerfiles:
        raise Exception(
            f"No Dockerfile(s) found for distro={distro or '*'}"
        )

    for dockerfile in dockerfiles:
        # suite_name needs to contain family and release, e.g. fedora.44, fedora.rawhide, etc.
        suite_name = '.'.join(os.path.basename(dockerfile).split('.')[-2:])
        image_name = DOCKER_IMAGE_NAME_FMT.format(suite_name=suite_name)

        logger.debug(f"Rebuilding Docker image '{image_name}' from {dockerfile}...")

        # Delete/purge the current image if it exists
        try:
            subprocess.run(["docker", "image", "rm", "-f", image_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            logger.info(f"Deleted existing image '{image_name}'.")
        except subprocess.CalledProcessError:
            logger.debug(f"No existing image '{image_name}' to delete.")

        build_docker_image(suite_name)


# ----------------------------------------------------------------------
# Build RPM inside Docker
# ----------------------------------------------------------------------
def build_package_in_docker(image_name: str, source_dir: str, output_dir: str, extra_repo: list, extra_package: list) -> bool:
    """
    Run ``rpmbuild`` inside the container and copy the resulting RPMs
    to the host output directory.

    Returns True on success, False on failure.
    """
    # The container is expected to have rpmbuild installed and a
    # ``~/rpmbuild`` tree. The command builds all .spec files and
    # copies the resulting RPMs back to the host.
    build_cmd = "rpmbuild -ba *.spec && cp -a ~/rpmbuild/RPMS/* /workspace/output/",

    # Prepare mounts for extra RPMs
    extra_mounts = []
    for pkg_path in extra_package:
        abs_path = os.path.abspath(pkg_path)
        if not os.path.exists(abs_path):
            logger.warning(f"Extra package path does not exist and will be ignored: {pkg_path}")
            continue
        # Mount at the same absolute path inside the container
        extra_mounts.extend(['-v', f"{abs_path}:{abs_path}:Z"])

    # Prepare mounts for extra repo files (e.g., .repo)
    extra_repo_mounts = []
    for repo_path in extra_repo:
        abs_path = os.path.abspath(repo_path)
        if not os.path.exists(abs_path):
            logger.warning(f"Extra repo path does not exist and will be ignored: {repo_path}")
            continue
        # Mount at the same absolute path inside the container
        extra_repo_mounts.extend(["-v", f"{abs_path}:{abs_path}:Z"])

    docker_cmd = [
        'docker', 'run', '--rm', '--privileged', "-t",
        '-v', f"{source_dir}:/workspace/src:Z",
        '-v', f"{output_dir}:/workspace/output:Z",
        # Insert any extra package mounts
        *extra_mounts,
        # TODO make sure we're handling extra repos properly
        *extra_repo_mounts,
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
        logger.info("✅ RPM package built successfully.")
    else:
        logger.error("❌ RPM build failed.")

    # TODO are there any logs we can/should gather here?

    return res.returncode == 0

# ----------------------------------------------------------------------
# Repository up-to-date check
# ----------------------------------------------------------------------
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
        logger.critical("Local repository is NOT up-to-date with the remote!")
        logger.critical(f"  Local HEAD : {local_head}")
        logger.critical(f"  Remote HEAD: {remote_head}")
        logger.critical(f"Please pull the latest changes from {REMOTE}.")
        logger.critical(f"Then re-run this script with --rebuild to rebuild the docker images if needed.")
        logger.warning("To bypass this check, supply the --no-update-check argument.")
        logger.critical("!"*80)
        sys.exit(2)

    else:
        logger.info("The docker-pkg-build repo is up to date with the remote.")

# ----------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------
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

    # TODO rpmbuild isn't sbuild. What do we actually need to do here to handle arch?
    # Determine host architecture
    host_arch = platform.machine()
    if host_arch == "x86_64":
        build_arch = "amd64"
        logger.debug("Host architecture x86_64 mapped to amd64.")
        # TODO check whether we support cross-compilation for RPM builds
        # raise Exception(
        #     "AMD64 host is not supported for these builds; run on an ARM64 host."
        # )
    elif host_arch == "aarch64":
        build_arch = "arm64"
        logger.debug("Host architecture aarch64 mapped to arm64.")
    else:
        raise Exception(f"Unsupported host architecture: {host_arch}")

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

    # Determine image name
    image_name = DOCKER_IMAGE_NAME_FMT.format(suite_name=args.distro)

    # Ensure the Docker image exists locally
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

    # Run the build inside Docker
    ret = build_package_in_docker(image_name, args.source_dir, args.output_dir, args.extra_repo, args.extra_package)

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

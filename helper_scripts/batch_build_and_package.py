#!/usr/bin/env python3

# Copyright (c) Qualcomm Technologies, Inc. and/or its subsidiaries.
#
# SPDX-License-Identifier: BSD-3-Clause-Clear

"""
batch_build_and_package.py

Helper script that:
  1. Scans a root folder for immediate subfolders that contain a debian/ directory.
  2. Runs docker_deb_build.py on each subfolder (in sorted order) to produce
     .deb and .changes files in a shared output directory.
  3. After all builds, calls create_data_tar.py once per .changes file to
     extract every .deb into a shared  output-dir/data/<pkg>/<arch>/  tree.
  4. Creates one combined  combined_<distro>.tar.gz  tarball from that tree
     (running inside Docker so root-owned files are handled correctly).

Do NOT modify docker_deb_build.py or create_data_tar.py.
"""

import os
import sys
import argparse
import subprocess
import glob
import platform

# ---------------------------------------------------------------------------
# Locate the parent directory that contains docker_deb_build.py,
# create_data_tar.py, and color_logger.py.
# This script may live either alongside those scripts or one level below them
# (e.g. in a helper_scripts/ sub-directory).
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def _find_tools_dir() -> str:
    """
    Return the directory that contains docker_deb_build.py.
    Checks the script's own directory first, then its parent.
    """
    for candidate in (_SCRIPT_DIR, os.path.dirname(_SCRIPT_DIR)):
        if os.path.isfile(os.path.join(candidate, "docker_deb_build.py")):
            return candidate
    raise FileNotFoundError(
        "Cannot locate docker_deb_build.py relative to this script. "
        f"Searched: {_SCRIPT_DIR} and {os.path.dirname(_SCRIPT_DIR)}"
    )

_TOOLS_DIR = _find_tools_dir()

# Make color_logger importable regardless of where the script is invoked from.
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from color_logger import logger  # noqa: E402  (import after sys.path fixup)

_DOCKER_DEB_BUILD = os.path.join(_TOOLS_DIR, "docker_deb_build.py")
_CREATE_DATA_TAR  = os.path.join(_TOOLS_DIR, "create_data_tar.py")

# Image naming must match docker_deb_build.py
DOCKER_IMAGE_NAME_FMT = "ghcr.io/qualcomm-linux/pkg-builder:{suite_name}"


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build every Debian package found under SOURCE_ROOT and produce "
            "a single combined tarball of all built artefacts."
        )
    )

    parser.add_argument(
        "--source-root",
        required=True,
        metavar="DIR",
        help=(
            "Root folder whose immediate sub-directories are Debian package "
            "source trees (each must contain a debian/ sub-directory)."
        ),
    )

    parser.add_argument(
        "--output-dir",
        required=False,
        default=None,
        metavar="DIR",
        help=(
            "Directory where .deb / .changes files are written. "
            "Defaults to <source-root>/../output."
        ),
    )

    parser.add_argument(
        "--distro",
        required=True,
        choices=["noble", "questing", "resolute", "trixie", "sid"],
        help="Target distribution passed to docker_deb_build.py.",
    )

    parser.add_argument(
        "--final-tar-output",
        required=False,
        default=None,
        metavar="DIR",
        help=(
            "Base directory under which the combined tarball is written. "
            "The tarball is placed at <final-tar-output>/prebuilt_<distro>/<tar-name>.tar.gz. "
            "Defaults to --output-dir."
        ),
    )

    parser.add_argument(
        "--tar-name",
        required=False,
        default="combined",
        metavar="NAME",
        help=(
            "Base name (without extension) for the combined tarball. "
            "The file is written as <NAME>.tar.gz inside prebuilt_<distro>/. "
            "Defaults to 'combined'."
        ),
    )

    # ---- pass-through flags for docker_deb_build.py ----
    parser.add_argument(
        "-n", "--no-update-check",
        action="store_true",
        default=False,
        help="Bypass the remote update check (passed to docker_deb_build.py).",
    )
    parser.add_argument(
        "-l", "--run-lintian",
        action="store_true",
        default=False,
        help="Run lintian on each built package.",
    )
    parser.add_argument(
        "-e", "--extra-repo",
        type=str,
        action="append",
        default=[],
        metavar="REPO",
        help="Additional APT repository (may be repeated).",
    )
    parser.add_argument(
        "-p", "--extra-package",
        type=str,
        action="append",
        default=[],
        metavar="PKG",
        help="Additional .deb file or directory to install in the build chroot (may be repeated).",
    )
    parser.add_argument(
        "-k", "--skip-gbp",
        action="store_true",
        default=False,
        help="Skip gbp for quilt-format packages.",
    )

    # ---- combined-tarball options ----
    parser.add_argument(
        "--arch",
        required=False,
        default="arm64",
        help="Architecture label used when extracting .deb contents (default: arm64).",
    )
    parser.add_argument(
        "--keep-individual-tars",
        action="store_true",
        default=False,
        help=(
            "Keep the per-package tarballs produced by create_data_tar.py "
            "inside <output-dir>/prebuilt_<distro>/. By default they are removed "
            "after the combined tarball has been created."
        ),
    )

    # ---- exclusions ----
    parser.add_argument(
        "--exclude",
        nargs="+",
        action="append",
        default=[],
        metavar="DIR",
        help=(
            "One or more directories to skip during package discovery, even if they "
            "contain a debian/ sub-directory. Each entry can be a bare directory name "
            "(basename), a path relative to --source-root, or an absolute path. "
            "Only the named directory itself is skipped — sub-packages nested inside "
            "it (e.g. a sub-package inside an excluded parent) are still discovered and built. "
            "Accepts multiple space-separated values and/or can be repeated: "
            "--exclude a b  or  --exclude a --exclude b"
        ),
    )

    # ---- optional build ordering ----
    order_group = parser.add_mutually_exclusive_group()
    order_group.add_argument(
        "--build-order",
        nargs="+",
        metavar="PKG",
        default=None,
        help=(
            "Explicit build order: one or more package directory names (basenames) "
            "or paths relative to --source-root. "
            "Listed packages are built first in the given order; any remaining "
            "discovered packages are built afterwards in alphabetical order. "
            "Mutually exclusive with --order-file."
        ),
    )
    order_group.add_argument(
        "--order-file",
        metavar="FILE",
        default=None,
        help=(
            "Path to a text file listing package directory names (or paths relative "
            "to --source-root), one per line. "
            "Blank lines and lines starting with '#' are ignored. "
            "Listed packages are built first in the given order; remaining packages "
            "are built afterwards in alphabetical order. "
            "Mutually exclusive with --build-order."
        ),
    )

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_package_dirs(source_root: str, exclude: list = None) -> list:
    """
    Return a sorted list of directories at *any* depth under *source_root*
    that contain a  debian/  sub-directory.

    The search descends into every sub-directory but does NOT descend into
    a  debian/  directory itself (it is not a package root).

    *exclude* is an optional list of names to skip.  Each entry may be:
      - a bare directory name (basename) — matched against every directory
        encountered during the walk;
      - a path relative to *source_root* — resolved to an absolute path
        before the walk begins;
      - an absolute path.
    Excluded directories and all packages nested inside them are omitted.
    """
    # ---- pre-compute exclusion sets ----
    exclude_abs   = set()   # absolute paths to exclude
    exclude_names = set()   # bare basenames to exclude

    for name in (exclude or []):
        if os.path.isabs(name):
            exclude_abs.add(os.path.normpath(name))
        else:
            # Resolve as a path relative to source_root (covers both
            # "parent/child" style and bare "child" style).
            exclude_abs.add(os.path.normpath(os.path.join(source_root, name)))
            # If the name contains no path separator, also treat it as a
            # basename so it matches the same directory name anywhere in the tree.
            if os.sep not in name and "/" not in name:
                exclude_names.add(name)

    def _is_excluded(abs_path: str) -> bool:
        return (
            abs_path in exclude_abs
            or os.path.basename(abs_path) in exclude_names
        )

    results = []
    try:
        for dirpath, dirnames, _filenames in os.walk(source_root):
            abs_dirpath = os.path.abspath(dirpath)

            if _is_excluded(abs_dirpath):
                # This directory is excluded: do NOT record it as a package
                # root, but DO continue descending so that any sub-packages
                # nested inside it are still discovered.
                if "debian" in dirnames:
                    dirnames.remove("debian")   # don't walk inside debian/
                # Still prune hidden dirs; keep everything else for descent.
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                continue

            # Record as a package root if it contains debian/.
            if "debian" in dirnames:
                results.append(abs_dirpath)
                dirnames.remove("debian")   # prune: don't walk inside debian/

            # Prune hidden dirs from further traversal.
            # Note: we do NOT prune excluded dirs here — they are handled
            # above when os.walk visits them, so their children remain reachable.
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
    except PermissionError as exc:
        raise RuntimeError(f"Cannot scan source root: {exc}") from exc

    return sorted(results)


def load_order_file(path: str) -> list:
    """
    Read an order file and return a list of package names/paths.
    Blank lines and lines whose first non-whitespace character is '#' are ignored.
    """
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"--order-file not found: {path}")
    names = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            names.append(line)
    return names


def apply_build_order(pkg_dirs: list, order_names: list, source_root: str) -> list:
    """
    Reorder *pkg_dirs* so that packages named in *order_names* come first
    (in that order), followed by any remaining packages in their original
    alphabetical order.

    Each entry in *order_names* is matched against discovered packages by
    trying (in order):
      1. Exact absolute path match.
      2. Path relative to *source_root*.
      3. Basename (directory name) match — useful when names are unambiguous.

    Unrecognised names produce a warning and are skipped.
    Packages that appear in *order_names* more than once are only built once
    (first occurrence wins).
    """
    remaining = list(pkg_dirs)   # will shrink as we pull packages out
    ordered   = []
    seen      = set()

    for name in order_names:
        if name in seen:
            logger.warning(f"--build-order: '{name}' listed more than once; ignoring duplicate.")
            continue
        seen.add(name)

        matched = None

        # 1. Absolute path
        abs_name = os.path.abspath(name)
        if abs_name in remaining:
            matched = abs_name

        # 2. Relative to source_root
        if matched is None:
            rel = os.path.normpath(os.path.join(source_root, name))
            if rel in remaining:
                matched = rel

        # 3. Basename
        if matched is None:
            hits = [p for p in remaining if os.path.basename(p) == name]
            if len(hits) == 1:
                matched = hits[0]
            elif len(hits) > 1:
                logger.warning(
                    f"--build-order: '{name}' matches multiple packages "
                    f"({', '.join(hits)}); skipping — use a relative path to disambiguate."
                )
                continue

        if matched is None:
            logger.warning(f"--build-order: '{name}' did not match any discovered package; skipping.")
        else:
            ordered.append(matched)
            remaining.remove(matched)

    # Append whatever was not explicitly ordered (already sorted alphabetically)
    return ordered + remaining


def build_package(pkg_dir: str, output_dir: str, distro: str,
                  args: argparse.Namespace, skip_update_check: bool) -> bool:
    """
    Invoke docker_deb_build.py for a single package source directory.
    Returns True on success.
    """
    cmd = [
        sys.executable, _DOCKER_DEB_BUILD,
        "--source-dir", pkg_dir,
        "--output-dir", output_dir,
        "--distro",     distro,
    ]

    if skip_update_check or args.no_update_check:
        cmd.append("--no-update-check")
    if args.run_lintian:
        cmd.append("--run-lintian")
    for repo in args.extra_repo:
        cmd.extend(["--extra-repo", repo])
    for pkg in args.extra_package:
        cmd.extend(["--extra-package", pkg])
    if args.skip_gbp:
        cmd.append("--skip-gbp")

    logger.info(f"Building package: {pkg_dir}")
    logger.debug(f"Command: {' '.join(cmd)}")

    result = subprocess.run(cmd, check=False)
    return result.returncode == 0


def clear_data_dir(output_dir: str, image_name: str) -> bool:
    """
    Remove  output_dir/data/  by running rm -rf inside the Docker container.
    This is necessary because the directory is owned by root after Docker-based
    extraction and cannot be removed by the host user directly.
    Returns True on success (including when the directory does not exist).
    """
    data_dir = os.path.join(output_dir, "data")
    docker_cmd = [
        "docker", "run", "--rm",
        "-v", f"{output_dir}:{output_dir}:Z",
        image_name, "bash", "-c",
        f"rm -rf '{data_dir}'"
    ]
    logger.debug(f"Clearing data dir: {data_dir}")
    result = subprocess.run(docker_cmd, check=False,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def extract_changes_file(changes_file: str, output_dir: str,
                         distro: str, arch: str, image_name: str,
                         tar_output_dir: str = None) -> bool:
    """
    Call create_data_tar.py for a single .changes file.
    Debs are extracted into  output_dir/data/<pkg>/<arch>/
    and a tarball is written to  <tar_output_dir>/prebuilt_<distro>/
    (defaults to output_dir when tar_output_dir is None).
    Returns True on success.
    """
    cmd = [
        sys.executable, _CREATE_DATA_TAR,
        "--path-to-changes", changes_file,
        "--output-tar",      tar_output_dir if tar_output_dir else output_dir,
        "--distro",          distro,
        "--arch",            arch,
        "--docker-image",    image_name,
    ]

    logger.info(f"Extracting debs from: {os.path.basename(changes_file)}")
    logger.debug(f"Command: {' '.join(cmd)}")

    result = subprocess.run(cmd, check=False)
    return result.returncode == 0


def create_combined_tarball(output_dir: str, base_output_dir: str,
                            distro: str, tar_name: str, image_name: str) -> str:
    """
    Create  <tar_name>.tar.gz  inside  base_output_dir/prebuilt_<distro>/
    from  output_dir/data/  by running tar inside the Docker container
    (the data/ tree is owned by root after Docker-based extraction).

    Returns the absolute path of the created tarball on success, or an
    empty string on failure.
    """
    data_dir = os.path.join(output_dir, "data")
    if not os.path.isdir(data_dir):
        logger.error(f"data/ directory not found at {data_dir}. Nothing to archive.")
        return ""

    # Always place the combined tarball in prebuilt_<distro>/
    dest_dir = os.path.join(base_output_dir, f"prebuilt_{distro}")
    os.makedirs(dest_dir, exist_ok=True)

    tar_filename = f"{tar_name}.tar.gz"
    tar_path = os.path.join(dest_dir, tar_filename)

    # Build the tar command that runs inside the container.
    # Mount both output_dir and dest_dir so all paths are valid inside the container.
    mounts = []
    for host_path in sorted({output_dir, dest_dir}):
        mounts += ["-v", f"{host_path}:{host_path}:Z"]

    docker_cmd = (
        ["docker", "run", "--rm"]
        + mounts
        + [image_name, "bash", "-c",
           f"tar czf '{tar_path}' -C '{output_dir}' data"]
    )

    logger.info(f"Creating combined tarball: {tar_path}")
    logger.debug(f"Docker command: {' '.join(docker_cmd)}")

    result = subprocess.run(docker_cmd, check=False)
    return tar_path if result.returncode == 0 else ""


def remove_individual_tars(output_dir: str, distro: str) -> None:
    """
    Remove the per-package tarballs written by create_data_tar.py under
    output_dir/prebuilt_<distro>/.
    """
    prebuilt_dir = os.path.join(output_dir, f"prebuilt_{distro}")
    tarballs = glob.glob(os.path.join(prebuilt_dir, "*.tar.gz"))
    for tb in tarballs:
        try:
            os.remove(tb)
            logger.debug(f"Removed individual tarball: {tb}")
        except OSError as exc:
            logger.warning(f"Could not remove {tb}: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_arguments()

    # Resolve directories
    source_root = os.path.abspath(args.source_root)
    if not os.path.isdir(source_root):
        logger.critical(f"--source-root does not exist or is not a directory: {source_root}")
        sys.exit(1)

    if args.output_dir:
        output_dir = os.path.abspath(args.output_dir)
    else:
        output_dir = os.path.normpath(os.path.join(source_root, "..", "output"))

    final_tar_output = os.path.abspath(args.final_tar_output) if args.final_tar_output else output_dir

    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(final_tar_output, exist_ok=True)

    image_name = DOCKER_IMAGE_NAME_FMT.format(suite_name=args.distro)

    logger.info(f"Source root   : {source_root}")
    logger.info(f"Output dir    : {output_dir}")
    logger.info(f"Final tar dir : {final_tar_output}")
    logger.info(f"Distro        : {args.distro}")
    logger.info(f"Docker image  : {image_name}")

    # ------------------------------------------------------------------
    # Step 1 – discover package source directories
    # ------------------------------------------------------------------
    # Flatten [[a, b], [c]] -> [a, b, c]  (nargs="+" action="append" gives list-of-lists)
    exclude_flat = [item for group in args.exclude for item in group]

    if exclude_flat:
        logger.info(f"Excluding {len(exclude_flat)} director{'y' if len(exclude_flat) == 1 else 'ies'} from discovery:")
        for ex in exclude_flat:
            logger.info(f"  {ex}")

    pkg_dirs = find_package_dirs(source_root, exclude=exclude_flat)
    if not pkg_dirs:
        logger.critical(
            f"No sub-directories containing a debian/ folder found under: {source_root}"
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # Step 1b – apply optional build ordering
    # ------------------------------------------------------------------
    order_names = None
    if args.build_order:
        order_names = args.build_order
        logger.info(f"Build order supplied via --build-order ({len(order_names)} entr{'y' if len(order_names) == 1 else 'ies'}).")
    elif args.order_file:
        order_names = load_order_file(args.order_file)
        logger.info(f"Build order loaded from {args.order_file} ({len(order_names)} entr{'y' if len(order_names) == 1 else 'ies'}).")

    if order_names:
        pkg_dirs = apply_build_order(pkg_dirs, order_names, source_root)

    logger.info(f"Found {len(pkg_dirs)} package source director{'y' if len(pkg_dirs) == 1 else 'ies'} (build order):")
    for p in pkg_dirs:
        logger.info(f"  {p}")

    # ------------------------------------------------------------------
    # Step 2 – build each package
    # ------------------------------------------------------------------
    logger.info("=" * 60)
    logger.info("PHASE 1 – Building packages")
    logger.info("=" * 60)

    build_results: dict = {}   # pkg_dir -> bool
    for idx, pkg_dir in enumerate(pkg_dirs):
        # Skip the remote update check for every call after the first
        # (the check is expensive and the answer won't change mid-run).
        skip_update_check = idx > 0
        ok = build_package(pkg_dir, output_dir, args.distro, args, skip_update_check)
        build_results[pkg_dir] = ok
        status = "✅ OK" if ok else "❌ FAILED"
        logger.info(f"  [{idx + 1}/{len(pkg_dirs)}] {os.path.basename(pkg_dir)} — {status}")

        if not ok:
            logger.critical(
                f"Build failed for '{os.path.basename(pkg_dir)}' "
                f"({pkg_dir}). Aborting remaining builds."
            )
            sys.exit(1)

    succeeded = list(build_results.keys())   # all succeeded (we exit on first failure)
    failed: list = []

    logger.info(f"Build phase complete: {len(succeeded)} / {len(pkg_dirs)} succeeded.")

    # ------------------------------------------------------------------
    # Step 3 – Phase 2a: create per-package tarballs
    #
    # data/ is cleared before each call so that each per-package tarball
    # contains ONLY the debs from its own .changes file, not accumulated
    # data from previously processed packages.
    # ------------------------------------------------------------------
    logger.info("=" * 60)
    logger.info("PHASE 2a – Creating per-package tarballs")
    logger.info("=" * 60)

    changes_files = sorted(glob.glob(os.path.join(output_dir, "*.changes")))
    if not changes_files:
        logger.critical(
            f"No .changes files found in {output_dir}. "
            "Cannot create combined tarball."
        )
        sys.exit(1)

    logger.info(f"Found {len(changes_files)} .changes file(s).")

    extract_results: dict = {}   # changes_file -> bool
    for changes_file in changes_files:
        # Fresh data/ for each package so the per-package tarball is accurate.
        clear_data_dir(output_dir, image_name)
        ok = extract_changes_file(
            changes_file, output_dir, args.distro, args.arch, image_name
        )
        extract_results[changes_file] = ok
        status = "✅ OK" if ok else "❌ FAILED"
        logger.info(f"  {os.path.basename(changes_file)} — {status}")

    extract_failed = [c for c, ok in extract_results.items() if not ok]
    if extract_failed:
        logger.warning(f"{len(extract_failed)} extraction(s) failed:")
        for c in extract_failed:
            logger.warning(f"  {c}")

    # ------------------------------------------------------------------
    # Step 3b – Phase 2b: re-accumulate all data for the combined tarball
    #
    # A second pass accumulates every package into a single data/ tree.
    # Intermediate tarballs from this pass go to a throwaway directory so
    # they do not overwrite the correct per-package tarballs from Phase 2a.
    # ------------------------------------------------------------------
    logger.info("=" * 60)
    logger.info("PHASE 2b – Accumulating all packages for combined tarball")
    logger.info("=" * 60)

    accum_work_dir = os.path.join(output_dir, "_combined_work")
    os.makedirs(accum_work_dir, exist_ok=True)
    clear_data_dir(output_dir, image_name)

    for changes_file in changes_files:
        logger.info(f"  Accumulating: {os.path.basename(changes_file)}")
        extract_changes_file(
            changes_file, output_dir, args.distro, args.arch, image_name,
            tar_output_dir=accum_work_dir,
        )

    # ------------------------------------------------------------------
    # Step 4 – create one combined tarball
    # ------------------------------------------------------------------
    logger.info("=" * 60)
    logger.info("PHASE 3 – Creating combined tarball")
    logger.info("=" * 60)

    combined_tar = create_combined_tarball(
        output_dir, final_tar_output, args.distro, args.tar_name, image_name
    )
    if not combined_tar:
        logger.critical("Failed to create combined tarball.")
        sys.exit(1)

    logger.info(f"✅ Combined tarball: {combined_tar}")

    # Clean up the accumulation work directory (root-owned, use Docker).
    clear_data_dir(accum_work_dir, image_name)   # removes accum_work_dir/data/
    try:
        import shutil
        shutil.rmtree(accum_work_dir, ignore_errors=True)
    except Exception:
        pass   # non-fatal if cleanup fails

    # ------------------------------------------------------------------
    # Step 5 – optionally clean up per-package tarballs
    # ------------------------------------------------------------------
    if not args.keep_individual_tars:
        remove_individual_tars(output_dir, args.distro)
        logger.info("Per-package tarballs removed (use --keep-individual-tars to retain them).")

    # ------------------------------------------------------------------
    # Final summary
    # ------------------------------------------------------------------
    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info("=" * 60)
    logger.info(f"  Packages built      : {len(succeeded)} / {len(pkg_dirs)}")
    logger.info(f"  .changes processed  : {len(changes_files) - len(extract_failed)} / {len(changes_files)}")
    logger.info(f"  Combined tarball    : {combined_tar}")

    if failed or extract_failed:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logger.critical(f"Uncaught exception: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

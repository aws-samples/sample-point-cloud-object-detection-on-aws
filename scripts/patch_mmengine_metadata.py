#!/usr/bin/env python3
"""Make mmengine 0.10.7 read package metadata with importlib.metadata, not pkg_resources.

mmengine's `utils/package_utils.py` (Copyright (c) OpenMMLab, Apache-2.0) is the only
module in the GPU image that imports `pkg_resources`, which setuptools 82 removed. That
held the image at setuptools 81.0.0, with its advisories (PYSEC-2026-3447,
GHSA-xmj3-9gf7-h52w). This rewrites its three uses, `is_installed`,
`get_installed_path` and `package2module`, with the standard library's
`importlib.metadata`, keeping what each returns and raises, so the image can run the
setuptools that uv.lock pins.

Each original function must be found exactly as mmengine 0.10.7 ships it, or the script
fails and changes nothing: another mmengine version needs this script re-checked.
Running it twice is safe.

Usage, with the target environment's interpreter (Dockerfile.gpu, scripts/setup-ec2.sh):

    python scripts/patch_mmengine_metadata.py [path/to/mmengine/utils/package_utils.py]
"""

import importlib.util
import pathlib
import sys

MARKER = "# Patched by scripts/patch_mmengine_metadata.py: importlib.metadata, not pkg_resources."

IS_INSTALLED_OLD = """    import importlib.util

    import pkg_resources
    from pkg_resources import get_distribution

    # refresh the pkg_resources
    # more datails at https://github.com/pypa/setuptools/issues/373
    importlib.reload(pkg_resources)
    try:
        get_distribution(package)
        return True
    except pkg_resources.DistributionNotFound:
"""
IS_INSTALLED_NEW = """    import importlib.metadata
    import importlib.util

    try:
        importlib.metadata.distribution(package)
        return True
    except importlib.metadata.PackageNotFoundError:
"""

GET_PATH_OLD = """    import importlib.util

    from pkg_resources import DistributionNotFound, get_distribution

    # if the package name is not the same as module name, module name should be
    # inferred. For example, mmcv-full is the package name, but mmcv is module
    # name. If we want to get the installed path of mmcv-full, we should concat
    # the pkg.location and module name
    try:
        pkg = get_distribution(package)
    except DistributionNotFound as e:
"""
GET_PATH_NEW = """    import importlib.metadata
    import importlib.util

    # if the package name is not the same as module name, module name should be
    # inferred. For example, mmcv-full is the package name, but mmcv is module
    # name. If we want to get the installed path of mmcv-full, we should concat
    # the pkg.location and module name
    try:
        pkg = importlib.metadata.distribution(package)
    except importlib.metadata.PackageNotFoundError as e:
"""

LOCATION_OLD = """    possible_path = osp.join(pkg.location, package)  # type: ignore
    if osp.exists(possible_path):
        return possible_path
    else:
        return osp.join(pkg.location, package2module(package))  # type: ignore
"""
LOCATION_NEW = """    location = str(pkg.locate_file(''))
    possible_path = osp.join(location, package)
    if osp.exists(possible_path):
        return possible_path
    else:
        return osp.join(location, package2module(package))
"""

TO_MODULE_OLD = """    from pkg_resources import get_distribution
    pkg = get_distribution(package)
    if pkg.has_metadata('top_level.txt'):
        module_name = pkg.get_metadata('top_level.txt').split('\\n')[0]
        return module_name
"""
TO_MODULE_NEW = """    import importlib.metadata

    pkg = importlib.metadata.distribution(package)
    top_level = pkg.read_text('top_level.txt')
    if top_level:
        module_name = top_level.split('\\n')[0]
        return module_name
"""

REPLACEMENTS = [
    (IS_INSTALLED_OLD, IS_INSTALLED_NEW),
    (GET_PATH_OLD, GET_PATH_NEW),
    (LOCATION_OLD, LOCATION_NEW),
    (TO_MODULE_OLD, TO_MODULE_NEW),
]


def default_target() -> pathlib.Path:
    # find_spec on the top-level package locates it without importing it.
    spec = importlib.util.find_spec("mmengine")
    if spec is None or spec.origin is None:
        sys.exit("ERROR: mmengine is not installed in this environment")
    return pathlib.Path(spec.origin).parent / "utils" / "package_utils.py"


def patch(text: str) -> str:
    """Return the patched source, or exit if any original is missing."""
    missing = [
        old.splitlines()[2].strip() for old, _ in REPLACEMENTS if text.count(old) != 1
    ]
    if missing:
        sys.exit(
            "ERROR: package_utils.py is not mmengine 0.10.7's; not patched. "
            f"Missing: {missing}"
        )
    for old, new in REPLACEMENTS:
        text = text.replace(old, new)
    if "pkg_resources" in text.replace("pkg_resources will be imported", ""):
        sys.exit("ERROR: package_utils.py still refers to pkg_resources; not patched")
    return MARKER + "\n" + text


def main(argv: list[str]) -> int:
    target = pathlib.Path(argv[0]) if argv else default_target()
    text = target.read_text()
    if text.startswith(MARKER):
        print(f"{target}: already patched")
        return 0
    target.write_text(patch(text))
    print(f"{target}: patched to use importlib.metadata")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

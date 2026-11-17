#!/usr/bin/env python3
"""Make mmdet3d 1.4.0's Lyft metric optional, so lyft-dataset-sdk can be uninstalled.

mmdet3d 1.4.0 (Copyright (c) OpenMMLab, Apache-2.0) requires lyft-dataset-sdk, which is
licensed CC BY-NC-SA 4.0, a non-commercial licence, and imports it when it registers its
evaluation metrics: `evaluation/metrics/__init__.py` imports the Lyft metric and
`evaluation/functional/__init__.py` the Lyft evaluation functions, and both import the
SDK. This sample never evaluates on the Lyft dataset. The patch imports those two
modules only when the SDK is installed. Without it, the Lyft names are None and every
other metric registers, and neither the SDK nor pandas, which the Lyft metric also
imports, is imported at all. With it, both modules import as before, errors included.

Each import must be found exactly as mmdet3d 1.4.0 ships it, or the script fails and
changes nothing: another mmdet3d version needs this script re-checked. Running it twice
is safe.

Usage, with the target environment's interpreter (Dockerfile.gpu, scripts/setup-ec2.sh),
then uninstall lyft-dataset-sdk and the packages only it required:

    python scripts/patch_mmdet3d_lyft.py [path/to/mmdet3d]
"""

import importlib.util
import pathlib
import sys

MARKER = "# Patched by scripts/patch_mmdet3d_lyft.py: the Lyft SDK is optional."

METRICS = "evaluation/metrics/__init__.py"
METRICS_OLD = "from .lyft_metric import LyftMetric  # noqa: F401,F403\n"
METRICS_NEW = """import importlib.util

if importlib.util.find_spec('lyft_dataset_sdk') is not None:
    from .lyft_metric import LyftMetric  # noqa: F401,F403
else:  # lyft-dataset-sdk, CC BY-NC-SA 4.0, is not installed
    LyftMetric = None
"""

FUNCTIONAL = "evaluation/functional/__init__.py"
FUNCTIONAL_OLD = """from .lyft_eval import (get_classwise_aps, get_single_class_aps, load_lyft_gts,
                        load_lyft_predictions, lyft_eval)
"""
FUNCTIONAL_NEW = """import importlib.util

if importlib.util.find_spec('lyft_dataset_sdk') is not None:
    from .lyft_eval import (get_classwise_aps, get_single_class_aps,
                            load_lyft_gts, load_lyft_predictions, lyft_eval)
else:  # lyft-dataset-sdk, CC BY-NC-SA 4.0, is not installed
    get_classwise_aps = get_single_class_aps = load_lyft_gts = None
    load_lyft_predictions = lyft_eval = None
"""

PATCHES = [
    (METRICS, METRICS_OLD, METRICS_NEW),
    (FUNCTIONAL, FUNCTIONAL_OLD, FUNCTIONAL_NEW),
]


def default_target() -> pathlib.Path:
    # find_spec on the top-level package locates it without importing it.
    spec = importlib.util.find_spec("mmdet3d")
    if spec is None or spec.origin is None:
        sys.exit("ERROR: mmdet3d is not installed in this environment")
    return pathlib.Path(spec.origin).parent


def main(argv: list[str]) -> int:
    root = pathlib.Path(argv[0]) if argv else default_target()
    files = {rel: (root / rel).read_text() for rel, _, _ in PATCHES}
    if all(text.startswith(MARKER) for text in files.values()):
        print(f"{root}: already patched")
        return 0
    missing = [rel for rel, old, _ in PATCHES if files[rel].count(old) != 1]
    if missing:
        sys.exit(
            f"ERROR: not mmdet3d 1.4.0's code; nothing patched. Missing: {missing}"
        )
    # Checked before either file is written, so a refusal leaves both as they were.
    for rel, old, new in PATCHES:
        (root / rel).write_text(MARKER + "\n" + files[rel].replace(old, new))
    print(f"{root}: Lyft metric made optional")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

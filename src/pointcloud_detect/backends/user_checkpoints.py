"""Loading a checkpoint the user supplies, such as a fine-tuned one (threat model T28).

A checkpoint is a pickle, so loading one in full runs whatever it says. By default
only a plain weights file loads: tensors and plain containers, which ``torch.load``
reads with ``weights_only=True``. A checkpoint that needs more, as mmengine's do, loads
only with ``--trust-checkpoint``, which warns first. The inference scripts share these.
"""

import argparse
import sys

TRUST_WARNING = (
    "[warning] --trust-checkpoint: loading {path} with weights_only=False. "
    "Unpickling a checkpoint runs any code it contains, with this process's "
    "permissions. Only do this for a checkpoint you produced or otherwise trust."
)


def add_trust_checkpoint_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--trust-checkpoint",
        action="store_true",
        help=(
            "Allow full unpickling of the checkpoint (torch.load weights_only=False), "
            "which mmengine checkpoints need and which runs any code they contain. "
            "Without it, only a plain weights file loads."
        ),
    )


def monkey_patch_torch_load() -> None:
    """Only with --trust-checkpoint: make weights_only=False torch.load's default.

    mmengine's own loaders call torch.load without weights_only, and torch 2.6 and later
    default it to True, so a trusted mmengine checkpoint needs the old default back.
    """
    import torch  # pyright: ignore[reportMissingImports]

    if not hasattr(torch.load, "_patched"):
        _original = torch.load

        def _patched(*args, **kwargs):
            kwargs.setdefault("weights_only", False)
            return _original(*args, **kwargs)

        # setattr rather than direct assignment: the marker is read back via
        # hasattr(torch.load, "_patched") to make patching idempotent, but a
        # function object has no such declared attribute to assign to.
        setattr(_patched, "_patched", True)  # noqa: B010
        torch.load = _patched


def load_checkpoint(path: str, device: str, trusted: bool):
    """Load a checkpoint, unpickling arbitrary objects only when trusted.

    Untrusted, the checkpoint is inspected first, without unpickling it, for the globals
    a weights-only load would refuse. If there are any it is not loaded, and the message
    names them and the flag; otherwise it loads with weights_only=True.
    """
    import torch  # pyright: ignore[reportMissingImports]

    if trusted:
        print(TRUST_WARNING.format(path=path), file=sys.stderr)
        # Reached only with --trust-checkpoint, after the warning above.
        return torch.load(path, map_location=device, weights_only=False)  # nosec B614
    unsafe = torch.serialization.get_unsafe_globals_in_checkpoint(path)
    if unsafe:
        sys.exit(
            f"ERROR: {path} is not loadable as plain weights: it needs "
            f"{', '.join(unsafe)}. If you trust this checkpoint, re-run with "
            "--trust-checkpoint."
        )
    return torch.load(path, map_location=device, weights_only=True)

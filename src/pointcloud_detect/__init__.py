"""Point cloud object detection and segmentation pipeline."""

import os

__version__ = "0.1.0"

# E57 files carry an XML section that pye57 hands to the Xerces-C bundled in its
# wheel. That Xerces-C (3.2.3 in the locked pye57 0.4.19) resolves a DOCTYPE's
# external DTD and parameter entities by default: an uploaded E57 can make the
# worker open a local path of its choosing, and a FIFO or device there hangs or
# exhausts it (T54). Xerces-C's XMLScanner refuses any DOCTYPE when this variable is
# exactly "1"; it reads it with getenv() at parse time, so it only has to be set
# before the first E57 is opened. E57 files have no use for a DTD.
#
# Set on package import, not in the readers, so every path into pye57 -- the
# whole-load reader, the streaming reader, the parser sidecar and its children --
# gets it. setdefault leaves an operator's explicit value alone.
os.environ.setdefault("XERCES_DISABLE_DTD", "1")

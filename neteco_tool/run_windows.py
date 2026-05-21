"""Windows launcher — adds portable packages to sys.path before starting the app."""
import sys
import os

_here = os.path.dirname(os.path.abspath(__file__))
_pkgs = os.path.join(_here, "python-embed", "Lib", "site-packages")
if os.path.isdir(_pkgs) and _pkgs not in sys.path:
    sys.path.insert(0, _pkgs)

import app  # noqa: E402

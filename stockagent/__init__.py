__version__ = "0.1.0"

# Patch nselib's HTTP layer (timeouts + shared session) BEFORE any submodule import.
from stockagent.nselib_patch import apply as _apply_nselib_patch

_apply_nselib_patch()


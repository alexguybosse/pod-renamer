# Used by SETUP: prints this Python's path if it is suitable for POD Renamer.
# Needs 3.9+, 64-bit, and NOT the experimental "free-threaded" build (python3.13t),
# which the PDF and OCR libraries don't have ready-made installs for.
import struct
import sys
import sysconfig

ok = (
    sys.version_info >= (3, 9)
    and not sysconfig.get_config_var("Py_GIL_DISABLED")
    and struct.calcsize("P") == 8
)
if ok:
    print(sys.executable)

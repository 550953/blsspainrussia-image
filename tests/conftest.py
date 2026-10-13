"""Only stub optional image OCR wheels when absent; installed packages remain real."""
import importlib.util
import sys
import types


def _installed(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return name in sys.modules


if not _installed("cv2"):
    sys.modules["cv2"] = types.ModuleType("cv2")

if not _installed("numpy"):
    numpy_stub = types.ModuleType("numpy")
    numpy_stub.ndarray = object
    numpy_stub.uint8 = object
    sys.modules["numpy"] = numpy_stub

if not _installed("ddddocr"):
    ddddocr_stub = types.ModuleType("ddddocr")

    class DdddOcr:
        def __init__(self, *args, **kwargs):
            pass

    ddddocr_stub.DdddOcr = DdddOcr
    sys.modules["ddddocr"] = ddddocr_stub

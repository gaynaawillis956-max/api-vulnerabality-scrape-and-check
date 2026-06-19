import sys
import subprocess
import importlib

_PACKAGES = [
    ("httpx", "httpx>=0.27.0"),
    ("tqdm",  "tqdm>=4.60.0"),
]


def ensure_packages() -> None:
    """Install any missing runtime dependencies before the app starts."""
    missing = []
    for import_name, pip_spec in _PACKAGES:
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing.append(pip_spec)

    if not missing:
        return

    print(f"Auto-installing: {', '.join(missing)}")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", *missing, "-q"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print("Dependencies ready.\n")

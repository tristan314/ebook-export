"""Dependency checker — pure stdlib, runs before any third-party imports."""

import importlib
import subprocess
import sys

REQUIRED = {
    "requests": "requests",
    "aiohttp": "aiohttp",
    "fitz": "pymupdf",
    "rich": "rich",
    "keyring": "keyring",
}

OPTIONAL = {
    "cryptography": "cryptography",
}


def check_and_install():
    missing = []
    for module, pip_name in REQUIRED.items():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(pip_name)

    optional_missing = []
    for module, pip_name in OPTIONAL.items():
        try:
            importlib.import_module(module)
        except ImportError:
            optional_missing.append(pip_name)

    if not missing and not optional_missing:
        return

    if missing:
        print("Missing required packages:")
        for pkg in missing:
            print(f"  - {pkg}")

    if optional_missing:
        print("Missing optional packages (recommended):")
        for pkg in optional_missing:
            print(f"  - {pkg}")

    all_missing = missing + optional_missing
    answer = input(f"\nInstall {len(all_missing)} package(s) now? [Y/n] ").strip().lower()
    if answer in ("", "y", "yes"):
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", *all_missing],
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        print()
    elif missing:
        print("Cannot continue without required packages.")
        sys.exit(1)

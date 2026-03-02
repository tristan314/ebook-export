#!/usr/bin/env python3
"""Build a macOS .app bundle for eBook Export.

Usage:  python3 build_macos_app.py
Output: ../eBook Export.app  (next to Combined Export/)
"""

import os
import plistlib
import shutil
import stat
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)               # Combined Export/
OUTPUT_DIR = os.path.dirname(PROJECT_DIR)                # eBook Export/
APP_NAME = "eBook Export"
APP_BUNDLE = os.path.join(OUTPUT_DIR, f"{APP_NAME}.app")

# Directories inside the app bundle
CONTENTS = os.path.join(APP_BUNDLE, "Contents")
MACOS_DIR = os.path.join(CONTENTS, "MacOS")
RESOURCES = os.path.join(CONTENTS, "Resources")
APP_SOURCE = os.path.join(RESOURCES, "source")           # bundled source copy


def build():
    # Clean previous build
    if os.path.exists(APP_BUNDLE):
        shutil.rmtree(APP_BUNDLE)

    os.makedirs(MACOS_DIR)
    os.makedirs(APP_SOURCE)

    # ── 1. Copy source into bundle ───────────────────────────────────────
    copy_items = [
        "config.py", "deps.py", "ui.py", "main.py",
        "downloader.py", "pdf_builder.py", "login_form.py",
        "platforms",
        "GUI",
    ]
    for item in copy_items:
        src = os.path.join(PROJECT_DIR, item)
        dst = os.path.join(APP_SOURCE, item)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        elif os.path.exists(src):
            shutil.copy2(src, dst)

    # Copy config.json if it exists (user settings)
    cfg = os.path.join(PROJECT_DIR, "config.json")
    if os.path.exists(cfg):
        shutil.copy2(cfg, os.path.join(APP_SOURCE, "config.json"))

    # Ensure eBooks output dir exists
    os.makedirs(os.path.join(APP_SOURCE, "eBooks"), exist_ok=True)

    # ── 2. Create launcher script ────────────────────────────────────────
    launcher = os.path.join(MACOS_DIR, APP_NAME)

    # Record the exact Python that ran the build (has all deps installed).
    # At launch, try that first, then fall back to PATH python3.
    build_python = sys.executable

    # Detect architecture to force in launcher (prevents Rosetta mismatch)
    import platform
    arch = platform.machine()  # arm64 or x86_64

    with open(launcher, "w") as f:
        f.write(f"""#!/bin/bash
# eBook Export — macOS launcher
DIR="$(dirname "$0")"
SOURCE="$DIR/../Resources/source"
LOG="$SOURCE/crash.log"
cd "$SOURCE"

# Use the Python that built this app (has packages); fall back to PATH
if [ -x "{build_python}" ]; then
    PYTHON="{build_python}"
else
    PYTHON="$(command -v python3 || echo /usr/bin/python3)"
fi

# Force native arch to prevent Rosetta mismatch with installed packages
exec arch -{arch} "$PYTHON" -u GUI/app.py 2>"$LOG"
""")

    os.chmod(launcher, os.stat(launcher).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # ── 3. Create Info.plist ─────────────────────────────────────────────
    plist = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": "com.ebook-export.app",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0",
        "CFBundlePackageType": "APPL",
        "CFBundleSignature": "????",
        "CFBundleExecutable": APP_NAME,
        "LSMinimumSystemVersion": "10.15",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.utilities",
    }

    # Use an app icon if one exists
    icon_name = "AppIcon.icns"
    icon_src = os.path.join(SCRIPT_DIR, icon_name)
    if os.path.exists(icon_src):
        shutil.copy2(icon_src, os.path.join(RESOURCES, icon_name))
        plist["CFBundleIconFile"] = icon_name

    with open(os.path.join(CONTENTS, "Info.plist"), "wb") as f:
        plistlib.dump(plist, f)

    # ── 4. Register with Launch Services ─────────────────────────────────
    try:
        subprocess.run(
            ["/System/Library/Frameworks/CoreServices.framework/Frameworks/"
             "LaunchServices.framework/Support/lsregister",
             "-f", APP_BUNDLE],
            capture_output=True,
        )
    except Exception:
        pass

    print(f"Built:  {APP_BUNDLE}")
    print(f"Launch: open \"{APP_BUNDLE}\"")


if __name__ == "__main__":
    build()

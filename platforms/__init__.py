"""Platform registry."""

from platforms import klett, cornelsen

PLATFORMS = {
    "klett": klett,
    "cornelsen": cornelsen,
}


def get_platform(name):
    """Return the platform module for the given name."""
    return PLATFORMS[name]


def platform_names():
    return list(PLATFORMS.keys())

"""Config persistence (JSON) and credential storage (keyring)."""

import json
import os

import keyring

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
KEYRING_SERVICE = "ebook-export"

DEFAULTS = {
    "scale": 4,           # Klett: Scale1-4
    "quality": 4,         # Cornelsen: tile multiplier
    "max_concurrent_downloads": 10,
    "method": "auto",     # Cornelsen: auto / lossless / tiles
}


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    return cfg


def save_config(cfg):
    # Only persist non-default, non-credential fields
    data = {k: v for k, v in cfg.items() if k in DEFAULTS or k.startswith("email_")}
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)


def get_credentials(platform):
    """Return (email, password) from config + keyring, or (None, None)."""
    cfg = load_config()
    email = cfg.get(f"email_{platform}")
    password = None
    if email:
        password = keyring.get_password(KEYRING_SERVICE, f"{platform}:{email}")
    return email, password


def store_credentials(platform, email, password):
    """Save password to OS keyring. (Email is persisted by the caller via save_config.)"""
    keyring.set_password(KEYRING_SERVICE, f"{platform}:{email}", password)


def run_config_wizard(platform, console):
    """Interactive config wizard. Returns updated config dict."""
    from rich.prompt import Prompt

    cfg = load_config()

    console.print()
    console.print(f"[bold]Configuration — {platform.title()}[/bold]")
    console.print("[dim]  Leave empty to keep default value[/dim]")
    console.print()

    # Email
    current_email = cfg.get(f"email_{platform}", "")
    email = Prompt.ask("Email", default=current_email or None)

    # Password
    current_pw = ""
    if current_email:
        current_pw = keyring.get_password(KEYRING_SERVICE, f"{platform}:{current_email}") or ""
    pw_display = "••••••••" if current_pw else ""
    password = Prompt.ask(f"Password {f'[dim]({pw_display})[/dim]' if pw_display else ''}", password=True, default=None)
    if not password and current_pw:
        password = current_pw

    # Store email in cfg (saved with config), password in keyring
    if email:
        cfg[f"email_{platform}"] = email
    if email and password:
        store_credentials(platform, email, password)

    # Platform-specific settings
    if platform == "klett":
        scale = Prompt.ask("Image scale (1-4)", default=str(cfg.get("scale", 4)))
        cfg["scale"] = max(1, min(4, int(scale)))
    elif platform == "cornelsen":
        quality = Prompt.ask("Image quality multiplier (1-6)", default=str(cfg.get("quality", 4)))
        cfg["quality"] = max(1, min(6, int(quality)))
        method = Prompt.ask("Download method", choices=["auto", "lossless", "tiles"], default=cfg.get("method", "auto"))
        cfg["method"] = method

    # Shared settings
    concurrency = Prompt.ask("Max concurrent downloads", default=str(cfg.get("max_concurrent_downloads", 10)))
    cfg["max_concurrent_downloads"] = max(1, int(concurrency))

    save_config(cfg)
    console.print("[green]Configuration saved.[/green]")
    console.print()
    return cfg


def has_credentials(platform):
    """Check if credentials exist for a platform."""
    email, password = get_credentials(platform)
    return bool(email and password)

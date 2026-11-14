"""Xtream Codes login: the username/password style that IPTV Smarters, TiviMate and similar apps use.
The credentials only guard the Xtream endpoints; they are stored in the config file."""
import re
import secrets

from . import store

CRED_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


def get():
    """(username, password), created on first use so the feature works without any setup."""
    x = store.load().get("xtream")
    if x and x.get("username") and x.get("password"):
        return x["username"], x["password"]
    with store.edit() as cfg:
        x = cfg.get("xtream")
        if not (x and x.get("username") and x.get("password")):
            x = {"username": "sunny", "password": secrets.token_urlsafe(9)}
            cfg["xtream"] = x
    return x["username"], x["password"]


def set_credentials(username, password):
    if not CRED_RE.match(username or ""):
        raise ValueError("Username: 3 to 32 letters, numbers, dot, dash or underscore")
    if not CRED_RE.match(password or ""):
        raise ValueError("Password: 3 to 32 letters, numbers, dot, dash or underscore")
    with store.edit() as cfg:
        cfg["xtream"] = {"username": username, "password": password}


def check(username, password):
    u, p = get()
    return secrets.compare_digest(str(username), u) & secrets.compare_digest(str(password), p)

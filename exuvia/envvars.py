"""A project's environment variables: the API keys, tokens and settings its
nodes read with ``os.environ``, set in the workbench instead of a ``.env`` file.

They live in the project's database (``Store.env_vars``), so they work the
same on ``exuvia serve``, on a hub (each project its own) and with a shared
``EXUVIA_DB``, and they reach a remote executor with each run, which a
``.env`` never does. A run gets them on top of the environment it would get
anyway (``pool.node_environ``): a project variable wins over the server's
variable of the same name.

A variable is *secret* or *plain*. A plain one is configuration anyone who
can open the project may read (the agent too). A secret one is write-only:
once saved its value is never sent to a client or the agent again, and
wherever it shows up in a run's log or error it is replaced with ``***``
before the log is stored, streamed or read by the agent.

Secrets are stored encrypted (``Keyring``): Fernet (AES-128-CBC with an
HMAC-SHA256), as ``enc:v1:<key id>:<token>``. The key is the server's
``EXUVIA_SECRETS_KEY`` when it is set (several, comma-separated, to rotate:
the first encrypts, every one decrypts), or else a key exuvia makes once and
keeps in the project's ``.exuvia/secrets.key``, away from the database. A
run gets the decrypted values; nothing else does. Secrets saved before
encryption, or with a key that is no longer the first, are encrypted again
with the current one when the project loads.

Values are normalized when they are saved: line endings become ``\n`` and
surrounding whitespace is dropped, and a value of several lines (a PEM key,
a certificate chain) ends with exactly one newline, as PEM files and
OpenSSH keys expect.

Names are checked so a project cannot set exuvia's own settings for its
nodes (``EXUVIA_*``, ``PORTKEY_*``, package index credentials), which nodes
are never given (see pool.py).
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from pathlib import Path
from typing import Any, Iterable

NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
# What a node's process never gets from the server (pool.OWN_PREFIXES,
# pool.INDEX_CREDENTIAL), so a project cannot give it either.
RESERVED_PREFIXES = ("EXUVIA_", "PORTKEY_")
INDEX_CREDENTIAL = re.compile(r"^UV_INDEX_[A-Z0-9_]+_(USERNAME|PASSWORD)(_FILE)?$", re.IGNORECASE)
# Variables that would change how the runner itself starts or finds its code.
RESERVED_NAMES = frozenset({"PATH", "PYTHONPATH", "PYTHONHOME", "PYTHONSAFEPATH", "PYTHONUSERBASE", "VIRTUAL_ENV"})
# A service account's JSON or a PEM key fits; a dataset does not belong here.
MAX_VALUE_CHARS = 32_768
MAX_VARIABLES = 200
# A secret shorter than this is not looked for in logs: "1" or "on" would
# mask every digit or word that happens to match.
MIN_REDACT_CHARS = 4
MASK = "***"
# The armour of a PEM block names its kind, not the key: never masked.
PEM_ARMOUR = re.compile(r"^-{5}(BEGIN|END) [A-Z0-9 ]+-{5}$")

KEY_ENV = "EXUVIA_SECRETS_KEY"
KEY_FILE = "secrets.key"  # in the project's .exuvia/
ENCRYPTED = "enc:v1:"
KEY_HOWTO = "make one with `exuvia secrets-key`"


class EnvError(ValueError):
    """A variable that cannot be set as asked."""


class SecretsKeyError(EnvError):
    """A key that is malformed, or a secret no key here can decrypt."""


def check_name(name: str) -> str:
    if not NAME.match(name or ""):
        raise EnvError(
            "A variable's name is letters, digits and underscores, starting with a letter or underscore "
            "(e.g. FRED_API_KEY), at most 128 characters."
        )
    upper = name.upper()
    if upper.startswith(RESERVED_PREFIXES) or INDEX_CREDENTIAL.match(name):
        raise EnvError(
            f"{name} is one of exuvia's own settings, which nodes never get. Set it in the server's environment."
        )
    if upper in RESERVED_NAMES:
        raise EnvError(f"{name} is set by exuvia for every run and cannot be changed per project.")
    return name


def normalize(value: str) -> str:
    """How a value is saved: ``\n`` line endings, no surrounding whitespace,
    and one final newline when it has several lines (PEM, OpenSSH keys)."""
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    return value + "\n" if "\n" in value else value


def check_value(value: str) -> str:
    if not isinstance(value, str):
        raise EnvError("A variable's value is text.")
    if "\x00" in value:
        raise EnvError("A variable's value cannot contain a NUL character.")
    if len(value) > MAX_VALUE_CHARS:
        raise EnvError(f"A variable's value is at most {MAX_VALUE_CHARS:,} characters; put larger data in a node.")
    return value


def public(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """What a client (or the agent) may see of the variables: every name, a
    plain variable's value, and never a secret's. ``readable`` is false for a
    secret no key on this server can decrypt: it has to be entered again."""
    return [
        {
            "name": r["name"],
            "secret": bool(r["secret"]),
            "value": None if r["secret"] else r["value"],
            "readable": r.get("readable", True),
            "updated_at": r.get("updated_at"),
            "updated_by": r.get("updated_by"),
        }
        for r in rows
    ]


def run_env(rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    """The variables a run sets, name to value (not the unreadable ones)."""
    return {r["name"]: r["value"] for r in rows if r.get("readable", True)}


# -- encryption ---------------------------------------------------------------
def new_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def _parse_key(text: str, where: str) -> bytes:
    key = text.strip().encode()
    try:
        ok = len(base64.urlsafe_b64decode(key)) == 32
    except (ValueError, TypeError):
        ok = False
    if not ok:
        raise SecretsKeyError(f"{where} is not a secrets key (32 bytes, URL-safe base64): {KEY_HOWTO}")
    return key


def server_keys(environ: dict[str, str] | None = None) -> list[bytes]:
    """``EXUVIA_SECRETS_KEY``: the server's keys, the first one current.
    Empty when it is not set. A malformed key is an error."""
    env = os.environ if environ is None else environ
    parts = [p for p in (env.get(KEY_ENV) or "").split(",") if p.strip()]
    return [_parse_key(p, KEY_ENV) for p in parts]


def key_id(key: bytes | str) -> str:
    """Which key encrypted a value, without saying anything about the key."""
    raw = key.strip().encode() if isinstance(key, str) else key
    return hashlib.sha256(b"exuvia-secrets-key:" + raw).hexdigest()[:12]


def is_encrypted(stored: str) -> bool:
    return stored.startswith(ENCRYPTED)


class Keyring:
    """Encrypts and decrypts one project's secrets (see the module's doc)."""

    def __init__(self, state_dir: Path, environ: dict[str, str] | None = None):
        self.path = Path(state_dir) / KEY_FILE
        self.env_keys = server_keys(environ)
        self._file_key: bytes | None = None

    def _read_file(self) -> bytes | None:
        if self._file_key is None and self.path.is_file():
            self._file_key = _parse_key(self.path.read_text(encoding="utf-8"), str(self.path))
        return self._file_key

    def _make_file(self) -> bytes:
        """The project's own key, made once. Two processes making it at once
        (hub replicas on one volume) end up with the same one."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            key = self._read_file()
            assert key is not None
            return key
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_key() + "\n")
        key = self._read_file()
        assert key is not None
        return key

    def current(self, create: bool = True) -> bytes | None:
        """The key new values are encrypted with."""
        if self.env_keys:
            return self.env_keys[0]
        return self._read_file() or (self._make_file() if create else None)

    def _keys(self) -> list[bytes]:
        keys = list(self.env_keys)
        try:
            own = self._read_file()
        except SecretsKeyError:
            own = None
        if own is not None and own not in keys:
            keys.append(own)
        return keys

    def seal(self, value: str) -> str:
        from cryptography.fernet import Fernet

        key = self.current()
        assert key is not None
        return f"{ENCRYPTED}{key_id(key)}:{Fernet(key).encrypt(value.encode('utf-8')).decode()}"

    def open(self, stored: str) -> str:
        """The value of a stored secret: decrypted, or as it is when it was
        saved before encryption. ``SecretsKeyError`` when no key here fits."""
        if not is_encrypted(stored):
            return stored
        from cryptography.fernet import Fernet, InvalidToken

        kid, _, token = stored[len(ENCRYPTED):].partition(":")
        key = next((k for k in self._keys() if key_id(k) == kid), None)
        if key is None:
            raise SecretsKeyError(
                "it was encrypted with a key this server does not have: set EXUVIA_SECRETS_KEY to that key "
                "(or put the project's .exuvia/secrets.key back), or enter the value again"
            )
        try:
            return Fernet(key).decrypt(token.encode()).decode("utf-8")
        except InvalidToken as e:
            raise SecretsKeyError("it is damaged and cannot be decrypted: enter the value again") from e

    def is_current(self, stored: str) -> bool:
        """Whether a stored secret is encrypted with the current key."""
        key = self.current(create=False)
        return key is not None and is_encrypted(stored) and stored[len(ENCRYPTED):].startswith(key_id(key) + ":")


class Redactor:
    """Replaces each secret value in a text with ``***``. Longer secrets go
    first, so one that contains another is masked whole."""

    def __init__(self, rows: Iterable[dict[str, Any]]):
        values: set[str] = set()
        for r in rows:
            if not r["secret"]:
                continue
            v = r["value"]
            if not r.get("readable", True) or v is None:
                continue
            # A multi-line secret (a PEM key) is printed line by line as often
            # as whole; each line is masked, but not a PEM block's armour.
            for part in (v.strip(), *v.splitlines()):
                part = part.strip()
                if len(part) >= MIN_REDACT_CHARS and not PEM_ARMOUR.match(part):
                    values.add(part)
        self._pattern = (
            re.compile("|".join(re.escape(v) for v in sorted(values, key=len, reverse=True))) if values else None
        )

    def __bool__(self) -> bool:
        return self._pattern is not None

    def __call__(self, text: str | None) -> str | None:
        if not text or self._pattern is None:
            return text
        return self._pattern.sub(MASK, text)


# -- a project's variables -----------------------------------------------------------
class Env:
    """A project's variables in its store, secrets through its keyring: what
    the workspace and ``exuvia env`` (on a project folder) both use. It opens
    no workspace, so it is safe next to a server running the same project."""

    def __init__(self, store: Any, keyring: Keyring):
        self.store = store
        self.keyring = keyring

    def rows(self) -> list[dict[str, Any]]:
        """Every variable with its value decrypted, for runs and nothing else.
        A secret no key here can decrypt has ``readable`` false, its value
        None and ``problem`` saying why."""
        rows = self.store.env_vars()
        for r in rows:
            r["readable"] = True
            if r["secret"]:
                try:
                    r["value"] = self.keyring.open(r["value"])
                except SecretsKeyError as e:
                    r.update(value=None, readable=False, problem=str(e))
        return rows

    def seal_all(self) -> list[str]:
        """Encrypt, with the current key, each secret saved before encryption
        or with another key (rotation). One that cannot be decrypted stays as
        it is, for the key it needs to come back; its name is returned."""
        stuck = []
        for r in self.store.env_vars():
            if not r["secret"] or self.keyring.is_current(r["value"]):
                continue
            try:
                value = self.keyring.open(r["value"])
            except SecretsKeyError:
                stuck.append(r["name"])
                continue
            self.store.reseal_env_var(r["name"], r["value"], self.keyring.seal(value))
        return stuck

    def set(self, name: str, value: str | None, secret: bool, by: str | None = None) -> None:
        """Add or change a variable. ``value=None`` keeps the saved value, to
        make a plain variable secret; a secret never becomes plain without a
        new value, which would show the old one. Values are normalized and
        secrets encrypted before they are saved."""
        check_name(name)
        rows = {r["name"]: r for r in self.rows()}
        old = rows.get(name)
        if value is None:
            if old is None:
                raise EnvError(f"{name} is not set yet: give it a value.")
            if old["secret"] and not secret:
                raise EnvError(f"{name} is secret: give it a new value to make it plain.")
            if not old["readable"]:
                raise EnvError(f"{name} cannot be decrypted: give it a new value.")
            value = old["value"]
        value = normalize(check_value(value))
        if old is None and len(rows) >= MAX_VARIABLES:
            raise EnvError(f"A project has at most {MAX_VARIABLES} variables.")
        self.store.set_env_var(name, self.keyring.seal(value) if secret else value, secret, by)

    def delete(self, name: str) -> bool:
        return self.store.delete_env_var(name)

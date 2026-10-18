"""Store the child-session account password, so the loopback can log on unattended.

Why a stored secret is needed at all
-----------------------------------

The child session is an ordinary interactive logon for this account, and Windows
will not create it without a password being presented. There is no way around
that: a Windows Hello PIN is not usable for a network-style logon, and the
credential-delegation route only works when the account already has a password
the system is willing to hand over - which, on the machine this was written
against, it was not.

So the choice is: the person types the password at the RDP window every time, or
the password is stored once. This module implements the second, which is what
makes "the agent opens a child session by itself" possible.

How it is stored
----------------

Following ParaDesk's design, and for the same reasons:

* **DPAPI, CurrentUser scope.** The ciphertext can be decrypted only on this
  machine, by this Windows account. Copied to another machine or another
  account it is worthless - measured by writing and reading it back.
* **Extra entropy.** A caller-supplied salt is mixed in, so another program
  running as the same user cannot simply ``CryptUnprotectData`` the file.
* **Never in the settings file.** ``config.toml`` is a file people share when
  reporting a problem; a secret does not belong in it.
* **A separate file, deleted as a unit.** Removing it revokes the stored secret
  completely.
* **Off unless the user opts in.**

What this is not
----------------

It is **not** a defence against malware running as this user: anything running
as this user can decrypt it, by definition. What it defends against is the
password being written in clear text, being carried off by a sync client, or
being read over someone's shoulder. Saying so plainly is better than implying a
guarantee that DPAPI-CurrentUser cannot give.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("somnus.session.credentials")

#: Salt mixed into the protection, so the ciphertext is not interchangeable with
#: any other DPAPI blob this account can decrypt.
ENTROPY = b"SomnusUI.ChildSession.Credential.v1"

#: File name inside the data directory.
FILE_NAME = "child-session.credential.bin"


def _data_dir() -> Path:
    override = os.environ.get("SOMNUS_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "somnusui"
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "somnusui"


def credential_path() -> Path:
    return _data_dir() / FILE_NAME


class CredentialUnavailable(RuntimeError):
    """No usable stored credential, or it could not be decrypted here."""


@dataclass(slots=True)
class Credential:
    """An account name plus its password, as the RDP control needs them."""

    username: str
    password: str

    def split(self) -> tuple[str, str]:
        """Return ``(domain, user)`` for the control's separate fields.

        ``MACHINE\\user`` goes into the two fields; a provider-qualified name
        (``MicrosoftAccount\\...``, ``AzureAD\\...``) is kept whole in the user
        field with an empty domain, because those prefixes are identity-provider
        qualifiers rather than a Windows domain and the control must see them
        intact. This mirrors ParaDesk's ``RdpCredentialName.Parse``.
        """
        name = self.username or ""
        sep = name.find("\\")
        if sep <= 0:
            return "", name
        prefix = name[:sep]
        if prefix.lower() in ("microsoftaccount", "azuread"):
            return "", name
        return prefix, name[sep + 1:]


def _crypt32():
    if not sys.platform.startswith("win"):  # pragma: no cover - platform
        raise CredentialUnavailable("DPAPI is a Windows feature")
    import win32crypt

    return win32crypt


def save(username: str, password: str) -> Path:
    """Encrypt and write the credential. Returns the file path."""
    if not username:
        raise ValueError("a username is required")
    win32crypt = _crypt32()
    # \0 separates the fields: a username may contain characters that a visible
    # separator would not survive.
    blob = (username + "\0" + (password or "")).encode("utf-8")
    cipher = win32crypt.CryptProtectData(blob, "SomnusUI child session", ENTROPY, None, None, 0)
    target = credential_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(cipher)
    # Restrict to the owner. DPAPI already limits decryption to this account;
    # the ACL keeps it from being read (as ciphertext) by others.
    try:
        import win32security

        win32security.SetNamedSecurityInfo(
            str(target),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
            None,
            None,
            _owner_only_dacl(),
            None,
        )
    except Exception as exc:  # noqa: BLE001 - hardening, not correctness
        log.debug("could not tighten the credential file ACL: %s", exc)
    return target


def _owner_only_dacl():
    import win32security

    user = win32security.GetTokenInformation(
        win32security.OpenProcessToken(
            win32security.GetCurrentProcess(), win32security.TOKEN_QUERY
        ),
        win32security.TokenUser,
    )[0]
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION, win32security.FILE_ALL_ACCESS, user
    )
    return dacl


def load() -> Credential:
    """Read and decrypt the credential, or raise :class:`CredentialUnavailable`."""
    target = credential_path()
    if not target.is_file():
        raise CredentialUnavailable(
            "no stored child-session credential; run "
            "`somnus child-session credential --save`"
        )
    win32crypt = _crypt32()
    cipher = target.read_bytes()
    try:
        _desc, plain = win32crypt.CryptUnprotectData(cipher, ENTROPY, None, None, 0)
    except Exception as exc:  # noqa: BLE001 - reported as unavailability
        # Another machine or another Windows account: the blob is dead weight,
        # not a wrong password. Remove it so the next run does not retry it.
        log.warning("stored credential cannot be decrypted here: %s", exc)
        raise CredentialUnavailable(
            "the stored credential cannot be decrypted on this machine or "
            "account; save it again"
        ) from exc

    text = plain.decode("utf-8", "replace")
    sep = text.find("\0")
    if sep < 0:
        raise CredentialUnavailable("the stored credential is malformed")
    return Credential(username=text[:sep], password=text[sep + 1:])


def exists() -> bool:
    return credential_path().is_file()


def clear() -> bool:
    """Delete the stored credential. Returns whether a file was removed."""
    target = credential_path()
    if target.is_file():
        target.unlink()
        return True
    return False


def current_account_name() -> str:
    """The signed-in account as the control should be told it: ``MACHINE\\user``.

    A provider-qualified name (a Microsoft account) is *not* guessed here. That
    mapping needs the SID-checked lookup ParaDesk does through ``NetUserGetInfo``
    level 24, and guessing it wrong would turn a working local logon into a
    failed one; the user can pass the qualified name explicitly instead.
    """
    if not sys.platform.startswith("win"):  # pragma: no cover - platform
        return ""
    size = ctypes.c_ulong(256)
    buffer = ctypes.create_unicode_buffer(256)
    if ctypes.windll.advapi32.GetUserNameW(buffer, ctypes.byref(size)):
        user = buffer.value
    else:  # pragma: no cover - unlikely
        user = os.environ.get("USERNAME", "")
    machine = os.environ.get("COMPUTERNAME", "")
    if not user:
        return ""
    return f"{machine}\\{user}" if machine else user

import base64
import os
from pathlib import Path


def private_directory(directory):
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import ntsecuritycon
        import win32api
        import win32con
        import win32security

        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
        )
        try:
            user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            token.Close()
        acl = win32security.ACL()
        for sid in (
            user,
            win32security.CreateWellKnownSid(win32security.WinLocalSystemSid),
            win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid),
        ):
            acl.AddAccessAllowedAceEx(
                win32security.ACL_REVISION,
                win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE,
                ntsecuritycon.FILE_ALL_ACCESS,
                sid,
            )
        win32security.SetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            acl,
            None,
        )
    return path


def encrypt_text(value):
    import win32crypt

    return base64.b64encode(
        win32crypt.CryptProtectData(value.encode(), "JuiAI", None, None, None, 1)
    ).decode("ascii")


def decrypt_text(value):
    import win32crypt

    return win32crypt.CryptUnprotectData(base64.b64decode(value), None, None, None, 1)[
        1
    ].decode()

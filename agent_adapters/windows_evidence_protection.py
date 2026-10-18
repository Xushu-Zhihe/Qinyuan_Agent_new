"""Current-user DPAPI and an exact SID-only Windows directory DACL.

This adapter contains no evidence identifiers, metadata, or operator policy.
It accepts bytes in memory and never formats provider errors or path values.
"""

from __future__ import annotations

import ctypes as c
import os
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes as w
from pathlib import Path
from qinyuan_core import CodedError

_TOKEN_QUERY = 0x0008
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_DACL_PROTECTED = 0x1000
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_ALL_ACCESS = 0x001F01FF
_INHERIT_FLAGS = 0x03  # OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class WindowsEvidenceProtectionError(CodedError):
    """A fixed error code which is safe for an ordinary diagnostic channel."""


class _Blob(c.Structure):
    _fields_ = [("size", w.DWORD), ("data", c.c_void_p)]


class _SidAndAttributes(c.Structure):
    _fields_ = [("sid", c.c_void_p), ("attributes", w.DWORD)]


class _TokenUser(c.Structure):
    _fields_ = [("user", _SidAndAttributes)]


class _FileAttributes(c.Structure):
    _fields_ = [("attributes", w.DWORD), ("reparse_tag", w.DWORD)]


class _AclSize(c.Structure):
    _fields_ = [
        ("ace_count", w.DWORD),
        ("bytes_in_use", w.DWORD),
        ("bytes_free", w.DWORD),
    ]


class _AceHeader(c.Structure):
    _fields_ = [("kind", w.BYTE), ("flags", w.BYTE), ("size", w.WORD)]


class _AllowedAce(c.Structure):
    _fields_ = [("header", _AceHeader), ("mask", w.DWORD), ("sid_start", w.DWORD)]


class WindowsEvidenceProtection:
    """Protect for the current user and restrict an existing local directory.

    Directory operations pin each path component, reject reparse points, and
    use handle-based security operations. No directory is created or deleted.
    """

    def __init__(self) -> None:
        if os.name != "nt":
            raise WindowsEvidenceProtectionError("windows_protection_unavailable")
        try:
            self._kernel = c.WinDLL("kernel32", use_last_error=True)
            self._security = c.WinDLL("advapi32", use_last_error=True)
            self._crypt = c.WinDLL("crypt32", use_last_error=True)
            self._configure_api()
            self._identity = self._read_identity()
        except WindowsEvidenceProtectionError:
            raise
        except (AttributeError, OSError, ValueError):
            raise WindowsEvidenceProtectionError(
                "windows_protection_unavailable"
            ) from None

    @property
    def identity(self) -> str:
        """The process user's SID, without a mutable user-name lookup."""
        return self._identity

    def protect(self, value: bytes, entropy: bytes) -> bytes:
        """Use current-user DPAPI, with no machine scope and no UI prompt."""
        return self._transform(value, entropy, decrypt=False)

    def unprotect(self, value: bytes, entropy: bytes) -> bytes:
        """Wrong entropy, damaged data, and another user all fail closed."""
        return self._transform(value, entropy, decrypt=True)

    def restrict_directory(self, directory: Path) -> None:
        """Replace the directory DACL with one protected SID-only grant."""
        descriptor = c.c_void_p()
        try:
            with self._directory_handle(directory, writable=True) as handle:
                self._verify_owner(handle)
                if not self._security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    f"D:P(A;OICI;FA;;;{self.identity})", 1, c.byref(descriptor), None
                ):
                    raise WindowsEvidenceProtectionError("protected_directory_failed")
                dacl = self._descriptor_dacl(descriptor)
                result = self._security.SetSecurityInfo(
                    handle,
                    1,
                    _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                    None,
                    None,
                    dacl,
                    None,
                )
                if result != 0:
                    raise WindowsEvidenceProtectionError("protected_directory_failed")
                self._verify_handle(handle)
        finally:
            if descriptor.value:
                self._kernel.LocalFree(descriptor)

    def verify_directory(self, directory: Path) -> None:
        """Reject broadened, inherited, missing, or non-directory protection."""
        with self._directory_handle(directory, writable=False) as handle:
            self._verify_handle(handle)

    def _configure_api(self) -> None:
        pointer = c.c_void_p
        ppointer = c.POINTER(pointer)
        self._kernel.GetCurrentProcess.argtypes = []
        self._kernel.GetCurrentProcess.restype = w.HANDLE
        self._kernel.CloseHandle.argtypes = [w.HANDLE]
        self._kernel.CloseHandle.restype = w.BOOL
        self._kernel.LocalFree.argtypes = [pointer]
        self._kernel.LocalFree.restype = pointer
        self._kernel.CreateFileW.argtypes = [
            w.LPCWSTR,
            w.DWORD,
            w.DWORD,
            pointer,
            w.DWORD,
            w.DWORD,
            w.HANDLE,
        ]
        self._kernel.CreateFileW.restype = w.HANDLE
        self._kernel.GetFileInformationByHandleEx.argtypes = [
            w.HANDLE,
            c.c_int,
            pointer,
            w.DWORD,
        ]
        self._kernel.GetFileInformationByHandleEx.restype = w.BOOL
        self._security.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ppointer]
        self._security.OpenProcessToken.restype = w.BOOL
        self._security.GetTokenInformation.argtypes = [
            w.HANDLE,
            c.c_int,
            pointer,
            w.DWORD,
            c.POINTER(w.DWORD),
        ]
        self._security.GetTokenInformation.restype = w.BOOL
        self._security.ConvertSidToStringSidW.argtypes = [pointer, ppointer]
        self._security.ConvertSidToStringSidW.restype = w.BOOL
        self._security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            w.LPCWSTR,
            w.DWORD,
            ppointer,
            c.POINTER(w.DWORD),
        ]
        self._security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
            w.BOOL
        )
        self._security.GetSecurityDescriptorDacl.argtypes = [
            pointer,
            c.POINTER(w.BOOL),
            ppointer,
            c.POINTER(w.BOOL),
        ]
        self._security.GetSecurityDescriptorDacl.restype = w.BOOL
        self._security.GetSecurityDescriptorControl.argtypes = [
            pointer,
            c.POINTER(w.WORD),
            c.POINTER(w.DWORD),
        ]
        self._security.GetSecurityDescriptorControl.restype = w.BOOL
        self._security.GetSecurityInfo.argtypes = [
            w.HANDLE,
            c.c_int,
            w.DWORD,
            ppointer,
            ppointer,
            ppointer,
            ppointer,
            ppointer,
        ]
        self._security.GetSecurityInfo.restype = w.DWORD
        self._security.SetSecurityInfo.argtypes = [
            w.HANDLE,
            c.c_int,
            w.DWORD,
            pointer,
            pointer,
            pointer,
            pointer,
        ]
        self._security.SetSecurityInfo.restype = w.DWORD
        self._security.IsValidAcl.argtypes = [pointer]
        self._security.IsValidAcl.restype = w.BOOL
        self._security.IsValidSid.argtypes = [pointer]
        self._security.IsValidSid.restype = w.BOOL
        self._security.GetAclInformation.argtypes = [pointer, pointer, w.DWORD, c.c_int]
        self._security.GetAclInformation.restype = w.BOOL
        self._security.GetAce.argtypes = [pointer, w.DWORD, ppointer]
        self._security.GetAce.restype = w.BOOL
        for name in ("CryptProtectData", "CryptUnprotectData"):
            function = getattr(self._crypt, name)
            function.argtypes = [
                pointer,
                pointer,
                pointer,
                pointer,
                pointer,
                w.DWORD,
                pointer,
            ]
            function.restype = w.BOOL

    def _read_identity(self) -> str:
        token = c.c_void_p()
        if not self._security.OpenProcessToken(
            self._kernel.GetCurrentProcess(), _TOKEN_QUERY, c.byref(token)
        ):
            raise WindowsEvidenceProtectionError("windows_identity_unavailable")
        try:
            required = w.DWORD()
            self._security.GetTokenInformation(token, 1, None, 0, c.byref(required))
            if not required.value:
                raise WindowsEvidenceProtectionError("windows_identity_unavailable")
            buffer = c.create_string_buffer(required.value)
            if not self._security.GetTokenInformation(
                token, 1, buffer, required.value, c.byref(required)
            ):
                raise WindowsEvidenceProtectionError("windows_identity_unavailable")
            user = c.cast(buffer, c.POINTER(_TokenUser)).contents
            return self._sid_string(user.user.sid)
        finally:
            self._kernel.CloseHandle(token)

    def _sid_string(self, sid: int) -> str:
        text = c.c_void_p()
        if not self._security.IsValidSid(sid):
            raise WindowsEvidenceProtectionError("windows_identity_unavailable")
        try:
            if not self._security.ConvertSidToStringSidW(sid, c.byref(text)):
                raise WindowsEvidenceProtectionError("windows_identity_unavailable")
            return c.wstring_at(text)
        finally:
            if text.value:
                self._kernel.LocalFree(text)

    def _transform(self, value: bytes, entropy: bytes, *, decrypt: bool) -> bytes:
        if type(value) is not bytes or type(entropy) is not bytes:
            raise WindowsEvidenceProtectionError("protected_data_invalid")
        if len(value) > 0xFFFFFFFF or len(entropy) > 0xFFFFFFFF:
            raise WindowsEvidenceProtectionError("protected_data_invalid")
        value_buffer = c.create_string_buffer(value)
        entropy_buffer = c.create_string_buffer(entropy)
        source = _Blob(len(value), c.cast(value_buffer, c.c_void_p))
        binding = _Blob(len(entropy), c.cast(entropy_buffer, c.c_void_p))
        result = _Blob()
        failure = "protected_data_unavailable" if decrypt else "protected_data_failed"
        try:
            function = (
                self._crypt.CryptUnprotectData
                if decrypt
                else self._crypt.CryptProtectData
            )
            if not function(
                c.byref(source),
                None,
                c.byref(binding),
                None,
                None,
                _CRYPTPROTECT_UI_FORBIDDEN,
                c.byref(result),
            ):
                raise WindowsEvidenceProtectionError(failure)
            if result.size and not result.data:
                raise WindowsEvidenceProtectionError(failure)
            return c.string_at(result.data, result.size) if result.size else b""
        finally:
            c.memset(value_buffer, 0, c.sizeof(value_buffer))
            c.memset(entropy_buffer, 0, c.sizeof(entropy_buffer))
            if result.data:
                c.memset(result.data, 0, result.size)
                self._kernel.LocalFree(result.data)

    @contextmanager
    def _directory_handle(self, directory: Path, *, writable: bool) -> Iterator[int]:
        if not isinstance(directory, Path):
            raise WindowsEvidenceProtectionError("protected_directory_invalid")
        raw = str(directory)
        if (
            "\x00" in raw
            or raw.startswith("\\\\")
            or ":" in raw[2:]
            or ".." in directory.parts
        ):
            raise WindowsEvidenceProtectionError("protected_directory_invalid")
        try:
            path = Path(os.path.abspath(directory))
        except (OSError, ValueError):
            raise WindowsEvidenceProtectionError(
                "protected_directory_invalid"
            ) from None
        if path.parent == path:
            raise WindowsEvidenceProtectionError("protected_directory_invalid")
        handles: list[int] = []
        try:
            components = (*reversed(path.parents), path)
            for component in components:
                # LIST_DIRECTORY is needed for Windows to enforce share access;
                # metadata-only handles do not block a concurrent reparse writer.
                access = 0x81  # FILE_READ_ATTRIBUTES | FILE_LIST_DIRECTORY
                if component == path:
                    access |= _READ_CONTROL | (_WRITE_DAC if writable else 0)
                handle = self._kernel.CreateFileW(
                    str(component),
                    access,
                    0x01,  # FILE_SHARE_READ: also deny in-place reparse changes
                    None,
                    3,
                    0x02000000 | 0x00200000,
                    None,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
                )
                if handle is None or handle == c.c_void_p(-1).value:
                    raise WindowsEvidenceProtectionError(
                        "protected_directory_unavailable"
                    )
                handles.append(handle)
                attributes = _FileAttributes()
                if not self._kernel.GetFileInformationByHandleEx(
                    handle, 9, c.byref(attributes), c.sizeof(attributes)
                ):
                    raise WindowsEvidenceProtectionError(
                        "protected_directory_unavailable"
                    )
                if (
                    not attributes.attributes & _FILE_ATTRIBUTE_DIRECTORY
                    or attributes.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise WindowsEvidenceProtectionError("protected_directory_invalid")
            yield handles[-1]
        finally:
            for handle in reversed(handles):
                self._kernel.CloseHandle(handle)

    def _descriptor_dacl(self, descriptor: c.c_void_p) -> c.c_void_p:
        present, defaulted = w.BOOL(), w.BOOL()
        dacl = c.c_void_p()
        if (
            not self._security.GetSecurityDescriptorDacl(
                descriptor, c.byref(present), c.byref(dacl), c.byref(defaulted)
            )
            or not present.value
            or not dacl.value
            or not self._security.IsValidAcl(dacl)
        ):
            raise WindowsEvidenceProtectionError("protected_directory_invalid")
        return dacl

    def _verify_handle(self, handle: int) -> None:
        descriptor, dacl, owner = c.c_void_p(), c.c_void_p(), c.c_void_p()
        try:
            if (
                self._security.GetSecurityInfo(
                    handle,
                    1,
                    _DACL_SECURITY_INFORMATION | _OWNER_SECURITY_INFORMATION,
                    c.byref(owner),
                    None,
                    c.byref(dacl),
                    None,
                    c.byref(descriptor),
                )
                != 0
            ):
                raise WindowsEvidenceProtectionError("protected_directory_unavailable")
            if not owner.value or self._sid_string(owner.value) != self.identity:
                raise WindowsEvidenceProtectionError("protected_directory_invalid")
            dacl = self._descriptor_dacl(descriptor)
            control, revision = w.WORD(), w.DWORD()
            if (
                not self._security.GetSecurityDescriptorControl(
                    descriptor, c.byref(control), c.byref(revision)
                )
                or not control.value & _SE_DACL_PROTECTED
            ):
                raise WindowsEvidenceProtectionError("protected_directory_invalid")
            size = _AclSize()
            if (
                not self._security.GetAclInformation(
                    dacl, c.byref(size), c.sizeof(size), 2
                )
                or size.ace_count != 1
            ):
                raise WindowsEvidenceProtectionError("protected_directory_invalid")
            ace_pointer = c.c_void_p()
            if (
                not self._security.GetAce(dacl, 0, c.byref(ace_pointer))
                or not ace_pointer.value
            ):
                raise WindowsEvidenceProtectionError("protected_directory_invalid")
            ace = c.cast(ace_pointer, c.POINTER(_AllowedAce)).contents
            if (
                ace.header.kind != 0  # ACCESS_ALLOWED_ACE_TYPE
                or ace.header.flags != _INHERIT_FLAGS
                or ace.mask != _FILE_ALL_ACCESS
                or ace.header.size < c.sizeof(_AllowedAce)
                or self._sid_string(ace_pointer.value + _AllowedAce.sid_start.offset)
                != self.identity
            ):
                raise WindowsEvidenceProtectionError("protected_directory_invalid")
        finally:
            if descriptor.value:
                self._kernel.LocalFree(descriptor)

    def _verify_owner(self, handle: int) -> None:
        descriptor, owner = c.c_void_p(), c.c_void_p()
        try:
            if (
                self._security.GetSecurityInfo(
                    handle,
                    1,
                    _OWNER_SECURITY_INFORMATION,
                    c.byref(owner),
                    None,
                    None,
                    None,
                    c.byref(descriptor),
                )
                != 0
            ):
                raise WindowsEvidenceProtectionError("protected_directory_unavailable")
            if not owner.value or self._sid_string(owner.value) != self.identity:
                raise WindowsEvidenceProtectionError("protected_directory_invalid")
        finally:
            if descriptor.value:
                self._kernel.LocalFree(descriptor)

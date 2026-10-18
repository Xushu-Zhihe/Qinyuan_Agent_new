"""Exercise real current-user DPAPI and Windows ACL enforcement."""

from __future__ import annotations

import ctypes as c
import os
import subprocess
from pathlib import Path

import pytest

from agent_adapters.windows_evidence_protection import (
    WindowsEvidenceProtection,
    WindowsEvidenceProtectionError,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows API required")


@pytest.fixture
def protection() -> WindowsEvidenceProtection:
    return WindowsEvidenceProtection()


def _replace_dacl(
    protection: WindowsEvidenceProtection, directory: Path, sddl: str
) -> None:
    """Test tampering uses the OS directly, without implementation policy."""
    descriptor = c.c_void_p()
    try:
        assert (
            protection._security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, c.byref(descriptor), None
            )
        )
        dacl = protection._descriptor_dacl(descriptor)
        with protection._directory_handle(directory, writable=True) as handle:
            assert (
                protection._security.SetSecurityInfo(
                    handle, 1, 0x80000004, None, None, dacl, None
                )
                == 0
            )
    finally:
        if descriptor.value:
            protection._kernel.LocalFree(descriptor)


def test_current_user_identity_is_stable(protection) -> None:
    assert protection.identity.startswith("S-1-")
    assert WindowsEvidenceProtection().identity == protection.identity


@pytest.mark.parametrize("value", (b"", b"test-evidence-value", bytes(range(256))))
def test_dpapi_round_trip_requires_same_entropy(protection, value) -> None:
    ciphertext = protection.protect(value, b"run-and-evidence-binding")

    assert ciphertext != value
    assert protection.unprotect(ciphertext, b"run-and-evidence-binding") == value
    with pytest.raises(
        WindowsEvidenceProtectionError, match="^protected_data_unavailable$"
    ):
        protection.unprotect(ciphertext, b"another-evidence-binding")


def test_dpapi_tamper_is_rejected_without_provider_details(protection) -> None:
    ciphertext = protection.protect(b"synthetic-evidence", b"binding")
    damaged = ciphertext[:-1] + bytes((ciphertext[-1] ^ 1,))

    for invalid in (b"", b"not-a-dpapi-value", damaged):
        with pytest.raises(WindowsEvidenceProtectionError) as caught:
            protection.unprotect(invalid, b"binding")
        assert str(caught.value) == "protected_data_unavailable"
        assert caught.value.__cause__ is None


def test_directory_requires_sid_only_protected_acl(protection, tmp_path) -> None:
    directory = tmp_path / "protected"
    directory.mkdir()
    with pytest.raises(WindowsEvidenceProtectionError):
        protection.verify_directory(directory)

    protection.restrict_directory(directory)
    protection.verify_directory(directory)
    protection.restrict_directory(directory)
    (directory / "cipher.bin").write_bytes(b"synthetic-ciphertext")
    assert (directory / "cipher.bin").read_bytes() == b"synthetic-ciphertext"


def test_new_evidence_file_inherits_only_current_user_acl(protection, tmp_path) -> None:
    directory = tmp_path / "protected"
    directory.mkdir()
    protection.restrict_directory(directory)
    file = directory / "cipher.bin"
    file.write_bytes(b"synthetic-ciphertext")
    security = c.WinDLL("advapi32", use_last_error=True)
    security.GetFileSecurityW.argtypes = [
        c.c_wchar_p,
        c.c_ulong,
        c.c_void_p,
        c.c_ulong,
        c.POINTER(c.c_ulong),
    ]
    security.GetFileSecurityW.restype = c.c_int
    security.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        c.c_void_p,
        c.c_ulong,
        c.c_ulong,
        c.POINTER(c.c_void_p),
        c.c_void_p,
    ]
    security.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = c.c_int
    required = c.c_ulong()
    security.GetFileSecurityW(str(file), 4, None, 0, c.byref(required))
    assert required.value
    descriptor = c.create_string_buffer(required.value)
    assert security.GetFileSecurityW(
        str(file), 4, descriptor, required.value, c.byref(required)
    )
    text = c.c_void_p()
    try:
        assert security.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, 1, 4, c.byref(text), None
        )
        sddl = c.wstring_at(text)
        assert sddl.count("(") == 1
        assert f"(A;ID;FA;;;{protection.identity})" in sddl
    finally:
        if text.value:
            protection._kernel.LocalFree(text)


def test_volume_root_is_rejected_before_acl_changes(protection, tmp_path) -> None:
    with pytest.raises(
        WindowsEvidenceProtectionError, match="^protected_directory_invalid$"
    ):
        protection.restrict_directory(Path(tmp_path.anchor))


def test_existing_directory_writer_blocks_verification(protection, tmp_path) -> None:
    directory = tmp_path / "protected"
    directory.mkdir()
    protection.restrict_directory(directory)
    handle = protection._kernel.CreateFileW(
        str(directory), 0x40000000, 0x07, None, 3, 0x02200000, None
    )
    assert handle is not None and handle != c.c_void_p(-1).value
    try:
        with pytest.raises(
            WindowsEvidenceProtectionError, match="^protected_directory_unavailable$"
        ):
            protection.verify_directory(directory)
    finally:
        protection._kernel.CloseHandle(handle)
    protection.verify_directory(directory)


def test_directory_writer_cannot_open_during_verification(
    protection, tmp_path, monkeypatch
) -> None:
    directory = tmp_path / "protected"
    directory.mkdir()
    protection.restrict_directory(directory)
    original = protection._verify_handle
    attempts = []

    def verify_and_try_write(handle):
        writer = protection._kernel.CreateFileW(
            str(directory), 0x40000000, 0x07, None, 3, 0x02200000, None
        )
        if writer is not None and writer != c.c_void_p(-1).value:
            protection._kernel.CloseHandle(writer)
            pytest.fail("directory writer was admitted during verification")
        attempts.append(c.get_last_error())
        original(handle)

    monkeypatch.setattr(protection, "_verify_handle", verify_and_try_write)
    protection.verify_directory(directory)
    assert attempts == [32]  # ERROR_SHARING_VIOLATION


@pytest.mark.parametrize(
    "suffix",
    (
        "(A;OICI;FA;;;WD)",
        "(A;OICI;FR;;;WD)",
    ),
)
def test_broadened_acl_is_rejected(protection, tmp_path, suffix) -> None:
    directory = tmp_path / "protected"
    directory.mkdir()
    protection.restrict_directory(directory)
    _replace_dacl(
        protection, directory, f"D:P(A;OICI;FA;;;{protection.identity}){suffix}"
    )

    with pytest.raises(
        WindowsEvidenceProtectionError, match="^protected_directory_invalid$"
    ):
        protection.verify_directory(directory)
    protection.restrict_directory(directory)
    protection.verify_directory(directory)


def test_non_inheritable_acl_is_rejected(protection, tmp_path) -> None:
    directory = tmp_path / "protected"
    directory.mkdir()
    protection.restrict_directory(directory)
    _replace_dacl(protection, directory, f"D:P(A;;FA;;;{protection.identity})")

    with pytest.raises(
        WindowsEvidenceProtectionError, match="^protected_directory_invalid$"
    ):
        protection.verify_directory(directory)


def test_missing_and_file_paths_fail_without_path_content(protection, tmp_path) -> None:
    file = tmp_path / "private-path-name"
    file.write_bytes(b"synthetic-data")
    for directory in (file, tmp_path / "missing-private-path-name"):
        for operation in (protection.verify_directory, protection.restrict_directory):
            with pytest.raises(WindowsEvidenceProtectionError) as caught:
                operation(directory)
            assert "private-path-name" not in str(caught.value)
    assert file.read_bytes() == b"synthetic-data"


def test_junction_and_its_children_are_rejected(protection, tmp_path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    child = target / "child"
    child.mkdir()
    protection.restrict_directory(target)
    protection.restrict_directory(child)
    junction = tmp_path / "junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    try:
        for directory in (junction, junction / "child"):
            for operation in (
                protection.verify_directory,
                protection.restrict_directory,
            ):
                with pytest.raises(
                    WindowsEvidenceProtectionError,
                    match="^protected_directory_invalid$",
                ):
                    operation(directory)
        protection.verify_directory(target)
        protection.verify_directory(child)
    finally:
        junction.rmdir()

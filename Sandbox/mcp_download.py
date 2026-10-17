"""MCP Download Channel with SHA256 Validation.

Strict security boundary: Only HTTPS downloads with mandatory SHA256 validation.
Rejects unsigned or mismatched downloads before they reach the sandbox.

Design Principle: Strict about system - enforce cryptographic verification, no bypass.
"""

from __future__ import annotations

import hashlib
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class DownloadValidationError(Exception):
    """Download failed security validation."""

    pass


@dataclass(frozen=True)
class DownloadRequest:
    """Request to download and validate a file.

    Security boundaries:
    - url: Must be HTTPS (no HTTP, file://, ftp://)
    - expected_sha256: Mandatory (no unsigned downloads)
    - destination: Must be inside tmpfs workspace
    """

    url: str
    expected_sha256: str  # Hex-encoded SHA256 hash
    destination: Path  # Where to write validated file
    timeout_seconds: float = 30.0
    max_size_bytes: int = 100 * 1024 * 1024  # 100 MB default limit

    def __post_init__(self) -> None:
        # Validate URL is HTTPS-only
        if not self.url.startswith("https://"):
            raise DownloadValidationError(
                f"Only HTTPS downloads allowed, got: {self.url[:50]}"
            )

        # Validate SHA256 format (64 hex chars)
        if not isinstance(self.expected_sha256, str):
            raise DownloadValidationError("expected_sha256 must be string")
        if len(self.expected_sha256) != 64:
            raise DownloadValidationError(
                f"expected_sha256 must be 64 hex chars, got {len(self.expected_sha256)}"
            )
        try:
            int(self.expected_sha256, 16)
        except ValueError:
            raise DownloadValidationError(
                f"expected_sha256 must be hex string: {self.expected_sha256[:16]}..."
            )

        # Validate destination is absolute
        if not self.destination.is_absolute():
            raise DownloadValidationError(
                f"destination must be absolute path: {self.destination}"
            )


@dataclass(frozen=True)
class DownloadResult:
    """Result of validated download."""

    path: Path  # Where file was written
    actual_sha256: str  # Computed hash (matches expected_sha256)
    size_bytes: int  # Downloaded file size
    url: str  # Source URL


class MCPDownloadChannel:
    """Secure download channel with mandatory SHA256 validation.

    Strict security policy:
    1. HTTPS-only (no HTTP, file://, or other schemes)
    2. Mandatory SHA256 validation (no unsigned downloads)
    3. Size limits enforced (reject oversized downloads)
    4. Timeout enforced (prevent hanging downloads)
    5. Atomic write (temp file → validated rename)

    Never allows bypassing validation or downloading to arbitrary paths.
    """

    def __init__(self, workspace_root: Path) -> None:
        """
        Args:
            workspace_root: Tmpfs workspace root (all downloads go here)
        """
        if not workspace_root.is_absolute():
            raise ValueError(f"workspace_root must be absolute: {workspace_root}")
        self.workspace_root = workspace_root

    def download_and_validate(self, request: DownloadRequest) -> DownloadResult:
        """
        Download file from HTTPS URL and validate SHA256 hash.

        Strict validation:
        - URL must be HTTPS
        - expected_sha256 mandatory
        - destination must be inside workspace_root
        - Size limit enforced
        - Hash mismatch → exception, file deleted

        Args:
            request: DownloadRequest with URL and expected hash

        Returns:
            DownloadResult with validated path and hash

        Raises:
            DownloadValidationError: URL not HTTPS, hash mismatch, size exceeded
            urllib.error.URLError: Network error
            OSError: File write error
        """
        # Validate destination is inside workspace
        try:
            request.destination.resolve().relative_to(self.workspace_root.resolve())
        except ValueError:
            raise DownloadValidationError(
                f"destination must be inside workspace: {request.destination}"
            )

        # Create parent directory if needed
        request.destination.parent.mkdir(parents=True, exist_ok=True)

        # Download to temporary file first
        temp_path = request.destination.with_suffix(".tmp")
        try:
            self._download_to_temp(request, temp_path)
            actual_hash = self._compute_sha256(temp_path)

            # Strict validation: hash must match exactly
            if actual_hash != request.expected_sha256.lower():
                raise DownloadValidationError(
                    f"SHA256 mismatch for {request.url}\n"
                    f"Expected: {request.expected_sha256}\n"
                    f"Actual:   {actual_hash}"
                )

            # Validation passed - atomically rename to final destination
            temp_path.rename(request.destination)

            size = request.destination.stat().st_size
            return DownloadResult(
                path=request.destination,
                actual_sha256=actual_hash,
                size_bytes=size,
                url=request.url,
            )

        except Exception:
            # Clean up temp file on any error
            if temp_path.exists():
                temp_path.unlink()
            raise

    def _download_to_temp(self, request: DownloadRequest, temp_path: Path) -> None:
        """Download URL to temporary file with size limit."""
        # Strict timeout and size enforcement
        req = urllib.request.Request(
            request.url,
            headers={"User-Agent": "Qinyuan-Agent/D8"},
        )

        with urllib.request.urlopen(req, timeout=request.timeout_seconds) as response:
            content_length = response.getheader("Content-Length")
            if content_length:
                size = int(content_length)
                if size > request.max_size_bytes:
                    raise DownloadValidationError(
                        f"Download too large: {size} bytes > {request.max_size_bytes} limit"
                    )

            # Stream download with size tracking
            downloaded = 0
            chunk_size = 8192
            with open(temp_path, "wb") as f:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > request.max_size_bytes:
                        raise DownloadValidationError(
                            f"Download exceeded size limit: {downloaded} > {request.max_size_bytes}"
                        )
                    f.write(chunk)

    def _compute_sha256(self, path: Path) -> str:
        """Compute SHA256 hash of file."""
        hasher = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()


class DownloadRegistry(Protocol):
    """Registry of allowed downloads with their expected SHA256 hashes.

    Strict about system: only pre-approved downloads with known hashes allowed.
    """

    def get_expected_hash(self, url: str) -> str | None:
        """Get expected SHA256 for URL, or None if not registered."""
        ...

    def register_download(self, url: str, expected_sha256: str) -> None:
        """Register a new allowed download with its hash."""
        ...


class SimpleDownloadRegistry:
    """In-memory download registry."""

    def __init__(self) -> None:
        self._registry: dict[str, str] = {}

    def get_expected_hash(self, url: str) -> str | None:
        return self._registry.get(url)

    def register_download(self, url: str, expected_sha256: str) -> None:
        # Validate hash format
        if len(expected_sha256) != 64:
            raise ValueError(f"Invalid SHA256 hash: {expected_sha256}")
        try:
            int(expected_sha256, 16)
        except ValueError:
            raise ValueError(f"SHA256 must be hex string: {expected_sha256}")

        self._registry[url] = expected_sha256.lower()

    def is_registered(self, url: str) -> bool:
        """Check if URL is in registry."""
        return url in self._registry

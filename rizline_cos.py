"""Private Tencent COS delivery for generated RizLine score cards.

QQ's official group and C2C media APIs take a URL, rather than image bytes.
This module uploads a local PNG to a private COS bucket and returns a short
lived signed HTTPS URL that QQ can fetch immediately.  It never makes an
object publicly readable.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from qcloud_cos import CosConfig, CosS3Client


MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_OBJECT_PREFIX = "qq-rizline-b40"
DEFAULT_URL_EXPIRES_SECONDS = 600
OBJECT_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9_-]+\.png$")


class CosImageUploadError(RuntimeError):
    """Raised when a score card cannot safely be uploaded to COS."""


@dataclass(frozen=True)
class CosImageStorageSettings:
    """The least-privilege COS settings needed to deliver score card images."""

    bucket: str
    region: str
    secret_id: str = field(repr=False)
    secret_key: str = field(repr=False)
    prefix: str = DEFAULT_OBJECT_PREFIX
    url_expires_seconds: int = DEFAULT_URL_EXPIRES_SECONDS
    session_token: str = field(default="", repr=False)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "CosImageStorageSettings | None":
        values = environment if environment is not None else os.environ
        bucket = values.get("RIZLINE_COS_BUCKET", "").strip()
        region = values.get("RIZLINE_COS_REGION", "").strip()
        secret_id = values.get("RIZLINE_COS_SECRET_ID", "").strip()
        secret_key = values.get("RIZLINE_COS_SECRET_KEY", "").strip()
        session_token = values.get("RIZLINE_COS_SESSION_TOKEN", "").strip()
        configured_values = (bucket, region, secret_id, secret_key, session_token)
        if not any(configured_values):
            return None

        missing = [
            name
            for name, value in (
                ("RIZLINE_COS_BUCKET", bucket),
                ("RIZLINE_COS_REGION", region),
                ("RIZLINE_COS_SECRET_ID", secret_id),
                ("RIZLINE_COS_SECRET_KEY", secret_key),
            )
            if not value
        ]
        if missing:
            raise ValueError("Missing " + ", ".join(missing) + " for Tencent COS image delivery.")

        prefix = values.get("RIZLINE_COS_PREFIX", DEFAULT_OBJECT_PREFIX).strip().strip("/")
        if (
            not prefix
            or not OBJECT_PREFIX_RE.fullmatch(prefix)
            or any(part in {"", ".", ".."} for part in prefix.split("/"))
        ):
            raise ValueError("RIZLINE_COS_PREFIX must be a safe object-key prefix without '..'.")

        expires_text = values.get(
            "RIZLINE_COS_URL_EXPIRES_SECONDS", str(DEFAULT_URL_EXPIRES_SECONDS)
        ).strip()
        try:
            expires = int(expires_text)
        except ValueError as exc:
            raise ValueError("RIZLINE_COS_URL_EXPIRES_SECONDS must be an integer from 60 to 3600.") from exc
        if not 60 <= expires <= 3600:
            raise ValueError("RIZLINE_COS_URL_EXPIRES_SECONDS must be from 60 to 3600.")

        return cls(
            bucket=bucket,
            region=region,
            secret_id=secret_id,
            secret_key=secret_key,
            session_token=session_token,
            prefix=prefix,
            url_expires_seconds=expires,
        )


class CosImageUploader:
    """Upload a generated PNG to COS and return a time-limited download URL."""

    def __init__(self, settings: CosImageStorageSettings, *, client: Any | None = None) -> None:
        self._settings = settings
        if client is None:
            config = CosConfig(
                Region=settings.region,
                SecretId=settings.secret_id,
                SecretKey=settings.secret_key,
                Token=settings.session_token or None,
                Scheme="https",
                Timeout=30,
            )
            client = CosS3Client(config)
        self._client = client

    def upload_and_get_url(self, image_path: Path) -> str:
        """Store one locally rendered score card and sign a short-lived URL."""
        try:
            resolved_path = image_path.resolve(strict=True)
            size = resolved_path.stat().st_size
        except OSError as exc:
            raise CosImageUploadError("The generated score image is unavailable.") from exc
        if not SAFE_FILENAME_RE.fullmatch(resolved_path.name):
            raise CosImageUploadError("Refusing to upload an unexpected score image filename.")
        if not 0 < size <= MAX_IMAGE_BYTES:
            raise CosImageUploadError("The generated score image has an unsupported size.")

        key = f"{self._settings.prefix}/{resolved_path.name}"
        try:
            with resolved_path.open("rb") as image_file:
                self._client.put_object(
                    Bucket=self._settings.bucket,
                    Key=key,
                    Body=image_file,
                    ContentType="image/png",
                    CacheControl="private, no-store, max-age=0",
                )
            url = self._client.get_presigned_download_url(
                Bucket=self._settings.bucket,
                Key=key,
                Expired=self._settings.url_expires_seconds,
            )
        except Exception as exc:
            raise CosImageUploadError("Tencent COS upload or signed-link creation failed.") from exc

        if not isinstance(url, str):
            raise CosImageUploadError("Tencent COS returned an invalid signed image URL.")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise CosImageUploadError("Tencent COS returned a non-HTTPS signed image URL.")
        return url

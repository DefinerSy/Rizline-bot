from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rizline_cos import CosImageStorageSettings, CosImageUploader


class FakeCosClient:
    def __init__(self) -> None:
        self.put_calls: list[dict] = []
        self.sign_calls: list[dict] = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)

    def get_presigned_download_url(self, **kwargs):
        self.sign_calls.append(kwargs)
        return "https://example.cos.ap-guangzhou.myqcloud.com/card.png?q-sign-time=temporary"


class CosImageStorageTests(unittest.TestCase):
    def test_settings_require_all_credential_fields_when_cos_is_enabled(self) -> None:
        with self.assertRaisesRegex(ValueError, "RIZLINE_COS_REGION"):
            CosImageStorageSettings.from_environment({"RIZLINE_COS_BUCKET": "bucket-123"})

    def test_settings_accept_private_bucket_credentials_and_safe_prefix(self) -> None:
        settings = CosImageStorageSettings.from_environment(
            {
                "RIZLINE_COS_BUCKET": "bucket-123",
                "RIZLINE_COS_REGION": "ap-guangzhou",
                "RIZLINE_COS_SECRET_ID": "id",
                "RIZLINE_COS_SECRET_KEY": "key",
                "RIZLINE_COS_PREFIX": "private/score-cards",
                "RIZLINE_COS_URL_EXPIRES_SECONDS": "300",
            }
        )
        assert settings is not None
        self.assertEqual(settings.prefix, "private/score-cards")
        self.assertEqual(settings.url_expires_seconds, 300)

    def test_upload_uses_png_content_type_private_cache_and_signed_https_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "rizline_b40_alice_1_abc.png"
            image_path.write_bytes(b"png bytes")
            client = FakeCosClient()
            settings = CosImageStorageSettings(
                bucket="bucket-123",
                region="ap-guangzhou",
                secret_id="id",
                secret_key="key",
                prefix="private/score-cards",
                url_expires_seconds=300,
            )

            url = CosImageUploader(settings, client=client).upload_and_get_url(image_path)

        self.assertTrue(url.startswith("https://"))
        self.assertEqual(client.put_calls[0]["Bucket"], "bucket-123")
        self.assertEqual(client.put_calls[0]["Key"], "private/score-cards/rizline_b40_alice_1_abc.png")
        self.assertEqual(client.put_calls[0]["ContentType"], "image/png")
        self.assertEqual(client.put_calls[0]["CacheControl"], "private, no-store, max-age=0")
        self.assertEqual(client.sign_calls[0]["Expired"], 300)


if __name__ == "__main__":
    unittest.main()

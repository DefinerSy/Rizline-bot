from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bot import QQOfficialBot
from rizline_cos import CosImageUploadError


class FakeAPI:
    def __init__(self) -> None:
        self.group_calls: list[dict] = []
        self.c2c_calls: list[dict] = []

    async def post_group_file(self, **kwargs):
        self.group_calls.append(kwargs)
        return {"file_info": "group-media"}

    async def post_c2c_file(self, **kwargs):
        self.c2c_calls.append(kwargs)
        return {"file_info": "c2c-media"}


class FakeMessage:
    def __init__(self, api: FakeAPI) -> None:
        self._api = api
        self.group_openid = "group-openid"
        self.author = SimpleNamespace(user_openid="user-openid")
        self.replies: list[dict] = []

    async def reply(self, **kwargs):
        self.replies.append(kwargs)


class BotStub:
    _public_image_url = QQOfficialBot._public_image_url
    _image_url_for_delivery = QQOfficialBot._image_url_for_delivery

    def __init__(self, output_dir: Path, public_base_url: str, cos_image_uploader=None) -> None:
        self._image_output_dir = output_dir.resolve()
        self._image_public_base_url = public_base_url
        self._cos_image_uploader = cos_image_uploader


class FakeCosUploader:
    def __init__(self, url: str | None = "https://bucket.cos.ap-guangzhou.myqcloud.com/image.png?sign=temporary") -> None:
        self.url = url
        self.paths: list[Path] = []

    def upload_and_get_url(self, image_path: Path) -> str:
        self.paths.append(image_path)
        if self.url is None:
            raise CosImageUploadError("upload failed")
        return self.url


class QQMediaReplyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.output_dir = Path(self._temporary_directory.name) / "exports"
        self.output_dir.mkdir()
        self.image_path = self.output_dir / "b40 score.png"
        self.image_path.write_bytes(b"not used by the mocked media API")

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_group_media_reply_uploads_the_public_url_then_sends_media(self) -> None:
        api = FakeAPI()
        message = FakeMessage(api)
        bot = BotStub(self.output_dir, "https://scores.example.test/rizline/")

        await QQOfficialBot._reply_with_image(bot, message, source="group", image_path=self.image_path)

        self.assertEqual(
            api.group_calls,
            [
                {
                    "group_openid": "group-openid",
                    "file_type": 1,
                    "url": "https://scores.example.test/rizline/b40%20score.png",
                    "srv_send_msg": False,
                }
            ],
        )
        self.assertEqual(message.replies, [{"msg_type": 7, "media": {"file_info": "group-media"}, "msg_seq": 2}])

    async def test_c2c_media_reply_uses_the_user_openid(self) -> None:
        api = FakeAPI()
        message = FakeMessage(api)
        bot = BotStub(self.output_dir, "https://scores.example.test/rizline/")

        await QQOfficialBot._reply_with_image(bot, message, source="c2c", image_path=self.image_path)

        self.assertEqual(api.c2c_calls[0]["openid"], "user-openid")
        self.assertEqual(message.replies[0]["media"], {"file_info": "c2c-media"})

    async def test_missing_public_base_url_returns_a_clear_second_text_reply(self) -> None:
        api = FakeAPI()
        message = FakeMessage(api)
        bot = BotStub(self.output_dir, "")

        await QQOfficialBot._reply_with_image(bot, message, source="group", image_path=self.image_path)

        self.assertEqual(api.group_calls, [])
        self.assertEqual(message.replies[0]["msg_seq"], 2)
        self.assertIn("RIZLINE_IMAGE_PUBLIC_BASE_URL", message.replies[0]["content"])

    async def test_cos_signed_url_is_used_without_a_static_public_base_url(self) -> None:
        api = FakeAPI()
        message = FakeMessage(api)
        uploader = FakeCosUploader()
        bot = BotStub(self.output_dir, "", uploader)

        await QQOfficialBot._reply_with_image(bot, message, source="group", image_path=self.image_path)

        self.assertEqual(uploader.paths, [self.image_path])
        self.assertEqual(api.group_calls[0]["url"], uploader.url)
        self.assertEqual(message.replies[0]["msg_type"], 7)

    async def test_cos_upload_failure_returns_a_clear_second_text_reply(self) -> None:
        api = FakeAPI()
        message = FakeMessage(api)
        bot = BotStub(self.output_dir, "", FakeCosUploader(None))

        await QQOfficialBot._reply_with_image(bot, message, source="group", image_path=self.image_path)

        self.assertEqual(api.group_calls, [])
        self.assertIn("腾讯 COS 上传失败", message.replies[0]["content"])


if __name__ == "__main__":
    unittest.main()

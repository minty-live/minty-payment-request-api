"""What reaches the bucket (the user, 2026-11-26, replacing the owner's 2026-11-04 "exactly as
uploaded"): a photo compressed by ``minty_shared.images.for_storage``, a document as uploaded,
through ``minty_shared.storage.put_file`` (ContentType set, retried twice)."""

import io
import struct
import zlib
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from bills.models import Attachment
from bills.tests.conftest import tiny_image


def _big_png(side=800) -> bytes:
    """A real PNG over 1 MB, stored uncompressed: the old downsizing re-saved it far smaller."""
    raw = b"".join(b"\x00" + bytes([row % 256]) * (side * 3) for row in range(side))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 0)) + chunk(b"IEND", b"")


def _s3(stored, failures=0):
    s3 = MagicMock(**{"generate_presigned_url.return_value": "https://s3.test/signed"})

    def upload(fileobj, bucket, key, ExtraArgs=None):  # noqa: N803 - boto3's name
        nonlocal failures
        if failures:
            failures -= 1
            raise ConnectionError("network down")
        stored.append((fileobj.read(), ExtraArgs))

    s3.upload_fileobj.side_effect = upload
    return s3


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("minty_shared.storage.time.sleep", lambda seconds: None)


@pytest.mark.django_db
class TestStoredAsUploaded:
    @patch("bills.services.attachment_service._get_s3_client")
    def test_a_large_png_photo_is_stored_as_a_smaller_jpeg(
        self, mock_get_s3, api_client, auth_headers, draft_bill, test_user_entity
    ):
        """2026-11-26 (the user): B2 keeps a photo compressed - a JPEG, when that is smaller."""
        stored = []
        mock_get_s3.return_value = _s3(stored)
        png = _big_png()
        assert len(png) > 1024 * 1024

        resp = api_client.post(
            f"/api/v1/bills/{draft_bill.id}/attachments",
            data={"files": SimpleUploadedFile("receipt.png", png, content_type="image/png")},
            **auth_headers,
        )

        assert resp.status_code == 201, resp.content
        [(data, extra)] = stored
        assert extra == {"ContentType": "image/jpeg"} and len(data) < len(png)
        with Image.open(io.BytesIO(data)) as img:
            assert (img.format, img.size) == ("JPEG", (800, 800))
        att = Attachment.objects.get(id=resp.json()[0]["attachment"]["id"])
        assert (att.file_size, att.mime_type, att.file_extension) == (len(data), "image/jpeg", "jpg")
        assert att.file_path.endswith(".jpg") and att.original_name == "receipt.jpg"

    @patch("bills.services.attachment_service._get_s3_client")
    def test_a_png_that_jpeg_would_not_shrink_is_stored_byte_for_byte(
        self, mock_get_s3, api_client, auth_headers, draft_bill, test_user_entity
    ):
        stored = []
        mock_get_s3.return_value = _s3(stored)
        png = tiny_image("PNG")

        resp = api_client.post(
            f"/api/v1/bills/{draft_bill.id}/attachments",
            data={"files": SimpleUploadedFile("screen.png", png, content_type="image/png")},
            **auth_headers,
        )

        assert resp.status_code == 201, resp.content
        assert stored == [(png, {"ContentType": "image/png"})]
        assert resp.json()[0]["attachment"]["original_name"] == "screen.png"

    @patch("bills.services.attachment_service._get_s3_client")
    def test_a_photo_that_will_not_open_is_refused(
        self, mock_get_s3, api_client, auth_headers, draft_bill, test_user_entity
    ):
        stored = []
        mock_get_s3.return_value = _s3(stored)

        resp = api_client.post(
            f"/api/v1/bills/{draft_bill.id}/attachments",
            data={"files": SimpleUploadedFile("a.jpg", b"not a photo", content_type="image/jpeg")},
            **auth_headers,
        )

        assert resp.status_code == 422
        assert "This photo can't be opened. Attach it again as a JPEG or PNG." in resp.content.decode()
        assert stored == []

    @patch("bills.services.attachment_service._get_s3_client")
    def test_a_dropped_upload_is_retried(
        self, mock_get_s3, api_client, auth_headers, draft_bill, test_user_entity
    ):
        stored = []
        mock_get_s3.return_value = _s3(stored, failures=2)

        resp = api_client.post(
            f"/api/v1/bills/{draft_bill.id}/attachments",
            data={"files": SimpleUploadedFile("invoice.pdf", b"%PDF-1.4 x", content_type="application/pdf")},
            **auth_headers,
        )

        assert resp.status_code == 201, resp.content
        assert stored == [(b"%PDF-1.4 x", {"ContentType": "application/pdf"})]

    @patch("bills.services.attachment_service._get_s3_client")
    def test_an_upload_that_keeps_failing_is_refused_and_stores_no_row(
        self, mock_get_s3, api_client, auth_headers, draft_bill, test_user_entity
    ):
        stored = []
        mock_get_s3.return_value = _s3(stored, failures=3)
        before = Attachment.objects.count()

        resp = api_client.post(
            f"/api/v1/bills/{draft_bill.id}/attachments",
            data={"files": SimpleUploadedFile("invoice.pdf", b"%PDF-1.4 x", content_type="application/pdf")},
            **auth_headers,
        )

        assert resp.status_code == 422, resp.content
        assert "I couldn't upload that file. Mind trying again?" in resp.content.decode()
        assert stored == [] and Attachment.objects.count() == before

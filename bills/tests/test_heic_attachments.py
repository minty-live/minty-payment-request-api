"""iPhone HEIC attachments: stored as an upright JPEG since 2026-11-26 (the user; B2 keeps photos
compressed). A HEIC stored 2026-11-24..26 is still HEIC in B2, so a JPEG is made on the fly where
HEIC can't be used - ``/preview/?as=jpeg`` (a browser that can't draw it) and the Xero publish
(Xero takes no HEIC)."""

import datetime
import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from bills.models import Attachment, BillAttachment, Payment, PaymentAttachment
from bills.tests import test_xero_publish_bankslip as bankslip_tests

# the bank-slip publish's fixtures: a published bill on a connected company, with one slip
entity = bankslip_tests.entity
user = bankslip_tests.user
user_entity = bankslip_tests.user_entity
published_bill = bankslip_tests.published_bill
payment_with_bankslip = bankslip_tests.payment_with_bankslip

#: 80x40 stored with EXIF orientation 6: upright 40x80 (minty-shared-py's fixture).
HEIC = (Path(__file__).parent / "fixtures" / "tiny.heic").read_bytes()
NOT_A_PHOTO = b"\x00\x00\x00\x18ftypheic but nothing a decoder can read"
WONT_OPEN = "This photo can't be opened. Attach it again as a JPEG or PNG."


def _image(data: bytes) -> tuple[str, tuple[int, int]]:
    with Image.open(io.BytesIO(data)) as img:
        return img.format, img.size


class _Bucket:
    """The S3 client: what was put, and what a get returns."""

    def __init__(self, stored: bytes = b""):
        self.put: dict[str, tuple[bytes, dict]] = {}
        self.stored = stored

    def upload_fileobj(self, fileobj, bucket, key, ExtraArgs=None):  # noqa: N803 - boto3's name
        self.put[key] = (fileobj.read(), ExtraArgs or {})

    def generate_presigned_url(self, *args, **kwargs):
        return "https://b2.example/presigned"

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3's names
        return {"Body": io.BytesIO(self.stored), "ContentLength": len(self.stored),
                "ContentType": "application/octet-stream"}


# ─── upload ────────────────────────────────────────────────────────────────


def _upload(api_client, auth_headers, bill, name, data, content_type):
    bucket = _Bucket()
    with patch("bills.services.attachment_service._get_s3_client", return_value=bucket):
        resp = api_client.post(
            f"/api/v1/bills/{bill.id}/attachments",
            data={"files": SimpleUploadedFile(name, data, content_type=content_type)},
            **auth_headers,
        )
    return resp, bucket


@pytest.mark.django_db
@pytest.mark.parametrize("content_type", ["image/heic", "application/octet-stream", ""])
def test_a_heic_is_stored_as_an_upright_jpeg(api_client, auth_headers, draft_bill, test_user_entity,
                                             content_type):
    """2026-11-26 (the user): B2 keeps photos compressed, in a type Xero takes."""
    resp, bucket = _upload(api_client, auth_headers, draft_bill, "IMG_1234.HEIC", HEIC, content_type)

    assert resp.status_code == 201, resp.content
    [(key, (data, extra))] = bucket.put.items()  # one object: no copy
    assert _image(data) == ("JPEG", (40, 80)) and key.endswith(".jpg")
    assert extra == {"ContentType": "image/jpeg"}
    attachment = resp.json()[0]["attachment"]
    assert (attachment["mime_type"], attachment["original_name"]) == ("image/jpeg", "IMG_1234.jpg")


@pytest.mark.django_db
def test_a_heic_named_like_a_jpeg_is_known_by_its_bytes(api_client, auth_headers, draft_bill, test_user_entity):
    resp, bucket = _upload(api_client, auth_headers, draft_bill, "photo.jpg", HEIC, "image/jpeg")

    assert resp.status_code == 201, resp.content
    [(data, extra)] = bucket.put.values()
    assert _image(data) == ("JPEG", (40, 80)) and extra == {"ContentType": "image/jpeg"}
    assert resp.json()[0]["attachment"]["mime_type"] == "image/jpeg"


@pytest.mark.django_db
def test_a_heic_that_will_not_open_is_refused_and_nothing_is_stored(
    api_client, auth_headers, draft_bill, test_user_entity,
):
    resp, bucket = _upload(api_client, auth_headers, draft_bill, "IMG_1.HEIC", NOT_A_PHOTO, "image/heic")

    assert resp.status_code == 422
    assert resp.json()["detail"] == WONT_OPEN
    assert bucket.put == {}
    assert not BillAttachment.objects.filter(bill=draft_bill).exists()


# ─── preview ───────────────────────────────────────────────────────────────


def _bill_attachment(bill, user, *, name="IMG_1234.HEIC", mime="image/heic") -> BillAttachment:
    att = Attachment.objects.create(
        original_name=name, stored_name="stored", file_path=f"attachments/{bill.id}/stored",
        mime_type=mime, file_size=len(HEIC), file_extension=name.rsplit(".", 1)[-1],
        storage_provider="s3", uploaded_by=user.id,
    )
    return BillAttachment.objects.create(bill=bill, attachment=att, attachment_role="invoice",
                                         created_by=user.id)


def _preview(api_client, auth_headers, path, stored, **query):
    with patch("bills.api._get_s3_client", return_value=_Bucket(stored)), \
            patch("bills.api_payments._get_s3_client", return_value=_Bucket(stored)):
        return api_client.get(path, query, **auth_headers)


def _body(resp) -> bytes:
    return b"".join(resp.streaming_content) if resp.streaming else resp.content


@pytest.mark.django_db
def test_the_preview_streams_a_heic_as_it_is_for_a_browser_that_draws_it(
    api_client, auth_headers, draft_bill, test_user, test_user_entity,
):
    ba = _bill_attachment(draft_bill, test_user)

    resp = _preview(api_client, auth_headers, f"/api/v1/bills/{draft_bill.id}/attachments/{ba.id}/preview/", HEIC)

    assert resp.status_code == 200
    assert resp["Content-Type"] == "image/heic" and _body(resp) == HEIC


@pytest.mark.django_db
def test_as_jpeg_makes_an_upright_jpeg_on_the_fly(api_client, auth_headers, draft_bill, test_user, test_user_entity):
    ba = _bill_attachment(draft_bill, test_user)

    resp = _preview(api_client, auth_headers, f"/api/v1/bills/{draft_bill.id}/attachments/{ba.id}/preview/", HEIC,
                    **{"as": "jpeg"})

    assert resp.status_code == 200
    assert resp["Content-Type"] == "image/jpeg"
    assert resp["Content-Disposition"] == 'inline; filename="IMG_1234.jpg"'
    assert resp["X-Frame-Options"] == "SAMEORIGIN"
    assert _image(_body(resp)) == ("JPEG", (40, 80))


@pytest.mark.django_db
def test_as_jpeg_leaves_any_other_file_exactly_as_it_is(api_client, auth_headers, draft_bill, test_user,
                                                        test_user_entity):
    ba = _bill_attachment(draft_bill, test_user, name="invoice.pdf", mime="application/pdf")

    resp = _preview(api_client, auth_headers, f"/api/v1/bills/{draft_bill.id}/attachments/{ba.id}/preview/",
                    b"%PDF-1.7 an invoice", **{"as": "jpeg"})

    assert resp.status_code == 200
    assert resp["Content-Type"] == "application/pdf" and _body(resp) == b"%PDF-1.7 an invoice"


@pytest.mark.django_db
def test_a_stored_heic_that_no_longer_opens_is_a_422(api_client, auth_headers, draft_bill, test_user,
                                                     test_user_entity):
    ba = _bill_attachment(draft_bill, test_user)

    resp = _preview(api_client, auth_headers, f"/api/v1/bills/{draft_bill.id}/attachments/{ba.id}/preview/",
                    NOT_A_PHOTO, **{"as": "jpeg"})

    assert resp.status_code == 422
    assert resp.json()["detail"] == "This photo can't be shown here."


@pytest.mark.django_db
def test_a_payment_slip_heic_previews_as_jpeg_too(api_client, auth_headers, draft_bill, test_user, test_user_entity):
    payment = Payment.objects.create(bill=draft_bill, payment_date="2026-03-20", amount="100.00",
                                     currency_code="HKD", payment_status="pending", created_by=test_user.id)
    att = Attachment.objects.create(original_name="slip.heic", stored_name="s", file_path="p/s",
                                    mime_type="image/heic", file_size=len(HEIC), file_extension="heic",
                                    storage_provider="s3", uploaded_by=test_user.id)
    pa = PaymentAttachment.objects.create(payment=payment, attachment=att, sort_order=0, created_by=test_user.id,
                                          attachment_role=PaymentAttachment.AttachmentRole.BANK_SLIP)
    path = f"/api/v1/bills/{draft_bill.id}/payments/{payment.id}/attachments/{pa.id}/preview/"

    plain = _preview(api_client, auth_headers, path, HEIC)
    jpeg = _preview(api_client, auth_headers, path, HEIC, **{"as": "jpeg"})

    assert plain.status_code == 200 and _body(plain) == HEIC
    assert jpeg.status_code == 200 and jpeg["Content-Type"] == "image/jpeg"
    assert _image(_body(jpeg)) == ("JPEG", (40, 80))


# ─── Xero publish ──────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_a_heic_bill_attachment_goes_to_xero_as_a_jpeg(test_user, draft_bill):
    from bills.models import XeroBillSync
    from bills.services.xero_publish_service import _upload_bill_attachments_to_xero

    ba = _bill_attachment(draft_bill, test_user)
    sync = XeroBillSync(
        bill=draft_bill, sync_direction=XeroBillSync.SyncDirection.OUTBOUND,
        sync_type=XeroBillSync.SyncType.CREATE_INVOICE, sync_status=XeroBillSync.SyncStatus.SUCCESS,
        request_type="ACCPAY", request_status="AUTHORISED", request_contact_id="", request_invoice_number="",
        request_reference="", response_invoice_id="xero-inv-001", response_invoice_number="INV-001",
        idempotency_key="k", requested_by=test_user.id,
        requested_at=datetime.datetime(2026, 4, 1, tzinfo=datetime.UTC),
    )
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=HEIC))}

    with (
        patch("bills.services.xero_publish_service.resolve_xero_access_token_for_entity", return_value="tok"),
        patch("bills.services.xero_publish_service._get_s3_client", return_value=s3),
        patch("bills.services.xero_publish_service._get_xero_files_for_invoice", return_value=[]),
        patch("bills.services.xero_publish_service._upload_and_associate_bill_attachment",
              return_value="xero-file-1") as upload,
        patch("bills.services.xero_publish_service._delete_xero_file"),
    ):
        _upload_bill_attachments_to_xero(draft_bill, sync, draft_bill.entity_id, test_user.id, "org-1")

    _token, _org, _invoice, name, data, content_type = upload.call_args[0]
    assert (name, content_type) == ("INV-001_PAYMENTREQUEST.jpg", "image/jpeg")
    assert _image(data) == ("JPEG", (40, 80))
    ba.refresh_from_db()
    assert ba.xero_filename == "INV-001_PAYMENTREQUEST.jpg"
    # the stored file is untouched
    assert Attachment.objects.get(id=ba.attachment_id).mime_type == "image/heic"


@pytest.mark.django_db
def test_a_heic_bank_slip_goes_to_xero_as_a_jpeg(payment_with_bankslip, entity, user_entity):
    from bills.services.xero_publish_service import upload_bankslip_to_xero

    payment, pa = payment_with_bankslip
    Attachment.objects.filter(id=pa.attachment_id).update(
        original_name="slip.HEIC", stored_name="s.HEIC", file_path="p/s.HEIC", mime_type="image/heic",
        file_size=len(HEIC), file_extension="HEIC")

    with (
        patch("bills.services.xero_publish_service._get_xero_files_for_invoice", return_value=[]),
        patch("bills.services.xero_publish_service._delete_xero_file"),
        patch("bills.services.xero_publish_service._upload_and_associate_bill_attachment",
              return_value="xero-file-bs") as upload,
        patch("bills.services.xero_publish_service._get_s3_client", return_value=MagicMock()),
        patch("bills.services.xero_publish_service._download_from_s3", return_value=HEIC),
    ):
        result = upload_bankslip_to_xero(bill_id=str(payment.bill_id), payment_id=str(payment.id),
                                         entity_id=entity.id, user_id=bankslip_tests.FAKE_USER_ID,
                                         access_token="tok")

    assert result["uploaded"] == 1
    _token, _org, _invoice, name, data, content_type = upload.call_args[0]
    assert (name, content_type) == (f"{bankslip_tests.FAKE_INVOICE_NUMBER}_BANKSLIP.jpg", "image/jpeg")
    assert _image(data) == ("JPEG", (40, 80))

import base64

import pytest

from proton_mail_mcp.mail_models import decode_attachments


def test_decode_attachments_enforces_count_and_total_size():
    item = {
        "filename": "file.bin",
        "content_type": "application/octet-stream",
        "content_base64": base64.b64encode(b"1234").decode(),
    }

    with pytest.raises(ValueError, match="count exceeds"):
        decode_attachments([item, item], max_count=1, max_total_bytes=100)
    with pytest.raises(ValueError, match="size exceeds"):
        decode_attachments([item], max_count=1, max_total_bytes=3)


def test_decode_attachments_rejects_paths_and_invalid_base64():
    with pytest.raises(ValueError, match="plain filename"):
        decode_attachments(
            [{"filename": "../secret", "content_type": "text/plain", "content_base64": "YQ=="}],
            max_count=1,
            max_total_bytes=10,
        )
    with pytest.raises(ValueError, match="invalid Base64"):
        decode_attachments(
            [{"filename": "file.txt", "content_type": "text/plain", "content_base64": "not-base64"}],
            max_count=1,
            max_total_bytes=10,
        )

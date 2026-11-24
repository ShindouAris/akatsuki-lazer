from app.api.v2.profile_assets import sniff_image_type


def test_image_types_are_recognised_by_their_first_bytes() -> None:
    assert sniff_image_type(b"\x89PNG\r\n\x1a\n" + b"x" * 8) == "image/png"
    assert sniff_image_type(b"\xff\xd8\xff\xe0" + b"x" * 8) == "image/jpeg"
    assert sniff_image_type(b"GIF89a" + b"x" * 8) == "image/gif"
    assert sniff_image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"


def test_other_files_are_not_images() -> None:
    assert sniff_image_type(b"<html><script>alert(1)</script>") is None
    assert sniff_image_type(b"") is None
    assert sniff_image_type(b"RIFF\x00\x00\x00\x00WAVEfmt ") is None

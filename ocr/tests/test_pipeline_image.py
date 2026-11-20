"""OCR 전처리(#110): 긴 변 축소와 EXIF 회전. 테스트 이미지는 코드로 만든다(실제 등록증 없음).

opencv·pillow가 있는 환경(OCR 이미지)에서 돈다. Worker 개발 환경(paddle 없음)에서는 건너뛴다.
"""

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
Image = pytest.importorskip("PIL.Image")

from pipeline.image import read_image, resize_to_max_side, size_text  # noqa: E402

pytestmark = pytest.mark.unit


def _image(width, height):
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, : width // 4] = (0, 0, 255)  # 왼쪽 1/4 빨강(BGR) - 회전 확인용
    return image


@pytest.mark.parametrize(
    ("size", "max_side", "expected"),
    [
        ((4032, 3024), 1600, "1600x1200"),  # 가로 사진
        ((3024, 4032), 1600, "1200x1600"),  # 세로 사진
        ((4032, 3024), 1280, "1280x960"),
        ((4000, 1001), 2000, "2000x500"),  # 비율 유지(500.5 → 반올림)
    ],
)
def test_large_image_is_shrunk_keeping_the_ratio(size, max_side, expected):
    resized = resize_to_max_side(_image(*size), max_side)
    assert size_text(resized) == expected


@pytest.mark.parametrize(
    ("size", "max_side"),
    [
        ((1500, 950), 1600),  # 이미 작다
        ((1600, 1200), 1600),  # 딱 맞다
        ((800, 600), 2000),  # 확대하지 않는다
        ((4032, 3024), 0),  # 0이면 줄이지 않는다
    ],
)
def test_small_image_or_zero_is_left_as_is(size, max_side):
    image = _image(*size)
    assert resize_to_max_side(image, max_side) is image


def test_exif_rotation_is_applied_before_resize(tmp_path):
    # 300x100으로 저장하고 EXIF Orientation=6(시계 방향 90도로 보여야 함)을 붙인다.
    stored = Image.new("RGB", (300, 100), "white")
    stored.paste((255, 0, 0), (0, 0, 60, 100))  # 왼쪽 빨간 띠
    exif = Image.Exif()
    exif[0x0112] = 6
    path = tmp_path / "rotated.jpg"
    stored.save(path, "JPEG", exif=exif.tobytes())

    image = read_image(str(path))
    assert size_text(image) == "100x300"  # 회전이 먼저 적용됐다
    # 빨간 띠는 회전 뒤 위쪽에 온다(BGR이라 빨강은 마지막 채널).
    top, bottom = image[:30].mean(axis=(0, 1)), image[-30:].mean(axis=(0, 1))
    assert top[2] > 200 and top[0] < 80 and bottom[0] > 200

    resized = resize_to_max_side(image, 150)
    assert size_text(resized) == "50x150"  # 회전한 모양 그대로 줄인다


def test_unreadable_file_raises(tmp_path):
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"not an image")
    with pytest.raises(ValueError):
        read_image(str(path))

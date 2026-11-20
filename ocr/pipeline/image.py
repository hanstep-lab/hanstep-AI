"""OCR 전처리(#110): 이미지 읽기(EXIF 회전) → 긴 변 축소.

PaddleOCR 검출 크기 설정(limit_type=min, limit_side_len=64, max_side_limit=4000)은 그대로 둔다.
짧은 변이 64보다 작을 때만 키우고 긴 변은 4000까지 두므로, 여기서 줄인 크기를 다시 바꾸지 않는다
(검출 입력은 32의 배수로만 맞춘다).
"""

from __future__ import annotations

import cv2
import numpy as np


def read_image(path: str) -> np.ndarray:
    """이미지 파일 → BGR 배열. OpenCV가 EXIF 회전(Orientation)을 적용한다(IMREAD_COLOR).

    #110 전에는 PaddleOCR이 같은 방법(cv2.imdecode, IMREAD_COLOR)으로 파일을 읽었다.
    """
    data = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("image could not be decoded")
    return image


def resize_to_max_side(image: np.ndarray, max_side: int) -> np.ndarray:
    """긴 변이 max_side를 넘을 때만 비율을 지켜 줄인다(INTER_AREA). 0이면 그대로, 확대는 없다."""
    height, width = image.shape[:2]
    longest = max(height, width)
    if not max_side or longest <= max_side:
        return image
    scale = max_side / longest
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return cv2.resize(image, size, interpolation=cv2.INTER_AREA)


def size_text(image: np.ndarray) -> str:
    """로그용 "가로x세로"."""
    height, width = image.shape[:2]
    return f"{width}x{height}"

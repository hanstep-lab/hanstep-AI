"""외국인등록증 OCR Worker(``python -m worker``). 엔진(``pipeline``)과 같은 이미지에서 돈다.

BE가 커밋 후 발행한 ``ocr.request`` 메시지를 받아 R2 원본을 읽고 엔진으로 앞면 6개 항목을
뽑아 Spring 내부 API로 보고한다. 큐 처리 규칙은 ``ai/app/worker``와 같다(Python 3.10 이미지라
import하지 않고 옮겨 썼다).
"""

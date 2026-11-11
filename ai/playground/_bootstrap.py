"""`streamlit run ai/playground/<파일>.py`로 실행할 때 `app` 패키지를 import할 수 있게 한다."""

import sys
from pathlib import Path

AI_ROOT = Path(__file__).resolve().parents[1]
if str(AI_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_ROOT))

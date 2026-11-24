import sys
from pathlib import Path

# 允许从仓库根目录或 backend/ 目录运行 pytest 时都能导入 src.*
BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

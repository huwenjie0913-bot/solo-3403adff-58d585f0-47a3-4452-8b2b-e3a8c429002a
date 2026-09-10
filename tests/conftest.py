import os
import sys
import tempfile
from pathlib import Path

# 保证可以 import app 包，并使用独立的临时数据库
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["SUBTITLE_QC_DB"] = f"sqlite:///{tempfile.mkdtemp(prefix='subtitle_qc_')}/test.db"

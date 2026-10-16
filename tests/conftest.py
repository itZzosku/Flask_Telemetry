import os
import sys
import tempfile

# The diary reads DIARY_DATA_DIR at import time: point it at a throwaway folder first.
os.environ["DIARY_DATA_DIR"] = tempfile.mkdtemp(prefix="diary-test-")
os.environ["DIARY_COOKIE_SECURE"] = "0"
os.environ["DIARY_BEHIND_PROXY"] = "0"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

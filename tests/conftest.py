import os

# Keep tests isolated from any local developer database.
os.environ.setdefault("QCLOUD_DB_PATH", ":memory:")

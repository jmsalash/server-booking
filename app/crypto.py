import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet


def load_or_create_key(path: Path) -> bytes:
    """Return the key at `path`, creating it if missing. The first writer wins a race.

    The key is written and fsynced to a private temp file in the same directory, then
    hard-linked into place: the link fails if another process got there first, and a
    racing reader never sees a partially written (empty) key file.
    """
    path = Path(path)
    if path.exists():
        return path.read_bytes().strip()
    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")  # 0600
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(key)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:  # lost a race with another process
            return path.read_bytes().strip()
    finally:
        os.unlink(tmp)
    return key


class SecretBox:
    def __init__(self, key: bytes):
        self._fernet = Fernet(key)

    def encrypt(self, s: str) -> str:
        return self._fernet.encrypt(s.encode()).decode()

    def decrypt(self, s: str) -> str:
        return self._fernet.decrypt(s.encode()).decode()

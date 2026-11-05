import os
from pathlib import Path

from cryptography.fernet import Fernet


def load_or_create_key(path: Path) -> bytes:
    path = Path(path)
    if path.exists():
        return path.read_bytes().strip()
    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # lost a race with another process
        return path.read_bytes().strip()
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


class SecretBox:
    def __init__(self, key: bytes):
        self._fernet = Fernet(key)

    def encrypt(self, s: str) -> str:
        return self._fernet.encrypt(s.encode()).decode()

    def decrypt(self, s: str) -> str:
        return self._fernet.decrypt(s.encode()).decode()

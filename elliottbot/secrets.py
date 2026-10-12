import json
import os
from pathlib import Path
import re
import stat
import tempfile


class SecretStore:
    """Owner-only local secret configuration. Values never enter journal/API output."""

    def __init__(self, root):
        self.root = Path(root) / "secrets"

    def _path(self, mode):
        if mode not in {"testnet", "live"}:
            raise ValueError("데모 또는 실거래 키만 저장할 수 있습니다.")
        return self.root / f"{mode}.json"

    def load(self, mode):
        path = self._path(mode)
        if not path.exists():
            return "", ""
        if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError("비밀 파일 권한은 600이어야 합니다.")
        data = json.loads(path.read_text())
        return data["key"], data["secret"]

    def save(self, mode, key, secret):
        if not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9]{16,256}", v) for v in (key, secret)):
            raise ValueError("Binance System generated HMAC 키와 Secret을 입력하세요.")
        path = self._path(mode)
        if self.root.is_symlink():
            raise ValueError("비밀 디렉터리에 심볼릭 링크를 사용할 수 없습니다.")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        fd, name = tempfile.mkstemp(dir=self.root)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump({"key": key, "secret": secret}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

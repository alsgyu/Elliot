from pathlib import Path
import fcntl


class ProcessLock:
    """Advisory ownership for macOS/Linux; the OS releases it on process death."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("a+")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            raise RuntimeError("another engine already owns this database") from exc

    def close(self):
        if not self.handle.closed:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()

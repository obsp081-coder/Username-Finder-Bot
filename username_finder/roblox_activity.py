"""Cross-process Roblox exclusion, shared by interactive and background workers."""

import os
from pathlib import Path


class RobloxActivity:
    def __init__(self, database):
        database = Path(database).resolve()
        self.path = database.with_name(database.name + ".roblox-activity.lock")
        self.block = self.path.with_suffix(".blocked")
        self.pending = self.path.with_suffix(".bulk-state")
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if not self.path.stat().st_size:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise ValueError("توجد عملية Roblox أخرى نشطة؛ لم يُرسل طلب.") from exc
        self.stream = stream
        try:
            pending = self.pending.read_bytes() if self.pending.exists() else b""
            if self.block.exists() or (pending and not pending.endswith(b"Clear\n")):
                raise ValueError("يوجد توقف Bulk وقائي محفوظ؛ يلزم تدقيق الدليل قبل أي تشغيل Roblox جديد.")
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def mark_pending(self):
        self._state(b"Pending\n")

    def clear_pending(self):
        self._state(b"Clear\n")

    def _state(self, value):
        with self.pending.open("ab") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())

    def restrict(self, reason):
        if not self.block.exists():
            with self.block.open("xb") as stream:
                stream.write(reason.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())

    def __exit__(self, *_):
        if self.stream:
            if os.name == "nt":
                import msvcrt

                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            self.stream.close()
            self.stream = None

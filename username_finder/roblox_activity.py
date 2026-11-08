"""Cross-process Roblox exclusion, shared by interactive and background workers."""

import os
from pathlib import Path


class RobloxActivity:
    def __init__(self, database, *, recover_pending=None):
        database = Path(database).resolve()
        self.path = database.with_name(database.name + ".roblox-activity.lock")
        self.block = self.path.with_suffix(".blocked")
        self.pending = self.path.with_suffix(".bulk-state")
        self.stream = None
        self.recover_pending = recover_pending

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
            pending = b""
            if self.pending.exists():
                with self.pending.open("rb") as stream:
                    stream.seek(0, os.SEEK_END)
                    stream.seek(max(0, stream.tell() - 1024))
                    pending = stream.read(1024)
            if self.block.exists():
                raise ValueError("يوجد توقف Bulk وقائي محفوظ؛ يلزم تدقيق الدليل قبل أي تشغيل Roblox جديد.")
            if pending and not pending.endswith(b"Clear\n"):
                last = pending.splitlines()[-1]
                if not self.recover_pending or not self.recover_pending(last):
                    raise ValueError("طلب Roblox غير محسوم؛ راجع دليله قبل أي إرسال جديد.")
                self.clear_pending()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def mark_pending(self, owner=""):
        if owner and (not owner.isascii() or not owner.replace(":", "").isalnum()):
            raise ValueError("Invalid activity owner")
        self._state(("Pending:" + owner + "\n").encode("ascii") if owner else b"Pending\n")

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

"""Cross-process Roblox exclusion, shared by interactive and background workers."""

import json
import os
from pathlib import Path


class RobloxActivity:
    def __init__(self, database, *, recover_pending=None, recover_cooldown=None, preserve_pending=False):
        database = Path(database).resolve()
        self.path = database.with_name(database.name + ".roblox-activity.lock")
        self.block = self.path.with_suffix(".blocked")
        self.pending = self.path.with_suffix(".bulk-state")
        self.cooldown = self.path.with_suffix(".cooldown")
        self.stream = None
        self.recover_pending = recover_pending
        self.recover_cooldown = recover_cooldown
        self.preserve_pending = preserve_pending

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
            cooldown = self.cooldown_state()
            if cooldown and cooldown["state"] == "Hold":
                if not self.recover_cooldown or not self.recover_cooldown(cooldown):
                    raise ValueError("مهلة Roblox محفوظة؛ يلزم مراجعة العملية المالكة وتفويض استمرار منفصل.")
            if pending and not pending.endswith(b"Clear\n"):
                last = pending.splitlines()[-1]
                if not self.recover_pending or not self.recover_pending(last):
                    raise ValueError("طلب Roblox غير محسوم؛ راجع دليله قبل أي إرسال جديد.")
                if not self.preserve_pending:
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

    def cooldown_state(self):
        if not self.cooldown.exists():
            return None
        with self.cooldown.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 8192))
            tail = stream.read(8192)
        try:
            if not tail.endswith(b"\n"):
                raise ValueError("Incomplete cooldown checkpoint")
            value = json.loads(tail.splitlines()[-1])
            if (
                not isinstance(value, dict)
                or set(value) != {"version", "owner", "restriction", "state"}
                or type(value["version"]) is not int
                or value["version"] != 1
                or not isinstance(value["owner"], str)
                or len(value["owner"]) != 129
                or value["owner"][64] != ":"
                or any(c not in "0123456789abcdef" for c in value["owner"].replace(":", ""))
                or not isinstance(value["restriction"], str)
                or len(value["restriction"]) != 64
                or any(c not in "0123456789abcdef" for c in value["restriction"])
            ):
                raise ValueError("Unsupported cooldown checkpoint")
            if value["state"] not in ("Hold", "Released"):
                raise ValueError("Invalid cooldown state")
            return value
        except (ValueError, IndexError, UnicodeError) as exc:
            raise ValueError("دليل مهلة Roblox غير سليم؛ لا إرسال قبل تدقيقه.") from exc

    def checkpoint_cooldown(self, owner, restriction, state):
        """Append under the shared lock; never touch a historical .blocked marker."""
        if not self.stream or self.block.exists():
            raise ValueError("Shared activity lock without a safety block is required")
        previous = self.cooldown_state()
        value = {"version": 1, "owner": owner, "restriction": restriction, "state": state}
        if previous == value:
            return
        if state == "Released" and previous != dict(value, state="Hold"):
            raise ValueError("Only the matching reviewed cooldown can be consumed")
        if state == "Hold" and previous and previous["state"] == "Hold":
            raise ValueError("Another cooldown is already held")
        if state not in ("Hold", "Released"):
            raise ValueError("Unsupported cooldown transition")
        with self.cooldown.open("ab") as stream:
            stream.write((json.dumps(value, sort_keys=True) + "\n").encode("utf-8"))
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

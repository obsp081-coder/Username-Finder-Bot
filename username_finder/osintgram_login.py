"""Private stdio login helper, run only after the user's explicit GUI action.

Executed by the external venv. Uses its installed instagrapi, without copying
Osintgram code. No exception strings or library output cross the protocol.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path


def authenticate(client, username, password, session, ask, two_factor):
    """A failed login never writes the existing session. Tests inject a fake client."""
    if session.is_file():
        try:
            client.load_settings(session)
        except Exception:
            # Keep the original file; only an explicit successful login may replace it.
            pass
    client.challenge_code_handler = lambda *_: ask("challenge")

    def login(relogin=False):
        try:
            return client.login(username, password, relogin=relogin)
        except two_factor:
            return client.login(username, password, verification_code=ask("two_factor"))

    result = login()
    if not result:
        raise ValueError("login failed")
    if client.account_info().username.casefold() != username.casefold():
        result = login(relogin=True)
        if not result or client.account_info().username.casefold() != username.casefold():
            raise ValueError("session account mismatch")
    settings = client.get_settings()
    if not client.user_id:
        raise ValueError("no authenticated user")
    # Stage inside the original directory; atomic replacement preserves a working file on failure.
    import tempfile

    session.parent.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=session.parent,
            prefix=".ufb-session-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            staged = Path(stream.name)
            json.dump(settings, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, session)
        staged = None
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def main():
    output = sys.stdout

    def emit(kind):
        output.write(json.dumps({"type": kind}) + "\n")
        output.flush()

    prompts = [0]

    def ask(kind):
        prompts[0] += 1
        if prompts[0] > 3:
            raise ValueError("prompt limit")
        emit(kind)
        response = json.loads(sys.stdin.readline(1024))
        code = response.get("code", "")
        if not isinstance(code, str) or not code.strip() or len(code) > 128:
            raise ValueError("cancelled")
        return code.strip()

    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            from instagrapi import Client
            from instagrapi.exceptions import TwoFactorRequired

            authenticate(
                Client(),
                os.environ.pop("INSTAGRAM_USERNAME"),
                os.environ.pop("INSTAGRAM_PASSWORD"),
                Path("config/instagrapi_session.json"),
                ask,
                TwoFactorRequired,
            )
        except Exception as exc:
            name = type(exc).__name__.lower()
            emit(
                "challenge_required"
                if any(word in name for word in ("challenge", "captcha", "checkpoint"))
                else "authentication_failed"
            )
            return 1
    emit("authenticated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

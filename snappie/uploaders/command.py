"""Upload by running a command: rclone, scp, s3cmd, curl, or your own script.

The command runs without a shell, so file names with spaces or quotes are safe as
`{file}`.  The same values are in environment variables, for `sh -c '…'` and scripts:
quoting "$SNAPPIE_FILE" there is the safe way.  The link is the URL template, or else
the last URL the command printed.
"""

import os
import random
import re
import shlex
import signal
import string
import subprocess

from . import Cancelled, Field, Plugin, UploadError

URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.I)
PLACEHOLDERS = ("file", "filename", "name", "ext", "random")


def values(path):
    """{placeholder: value} for one upload ({random} is the same in the command and the link)."""
    filename = os.path.basename(path)
    name, ext = os.path.splitext(filename)
    rnd = "".join(random.choices(string.ascii_letters + string.digits, k=8))
    return {"file": path, "filename": filename, "name": name, "ext": ext.lstrip("."), "random": rnd}


def expand(text, vals):
    return re.sub(r"\{(" + "|".join(PLACEHOLDERS) + r")\}", lambda m: vals[m.group(1)], text)


def find_url(output):
    """The last URL (https://, sftp://, …) in the output, or ""."""
    urls = URL.findall(output)
    return urls[-1].rstrip(".,;)\"'") if urls else ""


def _excerpt(text, n=300):
    text = " ".join(text.split())
    return text[-n:] if len(text) > n else text     # the end of the output says what went wrong


class Command(Plugin):
    id = "command"
    name = "Command"
    description = ("Runs a program to upload the file: rclone, scp, s3cmd, curl or your own script. "
                   "Placeholders: {file} (full path), {filename}, {name}, {ext}, {random}.")
    icon = "utilities-terminal"
    fields = [
        Field("command", "Command", required=True,
              placeholder="rclone copyto {file} remote:shots/{random}.{ext}",
              help="Runs without a shell. For pipes use sh -c '…' with \"$SNAPPIE_FILE\", "
                   "$SNAPPIE_FILENAME, $SNAPPIE_RANDOM."),
        Field("url", "Link", placeholder="https://cdn.example.com/shots/{random}.{ext}",
              help="Empty: the last URL the command prints."),
        Field("timeout", "Time limit", "int", default=0, maximum=86400, special="None",
              help="Stop the command after this many seconds."),
    ]

    def validate(self, config):
        super().validate(config)
        try:
            argv = shlex.split(config.get("command") or "")
        except ValueError as e:
            raise UploadError(f"The command can't be split into arguments: {e}") from None
        if not argv:
            raise UploadError("Command is required")

    def summary(self, config):
        return config.get("command") or ""

    def upload(self, path, config, progress, cancel):
        vals = values(path)
        try:
            argv = [expand(a, vals) for a in shlex.split(config.get("command") or "")]
        except ValueError as e:
            raise UploadError(f"The command can't be split into arguments: {e}") from None
        if not argv:
            raise UploadError("No command set")
        argv[0] = os.path.expanduser(argv[0])
        env = dict(os.environ, **{f"SNAPPIE_{k.upper()}": v for k, v in vals.items()})
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env, text=True, errors="replace",
                                    start_new_session=True)     # its own group: cancel stops it all
        except OSError as e:
            raise UploadError(f"Can't run {argv[0]}: {e.strerror or e}") from None

        size = os.path.getsize(path) if os.path.exists(path) else 0
        limit = int(config.get("timeout") or 0)
        waited = 0.0
        while True:
            try:
                out, err = proc.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                waited += 0.2
                if cancel.is_set() or (limit and waited >= limit):
                    _stop(proc)
                    if cancel.is_set():
                        raise Cancelled() from None
                    raise UploadError(f"{os.path.basename(argv[0])} took longer than {limit} s") from None

        if proc.returncode != 0:
            why = _excerpt(err) or _excerpt(out) or "no output"
            raise UploadError(f"{os.path.basename(argv[0])} failed (exit code {proc.returncode}): {why}")
        progress(size, size)
        url = expand(config.get("url") or "", vals).strip() or find_url(out)
        if not url:
            raise UploadError(f"{os.path.basename(argv[0])} printed no link, and no link is set")
        return url


def _stop(proc):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            pass
        try:
            proc.communicate(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass

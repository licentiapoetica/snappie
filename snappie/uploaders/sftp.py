"""SFTP: copy the file into a folder on a server over SSH, and link to it on the web.

The web address of the folder is required: the point is a link to share (copied like any
upload's), and an sftp:// link only works for the uploader.

Runs OpenSSH's own `sftp` in batch mode, so everything ssh can do works the same:
~/.ssh/config (Include, Match, ProxyJump, …), ssh-agent, hardware keys, certificates.
SFTP-only accounts work too (no shell is needed on the server).

- Progress: what its ssh process has written to the network (/proc/<pid>/io), but at most
  how far sftp has read the file (/proc/<pid>/fdinfo).  sftp alone reads megabytes ahead.
- Cancel: the sftp process group is killed, then a second connection removes the half file.
- Password (or key passphrase): handed to ssh through SSH_ASKPASS, a tiny script that
  prints an environment variable; without one, BatchMode keeps ssh from ever prompting.
- Host keys: checked against the user's known_hosts (read only, UpdateHostKeys=no) plus the
  servers trusted in Snappie (settings "upload.ssh_host_keys", written to a temporary
  known_hosts file).  For an unknown server, a second `ssh` with StrictHostKeyChecking=
  accept-new and a throwaway known_hosts file records the key the server offers (through the
  same ProxyJump etc.) and fails at login on purpose; UnknownHostKey then lets the app show its
  fingerprint and ask, as for self-signed certificates.
- The local file is passed as a symlink with a plain name: sftp would expand * ? [ in it.
"""

import os
import posixpath
import random
import re
import shlex
import shutil
import signal
import string
import subprocess
import tempfile
from urllib.parse import quote

from .. import settings
from .. import upload as http
from . import Cancelled, Field, Plugin, UnknownHostKey, UploadError

CONNECT_TIMEOUT = 15
SSH_CONFIG = None      # an ssh_config file instead of ~/.ssh/config (tests)
SFTP_ARGS = []         # more sftp arguments (tests: -l to slow it down)
ASKPASS = "#!/bin/sh\nprintf '%s\\n' \"$SNAPPIE_SSH_PASSWORD\"\n"


class Sftp(Plugin):
    id = "sftp"
    name = "SFTP"
    description = ("Copies the file into a folder on your server over SSH. Uses your SSH keys, "
                   "ssh-agent and ~/.ssh/config, like ssh does.")
    icon = "folder-remote"
    fields = [
        Field("host", "Server", required=True, placeholder="example.com",
              help="A host name, or a Host from ~/.ssh/config."),
        Field("port", "Port", "int", maximum=65535, special="Default (22)"),
        Field("user", "User name", placeholder="from ~/.ssh/config, or your own"),
        Field("password", "Password", "password",
              help="Or the passphrase of the key. Empty: ssh-agent and your keys."),
        Field("key_file", "Private key", "file", placeholder="~/.ssh/id_ed25519",
              help="Empty: the keys in ~/.ssh and ~/.ssh/config."),
        Field("folder", "Folder", placeholder="/var/www/example.com/i",
              help="Where the file goes; made if it's missing. Empty: the login folder."),
        Field("url", "Link", required=True, placeholder="https://example.com/i/",
              help="The web address of that folder; the file name is added "
                   "(or put {filename} where it goes). This link is copied after uploading."),
        Field("naming", "File names", "choice", choices=[
            ("keep", "Keep the file's name"), ("random", "Random, like a8Xk2pQz.png (hard to guess)")]),
    ]

    def available(self):
        if shutil.which("sftp") and shutil.which("ssh"):
            return ""
        from .. import deps
        return f"SFTP needs the OpenSSH client ({deps.install_hint(['openssh'])})"

    def validate(self, config):
        super().validate(config)
        host = (config.get("host") or "").strip()
        if host.startswith("-") or re.search(r"\s", host):
            raise UploadError("The server name can't start with “-” or have spaces")
        if re.search(r"[\x00-\x1f]", config.get("folder") or ""):
            raise UploadError("The folder can't have line breaks")
        if not re.match(r"https?://[^/\s]", (config.get("url") or "").strip(), re.I):
            raise UploadError("The link must be a web address (https://…) of the folder")

    def summary(self, config):
        where = config.get("host") or ""
        if config.get("user"):
            where = f"{config['user']}@{where}"
        if config.get("port"):
            where += f":{config['port']}"
        return f"{where}:{config.get('folder') or '~'}\nLink: {config.get('url') or '(none)'}"

    def upload(self, path, config, progress, cancel):
        self.validate(config)
        try:
            size = os.path.getsize(path)
        except OSError as e:
            raise UploadError(f"Can't read {path}: {e.strerror}") from None
        with Session(config) as s:
            folder = (config.get("folder") or "").strip()
            base = remote_folder(folder)
            filename = re.sub(r"[\x00-\x1f/]", "_", os.path.basename(path))
            naming = config.get("naming") or "keep"
            mkdirs = mkdir_commands(base) if folder else []
            if naming == "random":          # 62^8 names: no need to look first
                name = pick_name(lambda n: False, filename, naming)
            else:
                out = s.run(mkdirs + [f"ls -1 {q(base)}"], cancel)
                taken = {posixpath.basename(line) for line in out if line}
                name = pick_name(taken.__contains__, filename, naming)
                mkdirs = []
            remote = posixpath.join(base, name)
            s.run(mkdirs + [f"put {q(s.local(path))} {q(remote)}"], cancel, watch=(path, size, progress),
                  remote=remote)
            progress(size, size)
            return link(config["url"], name)


# -- running ssh / sftp ---------------------------------------------------------------

def q(arg):
    """Quote an argument for sftp's command line (and ssh's -o values)."""
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _askpass():
    """Path of the askpass script (in the data folder: /tmp may be mounted noexec)."""
    path = os.path.join(settings.DATA_DIR, "ssh-askpass")
    try:
        with open(path) as f:
            if f.read() == ASKPASS and os.access(path, os.X_OK):
                return path
    except OSError:
        pass
    os.makedirs(settings.DATA_DIR, exist_ok=True)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    with os.fdopen(fd, "w") as f:
        f.write(ASKPASS)
    os.chmod(path + ".tmp", 0o700)
    os.replace(path + ".tmp", path)
    return path


class Session:
    """One upload's ssh options, temporary files and resolved target."""

    def __init__(self, config):
        self.host = (config.get("host") or "").strip()
        if not self.host:
            raise UploadError("No server set")
        self.password = config.get("password") or ""
        self.key = os.path.expanduser((config.get("key_file") or "").strip())
        if self.key and not os.path.isfile(self.key):
            raise UploadError(f"The key {self.key} doesn't exist")
        common = ["-F", SSH_CONFIG] if SSH_CONFIG else []
        common += ["-o", f"ConnectTimeout={CONNECT_TIMEOUT}", "-o", "ServerAliveInterval=15",
                   "-o", "ServerAliveCountMax=4", "-o", "UpdateHostKeys=no"]
        if (config.get("user") or "").strip():
            common += ["-o", f"User={config['user'].strip()}"]
        if config.get("port"):
            common += ["-o", f"Port={int(config['port'])}"]
        if self.key:
            common += ["-o", f"IdentityFile={q(self.key)}", "-o", "IdentitiesOnly=yes"]
        self.common = common
        self.env = dict(os.environ)
        if self.password:
            self.env.update(SSH_ASKPASS=_askpass(), SSH_ASKPASS_REQUIRE="force", SNAPPIE_SSH_PASSWORD=self.password)
            self.auth = ["-o", "BatchMode=no", "-o", "NumberOfPasswordPrompts=1"]
        else:
            self.auth = ["-o", "BatchMode=yes"]
        self.target = self._resolve()
        self.tmp = tempfile.mkdtemp(prefix="snappie-sftp-")
        self.ours = os.path.join(self.tmp, "known_hosts")
        with open(self.ours, "w") as f:
            for host, value in http.trusted_host_keys().items():
                f.write(f"{host} {value}\n")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _resolve(self):
        """What ssh will use (`ssh -G`): {host, port, user, entry (known_hosts name), known (files)}."""
        try:
            r = subprocess.run(["ssh", "-G", *self.common, "--", self.host], capture_output=True, text=True,
                               timeout=10, env=self.env, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise UploadError(f"Can't run ssh: {e}") from None
        if r.returncode != 0:
            raise UploadError(f"ssh doesn't accept this server: {_last(r.stderr)}")
        g = {}
        for line in r.stdout.splitlines():
            k, _, v = line.partition(" ")
            g.setdefault(k.lower(), v)
        host, port = g.get("hostname") or self.host, int(g.get("port") or 22)
        name = g.get("hostkeyalias") or host
        return {"host": host, "port": port, "user": g.get("user") or "",
                "entry": name if port == 22 else f"[{name}]:{port}",
                "known": shlex.split(g.get("userknownhostsfile") or "~/.ssh/known_hosts")}

    def local(self, path):
        """The file as a plain-named symlink (sftp would expand * ? [ in its own name)."""
        link = os.path.join(self.tmp, "upload" + re.sub(r"[^\w.]", "", os.path.splitext(path)[1])[:16])
        if not os.path.lexists(link):
            os.symlink(os.path.abspath(path), link)
        return link

    def _sftp(self):
        known = " ".join(q(f) for f in self.target["known"] + [self.ours])
        return ["sftp", *self.common, *self.auth, "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={known}"]

    def run(self, commands, cancel, watch=None, remote=None):
        """Run sftp commands; returns their output lines (the `sftp>` echoes left out)."""
        argv = [*self._sftp(), *SFTP_ARGS, "-b", "-", "--", self.host]
        try:
            code, out, err = _execute(argv, "".join(c + "\n" for c in commands), self.env, cancel, watch)
        except Cancelled:
            if remote:
                self._cleanup(remote)
            raise
        if code != 0:
            raise self._error(err)
        return [ln for ln in out.splitlines() if not ln.startswith("sftp> ")]

    def _cleanup(self, remote):
        """Remove a half-uploaded file (best effort, new connection)."""
        try:
            subprocess.run([*self._sftp(), "-b", "-", "--", self.host], input=f"-rm {q(remote)}\n",
                           capture_output=True, text=True, env=self.env, timeout=CONNECT_TIMEOUT + 5,
                           start_new_session=True)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _error(self, err):
        t = self.target
        where = t["host"] if t["port"] == 22 else f"{t['host']}:{t['port']}"
        if "REMOTE HOST IDENTIFICATION HAS CHANGED" in err:
            fp = re.search(r"(SHA256:[A-Za-z0-9+/=]+)", err)
            new = f" New key: {fp.group(1)}" if fp else ""
            offending = re.search(r"Offending \S+ key in (.+):\d+", err)
            if offending and offending.group(1).strip() == self.ours:
                return UploadError(f"The host key of {where} changed since you trusted it. If you expected that, "
                                   f"remove the old one in Settings → Uploads → Trusted Servers.{new}")
            return UploadError(f"The host key of {where} doesn't match ~/.ssh/known_hosts — the server was "
                               f"reinstalled, or someone is in between. If you expected that: "
                               f"ssh-keygen -R {shlex.quote(t['entry'])}.{new}")
        if "Host key verification failed" in err:
            return self._unknown_host(err)
        if "Permission denied (" in err:
            return UploadError(f"{t['user']}@{where} didn't accept the "
                               + ("password or keys" if self.password else "SSH keys (no password set)"))
        if "Could not resolve hostname" in err:
            return UploadError(f"Can't find the server {t['host']}")
        m = re.search(r"connect to host \S+ port \d+: (.+)", err)
        if m:
            return UploadError(f"Can't connect to {where}: {m.group(1).strip()}")
        return UploadError(f"SFTP to {where} failed: {_last(err) or 'no reason given'}")

    def _unknown_host(self, err):
        """Get the key the server offers (the same way the real connection would) -> UnknownHostKey."""
        scan = os.path.join(self.tmp, "scan")
        argv = ["ssh", *self.common, "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"UserKnownHostsFile={q(scan)}", "-o", "GlobalKnownHostsFile=/dev/null",
                "-o", "HashKnownHosts=no", "-o", "PubkeyAuthentication=no", "-o", "PasswordAuthentication=no",
                "-o", "KbdInteractiveAuthentication=no", "-o", "GSSAPIAuthentication=no",
                "-o", "HostbasedAuthentication=no", "--", self.host, "true"]
        try:
            subprocess.run(argv, capture_output=True, text=True, env=dict(self.env, SSH_AUTH_SOCK=""),
                           timeout=CONNECT_TIMEOUT + 10, stdin=subprocess.DEVNULL, start_new_session=True)
            with open(scan) as f:
                fields = next(ln.split() for ln in f if ln.strip() and not ln.startswith(("#", "@")))
            return UnknownHostKey(self.target["entry"], fields[-2], fields[-1])
        except (OSError, subprocess.TimeoutExpired, StopIteration, IndexError):
            return UploadError(f"Can't verify the server {self.target['host']}: {_last(err)}")


def _last(err):
    """The line of ssh's error output that says what went wrong."""
    lines = [ln.strip() for ln in (err or "").splitlines()
             if ln.strip() and ln.strip() not in ("Connection closed", "Connection closed.")
             and not ln.startswith(("Warning: Permanently added", "@"))]
    return lines[-1][:300] if lines else ""


def _read_pos(pid, real):
    """How far process `pid` has read the file `real`, or None."""
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return None
    for fd in fds:
        try:
            if os.readlink(f"/proc/{pid}/fd/{fd}") != real:
                continue
            with open(f"/proc/{pid}/fdinfo/{fd}") as f:
                for line in f:
                    if line.startswith("pos:"):
                        return int(line.split()[1])
        except (OSError, ValueError):
            continue
    return None


def _ssh_written(pid):
    """Bytes written so far by the ssh process that sftp started, or None."""
    try:
        with open(f"/proc/{pid}/task/{pid}/children") as f:
            children = f.read().split()
        for child in children:
            with open(f"/proc/{child}/io") as f:
                for line in f:
                    if line.startswith("wchar:"):
                        return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


def _execute(argv, stdin, env, cancel, watch=None):
    """Run argv with `stdin`, watching for cancel and progress.  -> (exit code, stdout, stderr)
    watch: (path, size, progress) to report how far the process has read `path`."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, errors="replace", env=env, start_new_session=True)
    except OSError as e:
        raise UploadError(f"Can't run {argv[0]}: {e.strerror or e}") from None
    real = os.path.realpath(watch[0]) if watch else None
    last = -1
    while True:
        try:
            out, err = proc.communicate(stdin, timeout=0.15)
            return proc.returncode, out, err
        except subprocess.TimeoutExpired:
            stdin = None     # given once: communicate() keeps sending it on the next calls
            if cancel.is_set():
                _kill(proc)
                raise Cancelled() from None
            if watch:
                pos = _read_pos(proc.pid, real)
                sent = _ssh_written(proc.pid)    # (+ a few KB of handshake: close enough)
                if pos is not None and sent is not None:
                    pos = min(pos, sent)
                if pos is not None and pos > last:
                    last = pos
                    watch[2](min(pos, watch[1]), watch[1])


def _kill(proc):
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


# -- paths and names -------------------------------------------------------------

def remote_folder(folder):
    """The folder as an SFTP path: "~/x" and "~" are relative to the login folder (SFTP doesn't
    expand ~), "" is the login folder itself."""
    if folder == "~" or folder.startswith("~/"):
        folder = folder[2:]
    return folder.rstrip("/") or ("/" if folder.startswith("/") else ".")


def mkdir_commands(base):
    """`-mkdir` for each level of the folder (the - ignores "already exists")."""
    out, cur = [], "/" if base.startswith("/") else ""
    for part in base.split("/"):
        if not part or part == ".":
            continue
        cur = posixpath.join(cur, part) if cur else part
        out.append(f"-mkdir {q(cur)}")
    return out


def pick_name(taken, filename, naming="keep"):
    """A free name in the folder: the file's own (with -2, -3, … when taken) or a random one.
    taken(name) -> bool."""
    stem, ext = os.path.splitext(filename)
    if naming == "random":
        ext = ext.lower()
        for _ in range(20):
            name = "".join(random.choices(string.ascii_letters + string.digits, k=8)) + ext
            if not taken(name):
                return name
        raise UploadError("Couldn't find a free random file name")
    name, n = filename, 2
    while taken(name):
        name, n = f"{stem}-{n}{ext}", n + 1
    return name


def link(url, name):
    """The link for an uploaded file: the folder's web address + its name."""
    url = url.strip()
    if "{filename}" in url:
        return url.replace("{filename}", quote(name))
    return url + ("" if url.endswith("/") else "/") + quote(name)

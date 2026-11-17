import json
import os
import re
import shutil
import socket
import stat
import subprocess
import textwrap
import threading
import time

import pytest

from snappie import settings, upload, uploaders
from snappie.uploaders import command, sftp


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(settings, "SETTINGS", str(tmp_path / "config" / "settings.json"))
    monkeypatch.setattr(settings, "UPLOADERS_DIR", str(tmp_path / "config" / "uploaders"))
    monkeypatch.setattr(settings, "PLUGINS_DIR", str(tmp_path / "plugins"))
    monkeypatch.setattr(uploaders, "_log", lambda *a: None)
    os.makedirs(settings.UPLOADERS_DIR)
    os.makedirs(settings.PLUGINS_DIR)
    return tmp_path


def write(path, data):
    with open(path, "w") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))


@pytest.fixture
def blob(tmp_path):
    p = tmp_path / "my shot '1'.png"
    p.write_bytes(os.urandom(300_000))
    return p


# -- uploader files --

def test_entries_lists_every_kind(dirs):
    up = settings.UPLOADERS_DIR
    write(f"{up}/host.sxcu", {"Name": "Host", "RequestURL": "https://h.test/up", "Body": "MultipartFormData"})
    write(f"{up}/server.json", {"Plugin": "sftp", "Name": "My server", "Config": {"host": "x"}})
    write(f"{up}/gone.json", {"Plugin": "imgur", "Name": "Imgur", "Config": {}})
    write(f"{up}/broken.sxcu", "{nope")
    got = {e.file: e for e in uploaders.entries()}
    assert [e.name for e in uploaders.entries()] == ["broken", "Host", "Imgur", "My server"]
    assert got["host.sxcu"].plugin_id == "sxcu" and not got["host.sxcu"].error
    assert got["server.json"].plugin.name == "SFTP" and got["server.json"].editable
    assert not got["host.sxcu"].editable
    assert "isn't installed" in got["gone.json"].error
    assert "JSON" in got["broken.sxcu"].error
    # new fields get their defaults
    assert got["server.json"].full_config()["naming"] == "keep"


def test_get_by_file_name_display_name_path_and_default(dirs, tmp_path):
    write(f"{settings.UPLOADERS_DIR}/server.json", {"Plugin": "command", "Name": "My Server", "Config": {}})
    assert uploaders.get("server.json").name == "My Server"
    assert uploaders.get("my server").file == "server.json"
    assert uploaders.get("server").file == "server.json"
    outside = tmp_path / "x.sxcu"
    write(outside, {"RequestURL": "https://h.test/"})
    assert uploaders.get(str(outside)).file == str(outside)
    with pytest.raises(upload.UploadError, match="No uploader chosen"):
        uploaders.get()
    settings.set("upload.uploader", "server.json")
    assert uploaders.get().name == "My Server"
    with pytest.raises(upload.UploadError, match="not found"):
        uploaders.get("nope")


def test_save_is_private_unique_and_first_becomes_default(dirs):
    a = uploaders.save("sftp", "My server", {"host": "h", "password": "secret"})
    b = uploaders.save("sftp", "My server", {"host": "h2"})
    assert (a, b) == ("My server.json", "My server 2.json")
    path = os.path.join(settings.UPLOADERS_DIR, a)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert settings.get("upload.uploader") == a
    assert uploaders.get(a).config == {"host": "h", "password": "secret"}
    assert uploaders.save("sftp", "Renamed", {"host": "h3"}, a) == a
    assert uploaders.get(a).name == "Renamed"
    assert not [f for f in os.listdir(settings.UPLOADERS_DIR) if f.endswith(".tmp")]


def test_remove_clears_the_default(dirs):
    f = uploaders.save("command", "C", {"command": "true"})
    uploaders.remove(f)
    assert uploaders.entries() == [] and settings.get("upload.uploader") == ""


# -- user plugins --

PLUGIN = textwrap.dedent('''
    from snappie.uploaders import Field, Plugin, UploadError

    class Echo(Plugin):
        id = "echo"
        name = "Echo"
        fields = [Field("base", "Base URL", required=True), Field("n", "Number", "int", default=3)]

        def upload(self, path, config, progress, cancel):
            progress(1, 2)
            if config["base"] == "boom":
                raise RuntimeError("kaput")
            return config["base"] + "/" + str(config["n"])
''')


def test_user_plugin_loads_validates_and_uploads(dirs, blob):
    write(f"{settings.PLUGINS_DIR}/echo.py", PLUGIN)
    p = uploaders.plugin("echo")
    assert p is not None and p.source.endswith("echo.py")
    with pytest.raises(upload.UploadError, match="Base URL is required"):
        p.validate({"base": " "})
    seen = []
    res = uploaders.upload(str(blob), {"Plugin": "echo", "Name": "E", "Config": {"base": "https://e.test"}},
                           progress=lambda s, t: seen.append((s, t)))
    assert isinstance(res, upload.UploadResult) and res.url == "https://e.test/3"
    assert seen == [(1, 2)]
    # a crash in plugin code is an UploadError, not a crash of the app
    with pytest.raises(upload.UploadError, match="RuntimeError: kaput"):
        uploaders.upload(str(blob), {"Plugin": "echo", "Config": {"base": "boom"}})


def test_user_plugins_reload_and_report_errors(dirs):
    write(f"{settings.PLUGINS_DIR}/echo.py", PLUGIN)
    write(f"{settings.PLUGINS_DIR}/bad.py", "import nonexistent_module_xyz\n")
    write(f"{settings.PLUGINS_DIR}/clash.py", PLUGIN.replace('id = "echo"', 'id = "sftp"'))
    assert "echo" in uploaders.plugins()
    assert "ModuleNotFoundError" in uploaders.load_errors["bad.py"]
    assert "already taken" in uploaders.load_errors["clash.py"]
    assert uploaders.plugin("sftp").__class__ is sftp.Sftp
    os.remove(f"{settings.PLUGINS_DIR}/echo.py")
    assert "echo" not in uploaders.plugins()
    write(f"{settings.UPLOADERS_DIR}/b.json", {"Plugin": "bad", "Config": {}})
    assert "doesn't load" in uploaders.get("b.json").error


def test_cancel_before_start(dirs, blob):
    ev = threading.Event()
    ev.set()
    with pytest.raises(upload.Cancelled):
        uploaders.upload(str(blob), {"Plugin": "command", "Config": {"command": "true"}}, cancel=ev)


# -- command --

def run_command(blob, **config):
    return uploaders.upload(str(blob), {"Plugin": "command", "Config": config})


def test_command_gets_safe_arguments_and_env(blob):
    res = run_command(blob, command='sh -c \'echo "uploaded $1 as $SNAPPIE_FILENAME"; echo https://x.test/a\' - {file}')
    assert res.url == "https://x.test/a"
    res = run_command(blob, command="printf %s {filename}", url="https://x.test/{random}.{ext}?n={name}")
    assert res.url.startswith("https://x.test/") and res.url.endswith(".png?n=my shot '1'")


def test_command_random_is_the_same_in_command_and_link(blob):
    res = run_command(blob, command="echo {random}", url="https://x.test/{random}")
    assert len(res.url.rsplit("/", 1)[1]) == 8


def test_command_errors(blob):
    with pytest.raises(upload.UploadError, match=r"failed \(exit code 3\): nope"):
        run_command(blob, command="sh -c 'echo nope >&2; exit 3'")
    with pytest.raises(upload.UploadError, match="printed no link"):
        run_command(blob, command="echo Transferred: 1 file")
    with pytest.raises(upload.UploadError, match="Can't run"):
        run_command(blob, command="/nonexistent/thing {file}")
    with pytest.raises(upload.UploadError, match="took longer than 1 s"):
        run_command(blob, command="sleep 30", timeout=1)


def test_command_cancel_kills_it(blob):
    ev = threading.Event()
    threading.Timer(0.3, ev.set).start()
    t = time.monotonic()
    with pytest.raises(upload.Cancelled):
        uploaders.upload(str(blob), {"Plugin": "command", "Config": {"command": "sh -c 'sleep 30; echo x'"}},
                         cancel=ev)
    assert time.monotonic() - t < 5


def test_command_validate():
    p = command.Command()
    with pytest.raises(upload.UploadError):
        p.validate({"command": "echo 'unclosed"})
    p.validate({"command": "rclone copyto {file} r:x"})


# -- sftp helpers --

def test_pick_name():
    taken = {"a.png", "a-2.png"}
    assert sftp.pick_name(taken.__contains__, "a.png") == "a-3.png"
    assert sftp.pick_name(taken.__contains__, "b.png") == "b.png"
    name = sftp.pick_name(taken.__contains__, "Shot.PNG", "random")
    assert len(name) == 12 and name.endswith(".png")


def test_remote_folder():
    assert [sftp.remote_folder(f) for f in ("", "~", "~/public_html/", "/", "/srv/i/", "up")] == \
        [".", ".", "public_html", "/", "/srv/i", "up"]


def test_link():
    assert sftp.link("https://h.test/i", "a b.png") == "https://h.test/i/a%20b.png"
    assert sftp.link("https://h.test/i/", "a.png") == "https://h.test/i/a.png"
    assert sftp.link("https://h.test/v?f={filename}", "a.png") == "https://h.test/v?f=a.png"


def test_sftp_needs_a_web_link():
    p = sftp.Sftp()
    for url in ("", "  ", "sftp://h.test/i", "h.test/i", "https://"):
        with pytest.raises(upload.UploadError, match="Link is required|must be a web address"):
            p.validate({"host": "h.test", "url": url})
    p.validate({"host": "h.test", "url": "https://h.test/i/"})
    p.validate({"host": "h.test", "url": "http://10.0.0.2:8080/{filename}"})


def test_quoting_and_mkdir_commands():
    assert sftp.q('my "q" \\ *.png') == '"my \\"q\\" \\\\ *.png"'
    assert sftp.mkdir_commands("/srv/a b/i") == ['-mkdir "/srv"', '-mkdir "/srv/a b"', '-mkdir "/srv/a b/i"']
    assert sftp.mkdir_commands("up/x") == ['-mkdir "up"', '-mkdir "up/x"']
    assert sftp.mkdir_commands(".") == []


def test_askpass_prints_the_password(dirs, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(dirs / "data"))
    path = sftp._askpass()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o700 and sftp._askpass() == path
    out = subprocess.run([path, "user@host's password: "], capture_output=True, text=True,
                         env=dict(os.environ, SNAPPIE_SSH_PASSWORD="pa ss'$1\"")).stdout
    assert out == "pa ss'$1\"\n"


def ssh_config(tmp_path, text=""):
    """An ssh_config that keeps the real ~/.ssh out (ssh reads the passwd home, not $HOME)."""
    path = tmp_path / "ssh_config"
    path.write_text(text + "Host *\n  UserKnownHostsFile /dev/null\n  GlobalKnownHostsFile /dev/null\n"
                           "  IdentityAgent none\n")
    return str(path)


def test_session_resolves_through_ssh_config(tmp_path, monkeypatch):
    if not shutil.which("ssh"):
        pytest.skip("no ssh")
    monkeypatch.setattr(sftp, "SSH_CONFIG", ssh_config(tmp_path, "Host box\n  HostName box.example.test\n"
                                                                 "  Port 2200\n  User admin\n\n"))
    with sftp.Session({"host": "box"}) as s:
        assert (s.target["host"], s.target["port"], s.target["user"]) == ("box.example.test", 2200, "admin")
        assert s.target["entry"] == "[box.example.test]:2200" and s.target["known"] == ["/dev/null"]
        tmp = s.tmp
    assert not os.path.exists(tmp)
    with sftp.Session({"host": "box", "port": 22, "user": "me"}) as s:
        assert (s.target["port"], s.target["user"], s.target["entry"]) == (22, "me", "box.example.test")


def test_unreachable_server(tmp_path, monkeypatch, blob):
    if not shutil.which("sftp"):
        pytest.skip("no sftp")
    monkeypatch.setattr(sftp, "SSH_CONFIG", ssh_config(tmp_path))
    with pytest.raises(upload.UploadError, match=r"Can't connect to 127.0.0.1:\d+: Connection refused"):
        uploaders.upload(str(blob), {"Plugin": "sftp", "Config": {"host": "127.0.0.1", "port": _free_port(),
                                                                "url": "https://h.test/"}})


def test_ssh_fingerprint_matches_ssh_keygen(tmp_path):
    keygen = shutil.which("ssh-keygen")
    if not keygen:
        pytest.skip("no ssh-keygen")
    subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "k")], check=True)
    keytype, b64 = (tmp_path / "k.pub").read_text().split()[:2]
    want = subprocess.run([keygen, "-lf", str(tmp_path / "k.pub")], capture_output=True, text=True).stdout.split()[1]
    assert upload.ssh_fingerprint(b64) == want
    err = upload.UnknownHostKey("[h.test]:2222", keytype, b64)
    assert err.where == "h.test:2222" and err.fingerprint_text == want and "ED25519" in err.fingerprint_label


# -- sftp against a real (unprivileged, throwaway) sshd --

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def sshd(tmp_path, monkeypatch, dirs):
    daemon = next((p for p in ("/usr/bin/sshd", "/usr/sbin/sshd") if os.path.exists(p)), None)
    keygen = shutil.which("ssh-keygen")
    if not daemon or not keygen or not shutil.which("sftp"):
        pytest.skip("needs sshd, sftp and ssh-keygen")
    d = tmp_path / "sshd"
    d.mkdir()
    for k in ("host", "client"):
        subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(d / k)], check=True)
    shutil.copy(d / "client.pub", d / "authorized_keys")
    (tmp_path / "login").mkdir()    # the SFTP login folder: relative paths land here, not in the real home
    port = _free_port()
    (d / "config").write_text(textwrap.dedent(f"""\
        Port {port}
        ListenAddress 127.0.0.1
        HostKey {d}/host
        AuthorizedKeysFile {d}/authorized_keys
        PidFile none
        UsePAM no
        StrictModes no
        PasswordAuthentication no
        KbdInteractiveAuthentication no
        Subsystem sftp internal-sftp -d {tmp_path}/login
        LogLevel ERROR
        """))
    proc = subprocess.Popen([daemon, "-D", "-e", "-f", str(d / "config")], stderr=subprocess.PIPE, text=True)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            if proc.poll() is not None:
                pytest.skip(f"sshd didn't start: {proc.stderr.read()[:300]}")
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.skip("sshd didn't start")
    # no real ssh config, keys, agent or known_hosts of the user running the tests
    monkeypatch.setattr(sftp, "SSH_CONFIG", ssh_config(tmp_path))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    yield {"port": port, "key": str(d / "client"), "host_pub": (d / "host.pub").read_text().split()}
    proc.terminate()
    proc.wait(5)


def test_sftp_upload_end_to_end(sshd, tmp_path, blob):
    folder = tmp_path / "www" / "i"
    cfg = {"Plugin": "sftp", "Name": "S", "Config": {
        "host": "127.0.0.1", "port": sshd["port"], "key_file": sshd["key"], "folder": str(folder),
        "url": "https://h.test/i/"}}
    with pytest.raises(upload.UnknownHostKey) as e:
        uploaders.upload(str(blob), cfg)
    assert e.value.host == f"[127.0.0.1]:{sshd['port']}" and e.value.key_b64 == sshd["host_pub"][1]
    e.value.trust()

    seen = []
    res = uploaders.upload(str(blob), cfg, progress=lambda s, t: seen.append((s, t)))
    assert res.url == "https://h.test/i/my%20shot%20%271%27.png"
    assert (folder / blob.name).read_bytes() == blob.read_bytes()
    assert seen[-1] == (blob.stat().st_size, blob.stat().st_size)
    # the same name again gets a -2; glob characters stay literal; random names
    assert uploaders.upload(str(blob), cfg).url.endswith("%271%27-2.png")
    odd = tmp_path / 'a "b" *[1]?.png'
    odd.write_bytes(b"x")
    assert uploaders.upload(str(odd), cfg).url.endswith("a%20%22b%22%20%2A%5B1%5D%3F.png")
    assert (folder / odd.name).read_bytes() == b"x"
    cfg["Config"].update(naming="random")
    url = uploaders.upload(str(blob), cfg).url
    assert re.fullmatch(r"https://h\.test/i/[A-Za-z0-9]{8}\.png", url)
    assert os.path.exists(folder / url.rsplit("/", 1)[1]) and len(os.listdir(folder)) == 4


def test_sftp_relative_folder_and_progress(sshd, tmp_path, blob, monkeypatch):
    upload.trust_host_key(f"[127.0.0.1]:{sshd['port']}", *sshd["host_pub"][:2])
    monkeypatch.setattr(sftp, "SFTP_ARGS", ["-l", "8000"])      # ~1 MB/s: 2 s for 2 MB
    blob.write_bytes(os.urandom(2_000_000))
    seen = []
    cfg = {"Plugin": "sftp", "Config": {"host": "127.0.0.1", "port": sshd["port"], "key_file": sshd["key"],
                                        "folder": "~/sub", "url": "https://h.test/{filename}"}}
    url = uploaders.upload(str(blob), cfg, progress=lambda s, t: seen.append(s)).url
    # relative to the login folder
    assert (tmp_path / "login" / "sub" / blob.name).read_bytes() == blob.read_bytes()
    assert url == "https://h.test/my%20shot%20%271%27.png"
    assert len(seen) >= 3 and seen == sorted(seen) and seen[-1] == blob.stat().st_size


def test_sftp_cancel_removes_the_partial_file(sshd, tmp_path, blob, monkeypatch):
    upload.trust_host_key(f"[127.0.0.1]:{sshd['port']}", *sshd["host_pub"][:2])
    ev = threading.Event()
    monkeypatch.setattr(sftp, "SFTP_ARGS", ["-l", "800"])
    folder = tmp_path / "out"
    cfg = {"Plugin": "sftp", "Config": {"host": "127.0.0.1", "port": sshd["port"], "key_file": sshd["key"],
                                        "folder": str(folder), "url": "https://h.test/"}}
    with pytest.raises(upload.Cancelled):
        uploaders.upload(str(blob), cfg, progress=lambda s, t: ev.set() if s > 0 else None, cancel=ev)
    assert os.listdir(folder) == []


def test_sftp_changed_host_key_and_bad_login(sshd, tmp_path, blob):
    cfg = {"Plugin": "sftp", "Config": {"host": "127.0.0.1", "port": sshd["port"], "key_file": sshd["key"],
                                        "url": "https://h.test/"}}
    other = tmp_path / "other"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
    upload.trust_host_key(f"[127.0.0.1]:{sshd['port']}", *(tmp_path / "other.pub").read_text().split()[:2])
    with pytest.raises(upload.UploadError, match="changed since you trusted it"):
        uploaders.upload(str(blob), cfg)
    upload.trust_host_key(f"[127.0.0.1]:{sshd['port']}", *sshd["host_pub"][:2])
    cfg["Config"]["key_file"] = str(other)
    with pytest.raises(upload.UploadError, match="didn't accept the SSH keys"):
        uploaders.upload(str(blob), cfg)


def test_the_documented_example_plugin_loads(dirs, monkeypatch, blob):
    doc = open(os.path.join(os.path.dirname(__file__), "..", "docs", "uploader-plugins.md")).read()
    code = doc.split("```python\n", 1)[1].split("```", 1)[0]
    write(f"{settings.PLUGINS_DIR}/litterbox.py", code)
    p = uploaders.plugin("litterbox")
    assert p is not None, uploaders.load_errors
    assert [f.key for f in p.fields] == ["time"] and p.defaults() == {"time": "1h"}
    sent = {}

    def fake_post(url, path, **kw):
        sent.update(kw, url=url)
        return type("R", (), {"text": "https://litter.catbox.moe/abc.png\n"})()
    monkeypatch.setattr("snappie_plugin_litterbox.post_file", fake_post)
    f = uploaders.save("litterbox", "Litterbox", {"time": "24h"})
    assert uploaders.upload(str(blob), f).url == "https://litter.catbox.moe/abc.png"
    assert sent["fields"] == {"reqtype": "fileupload", "time": "24h"} and sent["field"] == "fileToUpload"


def test_sftp_password_goes_through_askpass(sshd, tmp_path, blob, monkeypatch):
    # a passphrase-protected key: ssh asks for it the same way as for a password (SSH_ASKPASS)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))
    upload.trust_host_key(f"[127.0.0.1]:{sshd['port']}", *sshd["host_pub"][:2])
    locked = tmp_path / "locked"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "s3cret phrase", "-f", str(locked)], check=True)
    with open(tmp_path / "sshd" / "authorized_keys", "a") as f:
        f.write((tmp_path / "locked.pub").read_text())
    cfg = {"Plugin": "sftp", "Config": {"host": "127.0.0.1", "port": sshd["port"], "key_file": str(locked),
                                        "folder": str(tmp_path / "pw"), "url": "https://h.test/"}}
    with pytest.raises(upload.UploadError, match="didn't accept the SSH keys"):
        uploaders.upload(str(blob), cfg)
    cfg["Config"]["password"] = "wrong"
    with pytest.raises(upload.UploadError, match="didn't accept the password or keys"):
        uploaders.upload(str(blob), cfg)
    cfg["Config"]["password"] = "s3cret phrase"
    uploaders.upload(str(blob), cfg)
    assert (tmp_path / "pw" / blob.name).exists()

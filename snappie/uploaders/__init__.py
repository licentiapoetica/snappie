"""Uploaders: where uploads go, and the plugins that know how to get them there.

A *plugin* is an uploader type: ShareX custom uploaders (.sxcu), SFTP, a command, or
anything a user plugin adds. An *uploader* is one set-up destination of some type,
stored as a file in UPLOADERS_DIR, and named by that file in settings and on the
command line (so `upload.uploader` from older versions keeps working):

- `*.sxcu`: a ShareX custom uploader, as imported.
- `*.json`: {"Plugin": "<plugin id>", "Name": "...", "Config": {field: value}}.  Written
  with mode 0600: the config can hold a password or a token.

User plugins are .py files in PLUGINS_DIR (~/.local/share/snappie/plugins).  Each
Plugin subclass with an `id` becomes an uploader type; its `fields` make the form in
Settings → Uploads.  Files are loaded again when they change, so a running app picks up
new or edited plugins.  See docs/uploader-plugins.md.
"""

import importlib.util
import json
import os
import re
import sys
import threading
import traceback

from .. import settings
from ..upload import (Cancelled, StreamBody, UnknownHostKey, Untrusted, UntrustedCertificate,  # noqa: F401
                      UploadError, UploadResult, http_error, load_config, post_file, read_sxcu, request)

BUILTIN = ("sxcu", "sftp", "command")     # modules in this package, in menu order

# for plugins: `from snappie.uploaders import Plugin, Field, UploadError, post_file, ...`
__all__ = ["Plugin", "Field", "UploadResult", "UploadError", "Cancelled", "Untrusted", "UntrustedCertificate",
           "UnknownHostKey", "StreamBody", "request", "post_file", "http_error",
           "entries", "get", "upload", "plugins"]


class Field:
    """One setting of a plugin, shown as a row of the uploader form.

    kind: text | password (hidden while typing) | int | bool | choice | file (a local path).
    choices: [(value, label)] for kind="choice".  help: a hint under the field."""

    KINDS = ("text", "password", "int", "bool", "choice", "file")

    def __init__(self, key, label, kind="text", default=None, required=False, help="", placeholder="",
                 choices=(), minimum=0, maximum=2**31 - 1, special=""):
        if kind not in self.KINDS:
            raise ValueError(f"unknown field kind {kind!r}")
        self.key, self.label, self.kind = key, label, kind
        self.required, self.help, self.placeholder = required, help, placeholder
        self.choices = list(choices)
        self.minimum, self.maximum = minimum, maximum
        self.special = special          # int: shown for the minimum, e.g. "Default"
        if default is None:
            default = {"int": minimum, "bool": False}.get(kind, "")
            if kind == "choice" and self.choices:
                default = self.choices[0][0]
        self.default = default

    def empty(self, value):
        return value is None or (isinstance(value, str) and not value.strip())


class Plugin:
    """An uploader type.  Subclass it, set `id` and `name`, list `fields`, write upload().

    One instance serves every uploader of this type, maybe from several threads: keep
    per-upload state in upload()'s locals."""

    id = ""              # stored in the uploader files: lowercase, never change it
    name = ""            # e.g. "SFTP"
    description = ""     # one line at the top of the form
    icon = "cloud-upload"
    fields = []          # [Field]
    editable = True      # set up with the form (.sxcu files are imported instead)
    source = ""          # the plugin's file, for user plugins

    def available(self):
        """Why this can't work on this system (e.g. a missing module), or "" if it can."""
        return ""

    def defaults(self):
        return {f.key: f.default for f in self.fields}

    def validate(self, config):
        """Raise UploadError for a config that can't work.  Called before saving."""
        for f in self.fields:
            if f.required and f.empty(config.get(f.key)):
                raise UploadError(f"{f.label} is required")

    def summary(self, config):
        """One line about an uploader for the settings, e.g. "user@host:/var/www"."""
        return ""

    def upload(self, path, config, progress, cancel):
        """Upload the file at `path`; return an UploadResult (or just the link).

        Runs on a worker thread.  config: this uploader's values, with the field defaults
        filled in.  progress(sent, total): call it as bytes go out.  cancel: a threading.Event
        set when the user cancels; stop and raise Cancelled.  Raise UploadError with a short,
        readable message when something fails."""
        raise NotImplementedError


# -- plugin registry ---------------------------------------------------------------

_lock = threading.RLock()
_builtin = {}
_user = {}
_user_stamp = None
load_errors = {}          # user plugin file name -> why it didn't load


def _log(*a):
    from ..app_util import log
    log(*a)


def _classes(module):
    return [c for c in vars(module).values()
            if isinstance(c, type) and issubclass(c, Plugin) and c is not Plugin
            and c.__module__ == module.__name__ and c.id]


def _load_builtin():
    if not _builtin:
        import importlib
        for name in BUILTIN:
            for cls in _classes(importlib.import_module(f"{__name__}.{name}")):
                _builtin[cls.id] = cls()


def _scan_user():
    """(Re)load the user plugins when files in PLUGINS_DIR were added, changed or removed."""
    global _user_stamp
    try:
        files = sorted(f for f in os.listdir(settings.PLUGINS_DIR)
                       if f.endswith(".py") and not f.startswith((".", "_")))
    except OSError:
        files = []
    stamp = []
    for f in files:
        try:
            stamp.append((f, os.stat(os.path.join(settings.PLUGINS_DIR, f)).st_mtime_ns))
        except OSError:
            pass
    stamp = (settings.PLUGINS_DIR, tuple(stamp))
    if stamp == _user_stamp:
        return
    _user_stamp = stamp
    _user.clear()
    load_errors.clear()
    for f, _ in stamp[1]:
        path = os.path.join(settings.PLUGINS_DIR, f)
        modname = "snappie_plugin_" + re.sub(r"\W", "_", f[:-3])
        try:
            spec = importlib.util.spec_from_file_location(modname, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[modname] = module
            spec.loader.exec_module(module)
            classes = _classes(module)
        except Exception as e:  # noqa: BLE001 - a broken plugin mustn't break uploads
            sys.modules.pop(modname, None)
            load_errors[f] = f"{type(e).__name__}: {e}"
            _log(f"uploader plugin {f} failed to load:\n" + traceback.format_exc().rstrip())
            continue
        if not classes:
            load_errors[f] = "no Plugin subclass with an id in it"
        for cls in classes:
            if cls.id in _builtin or cls.id in _user:
                load_errors[f] = f"the plugin id “{cls.id}” is already taken"
                continue
            try:
                plugin = cls()
            except Exception as e:  # noqa: BLE001
                load_errors[f] = f"{cls.__name__}: {type(e).__name__}: {e}"
                continue
            plugin.source = path
            _user[cls.id] = plugin


def plugins():
    """{id: Plugin}: the built-in types, then user plugins."""
    with _lock:
        _load_builtin()
        _scan_user()
        return dict(_builtin, **_user)


def plugin(plugin_id):
    return plugins().get(plugin_id)


# -- uploaders (files in UPLOADERS_DIR) ----------------------------------------------

class Entry:
    """One set-up uploader."""

    def __init__(self, file, name, plugin_id, config, plugin=None, error=""):
        self.file = file              # file name in UPLOADERS_DIR (or a path for outside files)
        self.name = name
        self.plugin_id = plugin_id
        self.config = config          # .sxcu: the file's JSON as is; plugins: {field: value}
        self.plugin = plugin
        self.error = error            # why it can't be used, "" if it can

    @property
    def path(self):
        return self.file if os.path.isabs(self.file) else os.path.join(settings.UPLOADERS_DIR, self.file)

    @property
    def editable(self):
        return self.plugin is not None and self.plugin.editable

    def full_config(self):
        """The config with the plugin's defaults filled in for fields it doesn't have yet."""
        if self.plugin is None or self.plugin_id == "sxcu":
            return dict(self.config)
        return dict(self.plugin.defaults(), **self.config)

    def summary(self):
        if self.error or self.plugin is None:
            return self.error
        try:
            return self.plugin.summary(self.full_config())
        except Exception as e:  # noqa: BLE001 - user plugin code
            return f"{type(e).__name__}: {e}"

    def __repr__(self):
        return f"Entry({self.file!r}, {self.name!r}, {self.plugin_id!r})"


def _missing_plugin(pid):
    broken = [f for f in load_errors if os.path.splitext(f)[0] == pid]
    if broken:
        return f"its plugin {broken[0]} doesn't load: {load_errors[broken[0]]}"
    return f"the “{pid}” plugin isn't installed (put it in {settings.PLUGINS_DIR})"


def from_dict(data, file=""):
    """An Entry from an uploader's JSON: an .sxcu config or {"Plugin": ..., "Config": ...}."""
    stem = os.path.splitext(os.path.basename(file))[0] or "uploader"
    if not isinstance(data, dict):
        return Entry(file, stem, "", {}, error="not an uploader file")
    if "Plugin" in data:
        pid = str(data["Plugin"])
        config = data.get("Config") if isinstance(data.get("Config"), dict) else {}
        p = plugin(pid)
        return Entry(file, str(data.get("Name") or stem), pid, config, p, "" if p else _missing_plugin(pid))
    name = str(data.get("Name") or stem)
    if not data.get("RequestURL"):
        return Entry(file, name, "sxcu", data, plugin("sxcu"), "not a ShareX custom uploader (no RequestURL)")
    try:
        load_config(data)
        error = ""
    except UploadError as e:
        error = str(e)
    return Entry(file, name, "sxcu", data, plugin("sxcu"), error)


def load_file(path):
    inside = os.path.dirname(os.path.abspath(path)) == os.path.abspath(settings.UPLOADERS_DIR)
    file = os.path.basename(path) if inside else path
    try:
        with open(path, encoding="utf-8-sig") as f:    # ShareX writes a BOM sometimes
            data = json.load(f)
    except OSError as e:
        return Entry(file, os.path.splitext(os.path.basename(path))[0], "", {},
                     error=f"can't read it: {e.strerror}")
    except ValueError as e:
        return Entry(file, os.path.splitext(os.path.basename(path))[0], "", {}, error=f"not valid JSON: {e}")
    return from_dict(data, file)


def entries():
    """Every set-up uploader, sorted by name (including broken ones, with .error set)."""
    try:
        names = os.listdir(settings.UPLOADERS_DIR)
    except OSError:
        return []
    out = [load_file(os.path.join(settings.UPLOADERS_DIR, n)) for n in names
           if n.lower().endswith((".sxcu", ".json"))]
    return sorted(out, key=lambda e: (e.name.lower(), e.file))


def get(uploader=None):
    """The Entry for `uploader`: a file name in UPLOADERS_DIR, an uploader's name, a path to
    an .sxcu or uploader .json, an Entry, or a dict (see from_dict).  None: the default."""
    if isinstance(uploader, Entry):
        return uploader
    if isinstance(uploader, dict):
        return from_dict(uploader)
    name = uploader or settings.get("upload.uploader")
    if not name:
        raise UploadError("No uploader chosen. Add one in Settings → Uploads.")
    if os.path.isabs(name):
        if not os.path.isfile(name):
            raise UploadError(f"{name} doesn't exist")
        return load_file(name)
    if os.path.isfile(os.path.join(settings.UPLOADERS_DIR, name)):
        return load_file(os.path.join(settings.UPLOADERS_DIR, name))
    for e in entries():
        if name.lower() in (e.name.lower(), os.path.splitext(e.file)[0].lower()):
            return e
    raise UploadError(f"Uploader {name} not found (was it removed?)")


def label(uploader=None):
    """The name to show for an uploader, without failing."""
    try:
        return get(uploader).name
    except UploadError:
        name = uploader if isinstance(uploader, str) and uploader else settings.get("upload.uploader") or ""
        return os.path.splitext(os.path.basename(name))[0] or "uploader"


def _file_name(name, ext):
    base = re.sub(r"[^\w.\- ()]+", "_", name).strip(" ._") or "uploader"
    return base + ext


def _make_default_if_none(file):
    if not settings.get("upload.uploader"):
        settings.set("upload.uploader", file)


def save(plugin_id, name, config, file=None):
    """Write a plugin uploader (a new file unless `file` is given); returns its file name.
    The first uploader becomes the default."""
    os.makedirs(settings.UPLOADERS_DIR, exist_ok=True)
    if file is None:
        file, n = _file_name(name, ".json"), 2
        while os.path.exists(os.path.join(settings.UPLOADERS_DIR, file)):
            file, n = _file_name(f"{name} {n}", ".json"), n + 1
    path = os.path.join(settings.UPLOADERS_DIR, file)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"Plugin": plugin_id, "Name": name, "Config": config}, f, indent=1, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    _make_default_if_none(file)
    return file


def import_sxcu(path):
    """Copy an .sxcu into UPLOADERS_DIR (replacing one with the same name); returns its file name.
    The first uploader becomes the default."""
    import shutil
    cfg = read_sxcu(path)
    load_config(cfg)  # fail early on configs we can't use
    base = os.path.basename(path)
    if not base.lower().endswith(".sxcu"):
        base = os.path.splitext(base)[0] + ".sxcu"
    base = re.sub(r"[^\w.\- ()]+", "_", base)
    os.makedirs(settings.UPLOADERS_DIR, exist_ok=True)
    dest = os.path.join(settings.UPLOADERS_DIR, base)
    if os.path.abspath(path) != os.path.abspath(dest):
        shutil.copyfile(path, dest)
    _make_default_if_none(base)
    return base


def remove(file):
    try:
        os.remove(os.path.join(settings.UPLOADERS_DIR, file))
    except FileNotFoundError:
        pass
    if settings.get("upload.uploader") == file:
        settings.set("upload.uploader", "")


# -- uploading ------------------------------------------------------------------------

def upload(path, uploader=None, progress=None, cancel=None):
    """Upload `path` with an uploader (see get(); None = the default) -> UploadResult.

    Blocking: run it on a worker thread (upload.Uploader does).  progress(sent, total) is
    called from this thread; cancel: a threading.Event that stops the upload (Cancelled)."""
    e = get(uploader)
    if e.error:
        raise UploadError(f"{e.name}: {e.error}")
    why = e.plugin.available()
    if why:
        raise UploadError(f"{e.name}: {why}")
    cancel = cancel if cancel is not None else threading.Event()
    if cancel.is_set():
        raise Cancelled()

    def report(sent, total):
        if progress:
            progress(sent, total)
    try:
        res = e.plugin.upload(path, e.full_config(), report, cancel)
    except UploadError:
        raise
    except Exception as ex:  # noqa: BLE001 - plugin code: report it like any failed upload
        _log(f"uploader {e.name} ({e.plugin_id}) crashed:\n" + traceback.format_exc().rstrip())
        raise UploadError(f"{e.name}: {type(ex).__name__}: {ex}") from ex
    if isinstance(res, str):
        res = UploadResult(res.strip())
    if not isinstance(res, UploadResult) or not res.url:
        raise UploadError(f"{e.name} gave no link")
    return res

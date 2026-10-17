"""Upload files through ShareX custom uploaders (.sxcu JSON files).

An .sxcu describes one HTTP request (method, URL, query, headers, body) and
how to read the result out of the response.  Values may contain ShareX's
syntax: `{json:files[0].url}`, `{regex:pattern|1}`, `{header:Location}`,
`{filename}`, ... (ShareX >= 13).  Older files use `$json:...$` instead;
those are converted on load, the same way ShareX migrates them.

Only the .sxcu format is supported, no built-in hosts: people already have
.sxcu files for the hosts they use (catbox, 0x0, zipline, xbackbone, ...).
"""

import base64
import json
import mimetypes
import os
import random
import re
import shutil
import threading
import xml.etree.ElementTree as ET
from urllib.parse import quote

from PyQt6.QtCore import QObject, Qt, pyqtSignal

from . import settings

USER_AGENT = "screenrec/1.0 (ShareX-compatible custom uploader)"
TIMEOUT = (15, 300)          # connect, read (seconds); videos can take a while
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
BODIES = ("None", "MultipartFormData", "FormURLEncoded", "JSON", "XML", "Binary")


class UploadError(Exception):
    pass


class UntrustedCertificate(UploadError):
    """The server's TLS certificate isn't trusted (self-signed, private CA). `fingerprint` is the
    SHA-256 of the certificate the server presented; trust_certificate() pins it."""

    def __init__(self, host, port, fingerprint):
        self.host, self.port, self.fingerprint = host, port, fingerprint
        where = host if port == 443 else f"{host}:{port}"
        super().__init__(f"{where} has a certificate that isn't trusted (self-signed?)."
                         + (f" SHA-256 fingerprint: {pretty_fingerprint(fingerprint)}" if fingerprint else ""))


# -- trusted (pinned) certificates -------------------------------------------
#
# A trusted server is reached through a connection that accepts exactly the pinned
# certificate (urllib3's assert_fingerprint) instead of checking it against the system
# CAs. Everything else, including redirects to other hosts, is verified normally.

def _trusted():
    t = settings.get("upload.trusted_certs") or {}
    return t if isinstance(t, dict) else {}


def trust_certificate(host, port, fingerprint):
    settings.set("upload.trusted_certs", dict(_trusted(), **{f"{host}:{port}": fingerprint.lower()}))


def untrust_certificate(host, port):
    t = _trusted()
    t.pop(f"{host}:{port}", None)
    settings.set("upload.trusted_certs", t)


def pretty_fingerprint(fp):
    fp = fp.upper()
    return ":".join(fp[i:i + 2] for i in range(0, len(fp), 2))


def server_fingerprint(host, port, timeout=10):
    """SHA-256 of the certificate the server presents (not verified; for showing / pinning)."""
    import hashlib
    import socket
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            return hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()


def _host_port(url):
    from urllib.parse import urlsplit
    u = urlsplit(url)
    return (u.hostname or "").lower(), u.port or (443 if u.scheme == "https" else 80)


def _pinned_session(host, port, fingerprint):
    import requests
    from requests.adapters import HTTPAdapter

    class Pinned(HTTPAdapter):
        """Accepts only the pinned certificate, whatever `verify` says."""
        def build_connection_pool_key_attributes(self, request, verify, cert=None):
            host_params, pool_kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
            pool_kwargs.update(cert_reqs="CERT_NONE", assert_fingerprint=fingerprint)
            for k in ("ca_certs", "ca_cert_dir"):
                pool_kwargs.pop(k, None)
            return host_params, pool_kwargs

        def cert_verify(self, conn, url, verify, cert):
            super().cert_verify(conn, url, False, cert)
            conn.cert_reqs = "CERT_NONE"
            conn.assert_fingerprint = fingerprint

    sess = requests.Session()
    adapter = Pinned()
    sess.mount(f"https://{host}:{port}/", adapter)
    if port == 443:
        sess.mount(f"https://{host}/", adapter)
    return sess


class UploadResult:
    def __init__(self, url, delete_url="", thumbnail_url="", response=""):
        self.url = url
        self.delete_url = delete_url or ""
        self.thumbnail_url = thumbnail_url or ""
        self.response = response

    def __repr__(self):
        return f"UploadResult({self.url!r}, delete_url={self.delete_url!r})"


# -- uploader files ---------------------------------------------------------

def _read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:   # ShareX writes a BOM sometimes
            data = json.load(f)
    except OSError as e:
        raise UploadError(f"Can't read {path}: {e.strerror}") from None
    except ValueError as e:
        raise UploadError(f"{os.path.basename(path)} is not valid JSON: {e}") from None
    if not isinstance(data, dict) or not data.get("RequestURL"):
        raise UploadError(f"{os.path.basename(path)} is not a ShareX custom uploader (no RequestURL)")
    return data


def list_uploaders():
    """[(file name, display name)] of the imported uploaders, sorted by display name."""
    out = []
    try:
        names = os.listdir(settings.UPLOADERS_DIR)
    except OSError:
        return []
    for name in names:
        if not name.lower().endswith((".sxcu", ".json")):
            continue
        try:
            cfg = _read_json(os.path.join(settings.UPLOADERS_DIR, name))
        except UploadError:
            continue
        out.append((name, str(cfg.get("Name") or os.path.splitext(name)[0])))
    return sorted(out, key=lambda t: t[1].lower())


def import_sxcu(path):
    """Copy an .sxcu into UPLOADERS_DIR (replacing one with the same name).

    The first imported uploader becomes the default."""
    cfg = _read_json(path)
    base = os.path.basename(path)
    if not base.lower().endswith(".sxcu"):
        base = os.path.splitext(base)[0] + ".sxcu"
    base = re.sub(r"[^\w.\- ()]+", "_", base)
    os.makedirs(settings.UPLOADERS_DIR, exist_ok=True)
    dest = os.path.join(settings.UPLOADERS_DIR, base)
    if os.path.abspath(path) != os.path.abspath(dest):
        shutil.copyfile(path, dest)
    if not settings.get("upload.uploader"):
        settings.set("upload.uploader", base)
    load_config(cfg)  # fail early on configs we can't use
    return base


def remove_uploader(name):
    try:
        os.remove(os.path.join(settings.UPLOADERS_DIR, name))
    except FileNotFoundError:
        pass
    if settings.get("upload.uploader") == name:
        settings.set("upload.uploader", "")


def uploader_path(name):
    return name if os.path.isabs(name) else os.path.join(settings.UPLOADERS_DIR, name)


# -- config normalization (old ShareX versions) ------------------------------

_OLD_FUNCS = r"(?:json|xml|regex|response|responseurl|header|random|filename|input|prompt|select|base64)"
_OLD_TOKEN = re.compile(r"\$(" + _OLD_FUNCS + r"(?::(?:\\.|[^$\\])*)?)\$", re.I)
_TEMPLATE_KEYS = ("RequestURL", "URL", "ThumbnailURL", "DeletionURL", "ErrorMessage", "Data")


def _version(v):
    try:
        return tuple(int(x) for x in str(v).split(".")[:3])
    except ValueError:
        return (0,)


def _esc(text):
    """Escape literal text for the new syntax."""
    return re.sub(r"([\\{}|])", r"\\\1", text)


def _convert_old(text, regex_list):
    """`$json:a.b$ text $regex:1,2$` -> `{json:a.b} text {regex:<pattern>|2}`."""
    out, pos = [], 0
    for m in _OLD_TOKEN.finditer(text):
        out.append(_esc(text[pos:m.start()].replace("\\$", "$")))
        name, _, arg = m.group(1).partition(":")
        name = name.lower()
        arg = arg.replace("\\$", "$")
        if name == "regex":
            idx, _, group = arg.partition(",")
            try:
                pattern = regex_list[int(idx) - 1]
            except (ValueError, IndexError):
                pattern = ""
            out.append("{regex:%s|%s}" % (_esc(pattern), _esc(group or "0")))
        elif name in ("random", "select", "prompt"):
            out.append("{%s:%s}" % (name, arg))          # args were "|"-separated already
        else:
            out.append("{%s}" % name if not arg else "{%s:%s}" % (name, _esc(arg)))
        pos = m.end()
    out.append(_esc(text[pos:].replace("\\$", "$")))
    return "".join(out)


def load_config(cfg):
    """Return a normalized copy: RequestMethod, Body, dicts present, old syntax converted."""
    cfg = dict(cfg)
    method = str(cfg.get("RequestMethod") or cfg.get("RequestType") or "POST").upper()
    if method not in METHODS:
        raise UploadError(f"Unsupported request method {method}")
    cfg["RequestMethod"] = method
    for key in ("Parameters", "Headers", "Arguments"):
        v = cfg.get(key)
        cfg[key] = {str(k): "" if x is None else str(x) for k, x in v.items()} if isinstance(v, dict) else {}

    body = cfg.get("Body")
    if not body:  # before ShareX 12.3.1 the body type was implied
        if cfg.get("FileFormName"):
            body = "MultipartFormData"
        elif cfg["Arguments"] and method == "GET":
            cfg["Parameters"].update(cfg["Arguments"])
            cfg["Arguments"] = {}
            body = "None"
        elif cfg["Arguments"]:
            body = "FormURLEncoded"
        else:
            body = "None"
    body = {b.lower(): b for b in BODIES}.get(str(body).replace(" ", "").lower())
    if not body:
        raise UploadError(f"Unsupported body type {cfg.get('Body')!r}")
    cfg["Body"] = body

    rtype = str(cfg.get("ResponseType") or "").lower()
    if not cfg.get("URL") and rtype == "redirectionurl":
        cfg["URL"] = "{responseurl}"
    elif not cfg.get("URL") and rtype == "locationheader":
        cfg["URL"] = "{header:Location}"
    cfg["_follow_redirects"] = rtype != "locationheader"

    old = _version(cfg.get("Version") or "0") < (13,)
    regex_list = [str(r) for r in cfg.get("RegexList") or []]

    def needs_convert(s):
        return old and isinstance(s, str) and _OLD_TOKEN.search(s)

    if any(needs_convert(cfg.get(k)) for k in _TEMPLATE_KEYS) or any(
            needs_convert(v) for k in ("Parameters", "Headers", "Arguments") for v in cfg[k].values()):
        for k in _TEMPLATE_KEYS:
            if isinstance(cfg.get(k), str):
                cfg[k] = _convert_old(cfg[k], regex_list)
        for k in ("Parameters", "Headers", "Arguments"):
            cfg[k] = {n: _convert_old(v, regex_list) for n, v in cfg[k].items()}

    dest = str(cfg.get("DestinationType") or "")
    if dest and dest != "None" and not re.search(r"ImageUploader|FileUploader", dest):
        raise UploadError(f"“{cfg.get('Name') or 'This uploader'}” uploads {dest}, not files")
    return cfg


# -- syntax engine ------------------------------------------------------------

_NAME = re.compile(r"([A-Za-z][A-Za-z0-9]*)([:}])")
_FUNCS = {"response", "responseurl", "header", "json", "xml", "regex", "filename", "name",
          "random", "select", "prompt", "inputbox", "base64", "input", "output", "outputbox"}


class Syntax:
    """Evaluates ShareX's `{function:arg|arg}` syntax.

    Arguments may contain nested functions; `\\{ \\} \\| \\\\` are literal.  A `{`
    that doesn't start a known function stays literal (with its `}`), so JSON
    bodies and regex quantifiers work without escaping.  Response functions
    give "" before the response exists.
    """

    def __init__(self, filename="", input="", uploader_name=""):
        self.filename = filename
        self.input = input
        self.uploader_name = uploader_name
        self.response = None   # requests.Response-like: .text, .url, .headers
        self._json = self._xml = None

    def render(self, text, encode=None):
        """encode: None | "url" | "json" | "xml" - applied to function results only."""
        if not text:
            return ""
        out, _, _ = self._seq(str(text), 0, True, encode)
        return out

    def _seq(self, s, i, top, encode=None):
        buf, depth = [], 0   # depth: literal braces inside an argument, e.g. `\d{2}`
        while i < len(s):
            c = s[i]
            if c == "\\" and i + 1 < len(s) and s[i + 1] in "\\{}|":
                buf.append(s[i + 1])
                i += 2
                continue
            if c == "{":
                m = _NAME.match(s, i + 1)
                if m and m.group(1).lower() in _FUNCS:
                    val, i = self._call(s, m)
                    buf.append(self._encode(val, encode) if top else val)
                    continue
                depth += 1
            elif c == "}" and depth:
                depth -= 1
            elif not top and not depth and c in "|}":
                return "".join(buf), i, c
            buf.append(c)
            i += 1
        return "".join(buf), i, None

    def _call(self, s, m):
        name, i, args = m.group(1).lower(), m.end(), []
        if m.group(2) == ":":
            while True:
                arg, i, term = self._seq(s, i, False)
                args.append(arg)
                if term is None:
                    break
                i += 1
                if term == "}":
                    break
        return self._eval(name, args), i

    @staticmethod
    def _encode(val, encode):
        if encode == "url":
            return quote(val, safe="")
        if encode == "json":
            return json.dumps(val)[1:-1]
        if encode == "xml":
            return val.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        return val

    # -- functions
    def _eval(self, name, args):
        r = self.response
        text = r.text if r is not None else ""
        if name == "response":
            return text
        if name == "responseurl":
            return r.url if r is not None else ""
        if name == "header":
            return (r.headers.get(args[0], "") if r is not None and args else "") or ""
        if name in ("json", "xml"):
            if not args:
                return ""
            src, path = (args[0], args[1]) if len(args) > 1 else (None, args[0])
            return self._json_path(src, path) if name == "json" else self._xpath(src, path)
        if name == "regex":
            if not args:
                return ""
            if len(args) >= 3:
                src, pattern, group = args[0], args[1], args[2]
            else:
                src, pattern, group = text, args[0], args[1] if len(args) > 1 else "0"
            return _regex(src, pattern, group)
        if name == "filename":
            return self.filename
        if name == "name":
            return self.uploader_name
        if name == "input":
            return self.input
        if name == "random":
            return random.choice(args) if args else ""
        if name == "select":
            return args[0] if args else ""          # no UI: take the first option
        if name in ("prompt", "inputbox"):
            return args[1] if len(args) > 1 else ""  # no UI: the default value
        if name == "base64":
            return base64.b64encode((args[0] if args else "").encode()).decode()
        return ""                                    # output / outputbox: nothing to show

    def _json_path(self, src, path):
        if src is None:
            if self._json is None:
                try:
                    self._json = json.loads(self.response.text) if self.response is not None else None
                except ValueError:
                    self._json = None
            data = self._json
        else:
            try:
                data = json.loads(src)
            except ValueError:
                return ""
        return _stringify(json_path(data, path))

    def _xpath(self, src, path):
        try:
            if src is None:
                if self._xml is None and self.response is not None:
                    self._xml = ET.fromstring(self.response.text)
                root = self._xml
            else:
                root = ET.fromstring(src)
        except ET.ParseError:
            return ""
        return xpath(root, path) if root is not None else ""


def _stringify(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return str(v)


_PATH_TOKEN = re.compile(r"""\.?([^.\[\]]+)|\[\s*(-?\d+)\s*\]|\[\s*(['"])(.*?)\3\s*\]""")


def json_path(data, path):
    """Minimal JSONPath: `a.b[0].c`, `$.a`, `['odd key']`, `[-1]`. None when missing."""
    path = path.strip()
    if path.startswith("$"):
        path = path[1:]
    pos = 0
    while pos < len(path):
        m = _PATH_TOKEN.match(path, pos)
        if not m:
            return None
        pos = m.end()
        key = m.group(1) if m.group(1) is not None else m.group(4)
        try:
            if m.group(2) is not None:
                data = data[int(m.group(2))]
            elif isinstance(data, list) and key.lstrip("-").isdigit():
                data = data[int(key)]
            else:
                data = data[key]
        except (KeyError, IndexError, TypeError):
            return None
    return data


def xpath(root, path):
    """Text of the first node matching a simple XPath (`/files/file/url`, `//url`, `a/@attr`)."""
    path = path.strip()
    attr = None
    if "/@" in path:
        path, attr = path.rsplit("/@", 1)
    if path.startswith("//"):
        found = root if root.tag == path[2:] else root.find("." + path)
    elif path.startswith("/"):
        first, _, rest = path[1:].partition("/")
        found = (root if not rest else root.find(rest)) if first == root.tag else None
    else:
        found = root.find(path) if path else root
    if found is None:
        return ""
    return found.get(attr, "") if attr else (found.text or "").strip()


def _regex(text, pattern, group):
    try:
        m = re.search(pattern, text)
    except re.error:
        return ""
    if not m:
        return ""
    group = group.strip() or "0"
    try:
        return m.group(int(group) if group.isdigit() else group) or ""
    except IndexError:  # no such group
        return ""


# -- the request --------------------------------------------------------------

def _fix_url(url):
    url = url.strip()
    return url if re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I) else "https://" + url


def _excerpt(text, n=200):
    text = re.sub(r"<[^>]+>", " ", text or "")       # error pages are often HTML
    text = re.sub(r"\s+", " ", text).strip()
    return text[:n] + ("…" if len(text) > n else "")


def resolve(name=None):
    """Load an uploader config by file name / path ("" or None = the default)."""
    name = name or settings.get("upload.uploader")
    if not name:
        raise UploadError("No uploader chosen. Import a ShareX .sxcu file in Settings → Uploads.")
    if isinstance(name, dict):
        return load_config(name)
    path = uploader_path(name)
    if not os.path.exists(path):
        raise UploadError(f"Uploader {name} not found (was it removed?)")
    return load_config(_read_json(path))


def upload(path, uploader=None, session=None):
    """Upload `path` and return an UploadResult (blocking).

    uploader: file name in UPLOADERS_DIR, a path, a config dict, or None for the
    default from settings.  session: something with requests' `request()` (tests)."""
    import requests

    cfg = resolve(uploader)
    filename = os.path.basename(path)
    syn = Syntax(filename=filename, uploader_name=str(cfg.get("Name") or ""))
    method, body = cfg["RequestMethod"], cfg["Body"]

    url = _fix_url(syn.render(cfg["RequestURL"], "url"))
    params = {k: syn.render(v) for k, v in cfg["Parameters"].items()}
    headers = {"User-Agent": USER_AGENT}
    headers.update({k: syn.render(v) for k, v in cfg["Headers"].items()})
    args = {k: syn.render(v) for k, v in cfg["Arguments"].items()}
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    kw = {}
    fh = None
    try:
        if body == "MultipartFormData":
            fh = open(path, "rb")
            kw["data"] = args
            kw["files"] = {cfg.get("FileFormName") or "file": (filename, fh, mime)}
        elif body == "FormURLEncoded":
            kw["data"] = args
        elif body in ("JSON", "XML"):
            kw["data"] = syn.render(cfg.get("Data") or "", "json" if body == "JSON" else "xml").encode()
            if not any(k.lower() == "content-type" for k in headers):
                headers["Content-Type"] = "application/json" if body == "JSON" else "application/xml"
        elif body == "Binary":
            fh = open(path, "rb")
            kw["data"] = fh
            if not any(k.lower() == "content-type" for k in headers):
                headers["Content-Type"] = mime
    except OSError as e:
        raise UploadError(f"Can't read {path}: {e.strerror}") from None

    http = session or requests
    host, port = _host_port(url)
    pinned = _trusted().get(f"{host}:{port}") if url.lower().startswith("https:") else None
    if pinned and session is None:
        http = _pinned_session(host, port, pinned)
    try:
        resp = http.request(method, url, params=params or None, headers=headers, timeout=TIMEOUT,
                            allow_redirects=cfg["_follow_redirects"], **kw)
    except requests.exceptions.SSLError as e:
        if pinned or "CERTIFICATE_VERIFY_FAILED" in str(e) or "fingerprint" in str(e).lower():
            try:
                fp = server_fingerprint(host, port)
            except OSError:
                fp = ""
            if pinned and fp and fp != pinned:
                raise UploadError(f"The certificate of {host} changed since you trusted it. If you expected "
                                  f"that, remove the old exception in Settings → Uploads. New SHA-256: "
                                  f"{pretty_fingerprint(fp)}") from None
            raise UntrustedCertificate(host, port, fp) from None
        raise UploadError(f"Secure connection to {_host(url)} failed: {_reason(e)}") from None
    except requests.exceptions.Timeout:
        raise UploadError(f"{_host(url)} did not answer in time") from None
    except requests.exceptions.ConnectionError as e:
        raise UploadError(f"Can't connect to {_host(url)}: {_reason(e)}") from None
    except requests.exceptions.RequestException as e:
        raise UploadError(f"Upload failed: {e}") from None
    finally:
        if fh:
            fh.close()

    syn.response = resp
    if resp.status_code >= 400:
        msg = syn.render(cfg.get("ErrorMessage") or "").strip() or _excerpt(resp.text)
        raise UploadError(f"HTTP {resp.status_code} {resp.reason or ''}".strip() + (f": {msg}" if msg else ""))

    result = syn.render(cfg.get("URL") or "").strip() if cfg.get("URL") else resp.text.strip()
    if not result:
        msg = syn.render(cfg.get("ErrorMessage") or "").strip() or _excerpt(resp.text) or "empty response"
        raise UploadError(f"No URL in the response: {msg}")
    return UploadResult(result, syn.render(cfg.get("DeletionURL") or "").strip(),
                        syn.render(cfg.get("ThumbnailURL") or "").strip(), resp.text)


def _host(url):
    m = re.match(r"^[a-z]+://([^/?#]+)", url, re.I)
    return m.group(1) if m else url


def _reason(e):
    s = str(e)
    for hint in ("Name or service not known", "Temporary failure in name resolution",
                 "Connection refused", "Network is unreachable", "No route to host",
                 "CERTIFICATE_VERIFY_FAILED", "timed out"):
        if hint in s:
            return hint
    return s[:160]


# -- Qt wrapper -----------------------------------------------------------------

class Uploader(QObject):
    """Runs upload() in a thread; `finished` arrives on the GUI thread."""

    finished = pyqtSignal(object, str)   # (UploadResult | None, error text)
    _done = pyqtSignal(object, str)
    _running = set()                     # keep instances alive while they work

    def __init__(self, parent=None):
        super().__init__(parent)
        self._done.connect(self._finish, Qt.ConnectionType.QueuedConnection)

    def start(self, path, uploader=None):
        Uploader._running.add(self)
        threading.Thread(target=self._work, args=(path, uploader), daemon=True).start()

    def _work(self, path, uploader):
        self.error = None
        try:
            res, err = upload(path, uploader), ""
        except UploadError as e:
            res, err = None, str(e)
            self.error = e       # e.g. UntrustedCertificate: the caller can offer to trust it
        except Exception as e:  # noqa: BLE001 - never leave the caller waiting
            res, err = None, f"Upload failed: {e}"
        self._done.emit(res, err)

    def _finish(self, res, err):
        Uploader._running.discard(self)
        self.finished.emit(res, err)

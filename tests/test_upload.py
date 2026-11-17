import email.parser
import email.policy
import http.server
import json
import threading

import pytest

from snappie import upload


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        if self.path == "/redirect":
            self.send_response(307)
            self.send_header("Location", "/up")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.server.got.append((self.headers, body))
        out = json.dumps({"url": "https://example.test/f", "len": n}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_PUT = do_POST

    def log_message(self, *a):
        pass

    def handle(self):
        try:
            super().handle()
        except ConnectionError:   # the client gave up (cancel test)
            pass


@pytest.fixture
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.got = []
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def cfg(server, body, path="/up", **kw):
    c = {"Version": "15.0.0", "Name": "test", "RequestMethod": "POST",
         "RequestURL": f"http://127.0.0.1:{server.server_port}{path}", "Body": body, "URL": "{json:url}"}
    c.update(kw)
    return c


@pytest.fixture
def blob(tmp_path):
    p = tmp_path / 'shot "1".png'
    p.write_bytes(bytes(range(256)) * 400)   # 100 KiB: several chunks
    return p


def parts(headers, body):
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + headers["Content-Type"].encode() + b"\r\n\r\n" + body)
    return {p.get_param("name", header="content-disposition"): p for p in msg.iter_parts()}


def test_multipart_streams_file_and_fields(server, blob):
    seen = []
    res = upload.upload(str(blob), cfg(server, "MultipartFormData", FileFormName="img",
                                       Arguments={"key": "abc", "note": "ü"}),
                        progress=lambda s, t: seen.append((s, t)))
    assert res.url == "https://example.test/f"
    headers, body = server.got[0]
    assert "chunked" not in (headers.get("Transfer-Encoding") or "")
    p = parts(headers, body)
    assert p["img"].get_payload(decode=True) == blob.read_bytes()
    assert p["img"].get_content_type() == "image/png"
    assert p["img"].get_filename() == 'shot %221%22.png'
    assert p["key"].get_payload(decode=True) == b"abc"
    assert p["note"].get_payload(decode=True).decode() == "ü"
    assert seen[-1] == (len(body), len(body))
    assert [s for s, _ in seen] == sorted(s for s, _ in seen)


def test_binary_body(server, blob):
    seen = []
    upload.upload(str(blob), cfg(server, "Binary", RequestMethod="PUT"), progress=lambda s, t: seen.append(s))
    headers, body = server.got[0]
    assert body == blob.read_bytes()
    assert headers["Content-Type"] == "image/png"
    assert seen[-1] == len(body)


def test_redirect_resends_the_body(server, blob):
    upload.upload(str(blob), cfg(server, "MultipartFormData", path="/redirect"))
    headers, body = server.got[0]
    assert parts(headers, body)["file"].get_payload(decode=True) == blob.read_bytes()


def test_cancel_stops_sending(server, blob):
    ev = threading.Event()
    ev.set()
    with pytest.raises(upload.Cancelled):
        upload.upload(str(blob), cfg(server, "MultipartFormData"), cancel=ev)
    # the server may see the headers of the cut-off request, never a whole body
    assert all(len(body) < int(headers["Content-Length"]) for headers, body in server.got)


def test_cancel_midway(blob):
    ev = threading.Event()

    def progress(sent, total):
        if sent > 1000:
            ev.set()
    b = upload.StreamBody([b"head", str(blob)], progress, ev)
    b.read(2000)
    with pytest.raises(upload.Cancelled):
        b.read(2000)
    b.close()


def test_stream_body_reads_across_parts_and_rewinds(blob):
    data = blob.read_bytes()
    b = upload.StreamBody([b"ab", str(blob), b"yz"])
    assert len(b) == len(data) + 4
    chunks = []
    while True:
        c = b.read(7777)
        if not c:
            break
        chunks.append(c)
    assert b"".join(chunks) == b"ab" + data + b"yz"
    assert b.tell() == len(b)
    b.seek(0)
    assert b.read() == b"ab" + data + b"yz"
    b.close()


def test_file_shrinking_while_sending_is_an_error(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"x" * 100)
    b = upload.StreamBody([str(p)])
    p.write_bytes(b"x" * 10)
    with pytest.raises(upload.UploadError, match="changed"):
        b.read()
    b.close()

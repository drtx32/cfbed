import json
import urllib.error
from pathlib import Path
import pytest
from cfbed.core import Client, CfbedError, classify_response, download_to_temp, encoded_url, encrypt_token, decrypt_token, USER_AGENT

def test_encoded_url_only_encodes_path():
    assert encoded_url("https://x.test/a folder/猫.png?q=a b") == "https://x.test/a%20folder/%E7%8C%AB.png?q=a b"

def test_token_is_encrypted():
    value = encrypt_token("secret-token")
    assert "secret-token" not in value
    assert decrypt_token(value) == "secret-token"

def test_upload_contract(tmp_path):
    file = tmp_path / "你好 file.html"; file.write_text("<h1>ok</h1>")
    class Response:
        status=200; headers={}
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def read(self): return b'[{"src":"/file/prefix_%E4%BD%A0.html","publicUrl":"https://cdn.test/prefix_%E4%BD%A0.html"}]'
    seen={}
    def opener(req): seen.update(url=req.full_url, body=req.data); return Response()
    result=Client("https://api.test", "token", opener).upload(file, directory="static-sites", name_type="origin")
    assert result["public_url"] == "https://cdn.test/prefix_%E4%BD%A0.html"
    assert result["encoded_url"] == "https://cdn.test/prefix_%E4%BD%A0.html"
    assert "uploadNameType=origin" in seen["url"]
    assert "你好 file.html".encode() in seen["body"] and "uploadFolder=static-sites" in seen["url"]


def test_upload_full_src_is_not_prefixed_twice(tmp_path):
    file = tmp_path / "report.md"
    file.write_text("report")

    class Response:
        status = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'[{"src":"https://cdn.test/report.md"}]'

    result = Client("https://api.test", None, lambda request: Response()).upload(file)
    assert result["public_url"] == "https://cdn.test/report.md"

def test_mcp_image_shape():
    class Response:
        status=200; headers={"Content-Type":"image/webp"}
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def read(self): return b"webp-bytes"
    status, headers, body=Client("https://api.test", None, lambda req: Response()).download("x/a b.webp")
    assert status == 200 and headers["Content-Type"] == "image/webp" and body == b"webp-bytes"


def test_requests_include_project_user_agent():
    class Response:
        status = 200; headers = {}
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b"{}"

    seen = {}
    def opener(req):
        seen.update(req.headers)
        return Response()

    Client("https://api.test", None, opener).request("GET", "/api/manage/list")
    assert seen["User-agent"] == USER_AGENT


def test_binary_http_error_never_decodes_body():
    error = urllib.error.HTTPError("https://api.test/file/missing.jpg", 404, "Not Found", {"Content-Type": "image/jpeg", "Content-Length": "4"}, None)
    error.read = lambda: b"\xff\xd8JFIF"
    with pytest.raises(CfbedError, match=r"API error 404: binary response \(image/jpeg, 6 bytes\)"):
        Client("https://api.test", None, lambda req: (_ for _ in ()).throw(error)).download("missing.jpg")


def test_text_http_error_is_readable():
    error = urllib.error.HTTPError("https://api.test/file/missing", 404, "Not Found", {"Content-Type": "application/json"}, None)
    error.read = lambda: b'{"error":"missing"}'
    with pytest.raises(CfbedError, match=r'API error 404: \{"error":"missing"\}'):
        Client("https://api.test", None, lambda req: (_ for _ in ()).throw(error)).download("missing")


@pytest.mark.parametrize(("mime", "kind"), [
    ("image/jpeg", "binary"), ("video/mp4", "binary"), ("application/octet-stream", "binary"),
    ("text/markdown; charset=utf-8", "text"), ("application/json", "text"), ("application/problem+json", "text"),
])
def test_response_classification(mime, kind):
    assert classify_response(200, {"Content-Type": mime}) == (kind, mime.split(";", 1)[0])


def test_download_to_temp_streams_and_uses_content_disposition(tmp_path):
    class Response:
        status = 200
        headers = {"Content-Type": "text/html; charset=utf-8", "Content-Disposition": "attachment; filename*=UTF-8''%E4%B8%AD%E6%96%87.html"}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size=-1):
            return b"<h1>" if size else b""

    # A finite response also proves the implementation does not call read() once
    # with an unbounded size.
    chunks = iter([b"<h1>", "中文".encode(), b"</h1>", b""])
    Response.read = lambda self, size=-1: next(chunks)
    import cfbed.core as core
    original = core.urllib.request.urlopen
    core.urllib.request.urlopen = lambda request: Response()
    try:
        path, name, mime = download_to_temp("https://example.test/path/original",)
    finally:
        core.urllib.request.urlopen = original
    try:
        assert name == "中文.html"
        assert mime == "text/html"
        assert path.read_bytes() == "<h1>中文</h1>".encode()
    finally:
        path.unlink(missing_ok=True)


def test_download_to_temp_http_failure_is_concise():
    import cfbed.core as core
    error = urllib.error.HTTPError("https://example.test/missing", 404, "Not Found", {}, None)
    original = core.urllib.request.urlopen
    core.urllib.request.urlopen = lambda request: (_ for _ in ()).throw(error)
    try:
        with pytest.raises(CfbedError, match="download failed: HTTP 404"):
            download_to_temp("https://example.test/missing")
    finally:
        core.urllib.request.urlopen = original

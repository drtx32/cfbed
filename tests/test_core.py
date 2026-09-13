import json
from pathlib import Path
from cfbed.core import Client, encoded_url, encrypt_token, decrypt_token, USER_AGENT

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

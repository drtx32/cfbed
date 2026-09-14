from __future__ import annotations

import base64
import json
import mimetypes
import os
import secrets
import stat
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from tempfile import NamedTemporaryFile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from . import __version__

CONFIG_DIR = Path(os.environ.get("CFBED_CONFIG_DIR", Path.home() / ".cache" / "cfbed"))
CONFIG_FILE = CONFIG_DIR / "config.json"
KEY_FILE = CONFIG_DIR / "secret.key"
USER_AGENT = f"cfbed/{__version__} (+https://github.com/drtx32/cfbed)"
DOWNLOAD_CHUNK_SIZE = 1024 * 1024


class CfbedError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


TEXTUAL_MIME_TYPES = {
    "application/json", "application/ld+json", "application/xml", "application/xhtml+xml",
    "application/javascript", "application/x-javascript", "application/sql",
    "application/markdown", "application/x-markdown", "application/yaml", "application/rtf",
}


def classify_response(status: int, headers) -> tuple[str, str]:
    """Return (text|binary, normalized MIME type) without inspecting the body."""
    mime = (headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0].strip().lower()
    textual = mime.startswith("text/") or mime in TEXTUAL_MIME_TYPES or mime.endswith("+json") or mime.endswith("+xml")
    return ("text" if textual else "binary"), mime


def _api_error(status: int, headers, body: bytes) -> CfbedError:
    kind, mime = classify_response(status, headers)
    if kind == "text":
        detail = body.decode("utf-8", "replace").strip()[:500]
        return CfbedError(f"API error {status}: {detail or 'empty response'}", 2)
    return CfbedError(f"API error {status}: binary response ({mime}, {len(body)} bytes)", 2)


def _secure_dir() -> None:
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)


def _read_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        value = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise CfbedError(f"cannot read config: {exc}")


def _write_config(config: dict) -> None:
    _secure_dir()
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_FILE)


def _key() -> bytes:
    _secure_dir()
    if KEY_FILE.exists():
        key = KEY_FILE.read_bytes()
    else:
        key = secrets.token_bytes(32)
        KEY_FILE.write_bytes(key)
        os.chmod(KEY_FILE, 0o600)
    if len(key) != 32:
        raise CfbedError("invalid local encryption key")
    return key


def encrypt_token(token: str) -> str:
    nonce = secrets.token_bytes(12)
    encrypted = AESGCM(_key()).encrypt(nonce, token.encode(), None)
    return base64.urlsafe_b64encode(nonce + encrypted).decode()


def decrypt_token(value: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(value.encode())
        return AESGCM(_key()).decrypt(raw[:12], raw[12:], None).decode()
    except Exception as exc:
        raise CfbedError(f"cannot decrypt token: {exc}")


def set_base_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise CfbedError("base URL must be an http(s) URL")
    config = _read_config()
    config["base_url"] = url.rstrip("/")
    _write_config(config)


def set_token(token: str) -> None:
    if not token.strip():
        raise CfbedError("token cannot be empty")
    config = _read_config()
    config["token_encrypted"] = encrypt_token(token.strip())
    _write_config(config)


def clear_token() -> None:
    config = _read_config()
    config.pop("token_encrypted", None)
    _write_config(config)


def config_public() -> dict:
    config = _read_config()
    return {"base_url": config.get("base_url"), "token_set": bool(config.get("token_encrypted"))}


def credentials() -> tuple[str, str | None]:
    config = _read_config()
    url = os.environ.get("CFBED_BASE_URL") or config.get("base_url")
    token = os.environ.get("CFBED_API_TOKEN")
    if token is None and config.get("token_encrypted"):
        token = decrypt_token(config["token_encrypted"])
    if not url:
        raise CfbedError("base URL is not configured; run `cfbed config set-base-url URL`")
    return url.rstrip("/"), token


def encoded_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    path = "/".join(urllib.parse.quote(urllib.parse.unquote(part), safe="") for part in parts.path.split("/"))
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _safe_download_name(value: str | None) -> str | None:
    """Return a usable basename, rejecting path-like or control-heavy values."""
    if not value:
        return None
    value = value.strip().replace("\\", "/")
    name = urllib.parse.unquote(value).rsplit("/", 1)[-1]
    if not name or name in {".", ".."} or any(ord(char) < 32 for char in name):
        return None
    return name[:255]


def _remote_filename(url: str, headers) -> str:
    content_disposition = headers.get("Content-Disposition")
    if content_disposition:
        # Message.get_filename handles quoted names and RFC 2231 filename*.
        from email.message import Message

        message = Message()
        message["content-disposition"] = content_disposition
        name = _safe_download_name(message.get_filename())
        if name:
            return name
    name = _safe_download_name(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
    return name or "download"


def download_to_temp(url: str) -> tuple[Path, str, str | None]:
    """Stream a remote source to a temporary file and return path/name/MIME."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise CfbedError("source URL must be an http(s) URL", 1)
    request = urllib.request.Request(url, headers={"Accept": "*/*", "User-Agent": USER_AGENT})
    try:
        response = urllib.request.urlopen(request)
        with response:
            if getattr(response, "status", 200) >= 400:
                raise CfbedError(f"download failed: HTTP {response.status}", 2)
            headers = response.headers
            name = _remote_filename(url, headers)
            content_type = (headers.get("Content-Type") or "").split(";", 1)[0].strip() or None
            suffix = Path(name).suffix
            with NamedTemporaryFile(prefix="cfbed-upload-", suffix=suffix, delete=False) as target:
                temp_path = Path(target.name)
                try:
                    while True:
                        chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                        if not chunk:
                            break
                        target.write(chunk)
                except Exception:
                    temp_path.unlink(missing_ok=True)
                    raise
            return temp_path, name, content_type
    except CfbedError:
        raise
    except urllib.error.HTTPError as exc:
        raise CfbedError(f"download failed: HTTP {exc.code}", 2)
    except urllib.error.URLError as exc:
        raise CfbedError(f"download failed: {exc.reason}", 2)
    except OSError as exc:
        raise CfbedError(f"download failed: {exc}", 2)


class Client:
    def __init__(self, base_url: str, token: str | None, opener=urllib.request.urlopen):
        self.base_url, self.token, self.opener = base_url.rstrip("/"), token, opener

    def request(self, method: str, path: str, data: bytes | None = None, headers=None, query=None):
        query = query or {}
        url = self.base_url + (path if path.startswith("/") else "/" + path)
        query_string = urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        if query_string:
            url += "?" + query_string
        req_headers = {"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})}
        if self.token:
            req_headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        try:
            with self.opener(request) as response:
                body = response.read()
                if response.status >= 400:
                    raise _api_error(response.status, response.headers, body)
                return response.status, dict(response.headers), body
        except urllib.error.HTTPError as exc:
            body = exc.read()
            raise _api_error(exc.code, exc.headers or {}, body)
        except urllib.error.URLError as exc:
            raise CfbedError(f"network error: {exc.reason}", 2)

    def upload(self, file: Path, directory=None, filename=None, name_type=None, channel=None, return_format="full", content_type=None, source_file=None):
        boundary = "----cfbed-" + secrets.token_hex(12)
        content_type = content_type or mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        name = filename or file.name
        payload = file.read_bytes()
        def field(key, value):
            return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n").encode()
        body = b"--" + boundary.encode() + f"\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\nContent-Type: {content_type}\r\n\r\n".encode() + payload + b"\r\n--" + boundary.encode() + b"--\r\n"
        _, _, raw = self.request("POST", "/upload", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}, {"returnFormat": return_format, "uploadChannel": channel, "uploadNameType": name_type, "uploadFolder": directory})
        result = json.loads(raw)
        item = result[0] if isinstance(result, list) and result else result
        public = item.get("publicUrl") or (self.base_url + item.get("src", ""))
        return {"source_file": source_file or str(file), "directory": directory, "stored_name": item.get("src"), "public_url": public, "encoded_url": encoded_url(public), "content_type": content_type, "size": len(payload)}

    def download(self, path: str):
        safe_path = "/".join(urllib.parse.quote(urllib.parse.unquote(p), safe="") for p in path.lstrip("/").split("/"))
        status, headers, body = self.request("GET", "/file/" + safe_path, headers={"Accept": "*/*"})
        return status, headers, body

    def list(self, path=None):
        _, _, raw = self.request("GET", "/api/manage/list", query={"dir": path} if path else {})
        return json.loads(raw)

    def delete(self, path):
        _, _, raw = self.request("GET", "/api/manage/delete/" + urllib.parse.quote(path, safe="/"))
        try: return json.loads(raw)
        except json.JSONDecodeError: return {"success": True}

    def move(self, src, dst):
        _, _, raw = self.request("POST", "/api/manage/move", json.dumps({"source": src, "target": dst}).encode(), {"Content-Type": "application/json"})
        return json.loads(raw)

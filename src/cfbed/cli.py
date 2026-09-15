"""Typer/Rich command-line interface for cfbed.

The CLI is defined with Typer so that ``--help`` produces a Rich-formatted
Usage/Options/Commands screen, but the public entry point ``main(argv)``
remains a list-accepting callable so unit tests can drive it without
invoking the Click runner. Typer's underlying Click machinery is used to
parse argv; the resulting command handler returns the integer exit code
that ``main`` propagates.

Behavior preserved from the argparse implementation:
* ``auth set-token`` prompts on a TTY (``getpass``), reads stdin when
  non-interactive, accepts a single positional or ``--token`` argument,
  and rejects ambiguous combinations.
* ``get`` defaults to a binary-safe file when the response is non-text
  and no ``--output`` is given, refuses to write binary to a TTY on
  ``--stdout``, and emits typed MCP Image / Text / Resource blocks in
  ``--format mcp`` mode.
* ``Client`` requests carry the explicit ``cfbed/<version>`` User-Agent.
* All existing JSON / MCP output contracts and exit codes are kept.
"""

from __future__ import annotations

import base64
import getpass
import json
import mimetypes
import sys
import tempfile
import urllib.parse
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.table import Table
from typer._click.exceptions import (
    ClickException,
    NoArgsIsHelpError,
    UsageError,
)

from . import __version__
from .core import (
    CfbedError,
    Client,
    classify_response,
    clear_token,
    config_public,
    credentials,
    encoded_url,
    set_base_url,
    set_token,
    WebDavClient,
    download_to_temp,
)


app = typer.Typer(
    name="cfbed",
    help="CloudFlare ImgBed 文件管理 CLI；支持人类可读、JSON 和 MCP 输出。",
    no_args_is_help=True,
    rich_markup_mode=None,
    pretty_exceptions_show_locals=False,
    add_completion=False,
)
config_app = typer.Typer(help="本地 endpoint 与配置管理。", no_args_is_help=True)
auth_app = typer.Typer(help="API token 管理（token 只在本地加密保存）。", no_args_is_help=True)
webdav_app = typer.Typer(help="[WebDAV] 文件管理与能力探测。", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(auth_app, name="auth")
app.add_typer(webdav_app, name="webdav")


def emit(value, fmt: str = "human") -> None:
    """Print a result without changing the machine-readable contract."""
    if fmt == "json":
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))


def _entry_value(entry: dict, *keys):
    for key in keys:
        value = entry.get(key)
        if value is not None and value != "":
            return value
    metadata = entry.get("metadata")
    if isinstance(metadata, dict):
        for key in keys:
            value = metadata.get(key)
            if value is not None and value != "":
                return value
    return None


def _list_entries(value) -> list[dict]:
    """Flatten the API's directory/file response for human rendering only."""
    if isinstance(value, dict) and ("directories" in value or "files" in value):
        entries = [{"name": item, "_directory": True} for item in value.get("directories", []) if isinstance(item, str)]
        files = value.get("files", [])
        if isinstance(files, list):
            entries.extend(item if isinstance(item, dict) else {"name": str(item)} for item in files)
        return entries
    if isinstance(value, list):
        return [entry if isinstance(entry, dict) else {"name": str(entry)} for entry in value]
    if isinstance(value, dict):
        for key in ("entries", "items", "data", "result"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [entry if isinstance(entry, dict) else {"name": str(entry)} for entry in nested]
        return [value]
    return [{"name": str(value)}]


def _format_size(value) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return str(value) if value not in (None, "") else "—"
    if size < 1024:
        return f"{int(size)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
    return str(value)


def _format_uploaded(value) -> str:
    if value in (None, ""):
        return "—"
    try:
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return str(value)


def _render_list_human(value, path: Optional[str] = None) -> None:
    """Render list results as a human-readable table, never as JSON."""
    entries = _list_entries(value)
    requested_path = "/" + (path or "").strip("/")
    if requested_path != "/":
        requested_path += "/"
    Console(file=sys.stdout, force_terminal=False, color_system=None).print(f"Path: {requested_path}")
    table = Table("Type", "Name", "Size", "Uploaded", show_header=True, header_style="bold cyan", expand=False)
    for entry in entries:
        is_directory = entry.get("_directory") is True or _entry_value(entry, "is_dir", "isDir", "directory", "folder") is True
        entry_type = _entry_value(entry, "type", "kind", "FileType")
        is_directory = is_directory or str(entry_type).lower() in {"dir", "directory", "folder"}
        name = str(_entry_value(entry, "name", "Name", "path", "Path", "key", "src") or "")
        name = name.rstrip("/").rsplit("/", 1)[-1]
        name = ("📁 " if is_directory else "📄 ") + name + ("/" if is_directory else "")
        file_type = "Directory" if is_directory else str(entry_type or _entry_value(entry, "mime", "mime_type", "content_type", "contentType") or "—")
        size = "—" if is_directory else _format_size(_entry_value(entry, "size", "Size", "bytes", "FileSizeBytes", "content_length", "contentLength"))
        uploaded = "—" if is_directory else _format_uploaded(_entry_value(entry, "uploaded", "Uploaded", "timestamp", "TimeStamp", "created_at", "createdAt"))
        table.add_row(file_type, name, size, uploaded)
    Console(file=sys.stdout, force_terminal=False, color_system=None).print(table)


def _format_option(help_text: str = "输出格式：human（终端）、json（稳定 JSON）或 mcp（MCP envelope）。") -> str:
    """Helper to keep --format declarations uniform; returns the help text."""
    return help_text


def _run(action, fmt: str = "human") -> None:
    """Run ``action`` and translate ``CfbedError`` into a CLI-friendly error.

    The actual exit-code translation is done in :func:`main`; this helper
    just renders the error to stderr in the chosen format.
    """
    try:
        action()
    except (CfbedError, OSError, ValueError) as exc:
        if fmt == "json":
            print(json.dumps({"error": str(exc), "code": getattr(exc, "code", 1)}), file=sys.stderr)
        else:
            print(f"cfbed: {exc}", file=sys.stderr)
        setattr(exc, "_cfbed_rendered", True)
        raise


def _client() -> tuple[str, Optional[str], Client]:
    base, token = credentials()
    return base, token, Client(base, token)


@app.callback()
def root(
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        callback=lambda value: _version(value),
        is_eager=True,
        help="显示 cfbed 版本并退出。",
    ),
) -> None:
    """CloudFlare ImgBed 文件管理 CLI；提供 secure local credentials、JSON 和 MCP 输出。"""


def _version(value: Optional[bool]) -> Optional[bool]:
    if value:
        typer.echo(__version__)
        raise typer.Exit()
    return value


@config_app.command("set-base-url")
def config_set_base_url(
    url: str = typer.Argument(..., help="ImgBed endpoint，例如 https://imgbed.example。"),
) -> None:
    """设置本地 API endpoint（仅保存 URL，不保存 token）。"""
    _run(lambda: (set_base_url(url), emit({"base_url": url.rstrip("/")}, "json")))


@config_app.command("show")
def config_show() -> None:
    """显示当前 endpoint 和 token 是否已设置；绝不打印 token。"""
    _run(lambda: emit(config_public(), "json"))


@auth_app.command("set-token")
def auth_set_token(
    ctx: typer.Context,
    token_value: Optional[str] = typer.Argument(
        None,
        metavar="[token]",
        help="[deprecated] 直接传入 token；为兼容旧用法保留；建议改用 stdin 或隐藏提示。",
    ),
    token_option: Optional[str] = typer.Option(
        None,
        "--token",
        help="[deprecated] 通过 --token 传入；会进入 shell history；建议改用 stdin。",
    ),
) -> None:
    """安全设置 API token：TTY 使用隐藏提示，非 TTY 从 stdin 读取。"""
    def action() -> None:
        if token_value is not None and token_option is not None:
            raise CfbedError("specify the token once, either as an argument or with --token")
        if token_value is not None:
            print("cfbed: warning: positional token is unsafe; use stdin instead", file=sys.stderr)
        token = token_value or token_option
        if token is None:
            token = getpass.getpass("Token: ") if sys.stdin.isatty() else sys.stdin.read().strip()
        set_token(token)
        emit({"token_set": True}, "json")
    _run(action)
    # Keep ctx referenced so Typer doesn't optimize the parameter away.
    del ctx


@auth_app.command("clear")
def auth_clear() -> None:
    """删除本地保存的 API token。"""
    _run(lambda: (clear_token(), emit({"token_set": False}, "json")))


def _format_callback(value: str) -> str:
    if value not in ("human", "json", "mcp"):
        raise typer.BadParameter("must be one of: human, json, mcp", param_hint="--format")
    return value


def _format_callback_hj(value: str) -> str:
    if value not in ("human", "json"):
        raise typer.BadParameter("must be one of: human, json", param_hint="--format")
    return value


@app.command("upload")
def upload(
    file: str = typer.Argument(..., help="本地文件路径、http(s) URL，或 --content 模式下的目标文件名。"),
    content: Optional[str] = typer.Option(None, "--content", help="直接上传文本内容；使用 - 从 stdin 读取 UTF-8 文本。"),
    directory: Optional[str] = typer.Option(None, help="远端目录；ImgBed 的 uploadFolder。"),
    filename: Optional[str] = typer.Option(None, help="远端文件名；默认使用本地文件名。"),
    name_type: Optional[str] = typer.Option(
        None,
        "--name-type",
        case_sensitive=False,
        help="命名策略：default、origin、index 或 short。",
    ),
    channel: Optional[str] = typer.Option(None, help="上传渠道；ImgBed 的 uploadChannel。"),
    output_format: str = typer.Option(
        "human",
        "--format",
        callback=_format_callback,
        help=_format_option(),
    ),
) -> None:
    """[REST] 上传本地文件、远程 URL，或直接上传 UTF-8 文本内容。"""
    if name_type not in (None, "default", "origin", "index", "short"):
        raise typer.BadParameter("must be one of: default, origin, index, short", param_hint="--name-type")
    fmt = output_format

    def action() -> None:
        temp_path: Optional[Path] = None
        source_name = file
        content_type = None
        try:
            if content is not None:
                text = sys.stdin.read() if content == "-" else content
                with tempfile.NamedTemporaryFile(prefix="cfbed-content-", suffix=Path(file).suffix, delete=False) as target:
                    temp_path = Path(target.name)
                    target.write(text.encode("utf-8"))
                upload_path = temp_path
                content_type = mimetypes.guess_type(file)[0]
            elif urllib.parse.urlsplit(file).scheme in ("http", "https"):
                temp_path, inferred_name, content_type = download_to_temp(file)
                source_name = inferred_name
                upload_path = temp_path
            else:
                upload_path = Path(file)
                if not upload_path.is_file():
                    raise CfbedError(f"local file not found: {file}", 1)
            result = _client()[2].upload(
                upload_path, directory, filename or (source_name if content is not None or temp_path else None),
                name_type, channel, content_type=content_type, source_file=source_name,
            )
            if fmt == "mcp":
                emit({"result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}}, "json")
            else:
                emit(result, fmt)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    _run(action, fmt)


@app.command("list")
def list_files(
    path: Optional[str] = typer.Argument(None, help="远端目录路径；可省略以列出根目录。"),
    output_format: str = typer.Option(
        "human", "--format", callback=_format_callback, help=_format_option()
    ),
) -> None:
    """[REST] 列出远端目录；path 接受 ImgBed 目录路径或 slash 形式。"""
    fmt = output_format
    def action() -> None:
        result = _client()[2].list(path)
        if fmt == "human":
            _render_list_human(result, path)
        else:
            emit(result, fmt)
    _run(action, fmt)


@app.command("info")
def info(
    path: str = typer.Argument(..., help="远端文件 path 或 file/id 形式。"),
    output_format: str = typer.Option(
        "human", "--format", callback=_format_callback, help=_format_option()
    ),
) -> None:
    """[REST] 查看远端文件 metadata（HTTP status、MIME、大小和 headers）。"""
    def action() -> None:
        base, _, client = _client()
        status, headers, _ = client.request("HEAD", "/file/" + path.lstrip("/"))
        emit(
            {
                "status": status,
                "content_type": headers.get("Content-Type"),
                "size": headers.get("Content-Length"),
                "headers": headers,
            },
            fmt,
        )
    fmt = output_format
    _run(action, fmt)


@app.command("url")
def url_cmd(
    path: str = typer.Argument(..., help="远端 path 或完整 public URL。"),
    encoded: bool = typer.Option(False, help="逐段 URL-encode path，保留 scheme、host、query。"),
    output_format: str = typer.Option(
        "human", "--format", callback=_format_callback, help=_format_option()
    ),
) -> None:
    """[REST] 解析远端文件的 public URL，可选返回安全编码后的 URL。"""
    def action() -> None:
        base, _, _ = _client()
        value = (
            path
            if path.startswith("http")
            else base + "/file/" + "/".join(urllib.parse.quote(p, safe="") for p in path.strip("/").split("/"))
        )
        emit(encoded_url(value) if encoded else value, fmt)
    fmt = output_format
    _run(action, fmt)


@app.command("get")
def get(
    path: str = typer.Argument(..., help="远端文件 path；按 path/id 形式读取内容。"),
    output: Optional[Path] = typer.Option(None, "--output", help="将原始 bytes 写入此本地文件。"),
    stdout: bool = typer.Option(False, "--stdout", help="明确将原始 bytes 写到 stdout。"),
    output_format: str = typer.Option(
        "human", "--format", callback=_format_callback, help=_format_option()
    ),
) -> None:
    """[REST] 读取或下载远端内容；二进制默认保存到文件，文本按 UTF-8 输出。"""
    def action() -> None:
        base, _, client = _client()
        _, headers, body = client.download(path)
        kind, mime = classify_response(200, headers)
        disposition = headers.get("Content-Disposition", "")
        filename = None
        if disposition:
            msg = Message()
            msg["Content-Disposition"] = disposition
            filename = msg.get_filename()
        filename = (
            filename
            or Path(urllib.parse.unquote(path.rstrip("/")).split("/")[-1]).name
            or "download.bin"
        )
        if output_format == "mcp":
            if mime.startswith("image/"):
                emit(
                    {
                        "result": {
                            "content": [
                                {"type": "image", "mimeType": mime, "data": base64.b64encode(body).decode()}
                            ]
                        }
                    },
                    "json",
                )
            elif kind == "text":
                emit(
                    {
                        "result": {
                            "content": [
                                {"type": "text", "text": body.decode("utf-8", "replace")}
                            ]
                        }
                    },
                    "json",
                )
            else:
                emit(
                    {
                        "result": {
                            "content": [
                                {
                                    "type": "resource",
                                    "resource": {
                                        "uri": base + "/file/" + path,
                                        "mimeType": mime,
                                        "size": len(body),
                                    },
                                }
                            ]
                        }
                    },
                    "json",
                )
        elif output:
            output.write_bytes(body)
            emit(
                {"output": str(output), "content_type": mime, "size": len(body)},
                output_format,
            )
        elif stdout:
            if sys.stdout.isatty():
                raise CfbedError("refusing to write binary data to a TTY; use --output FILE")
            sys.stdout.buffer.write(body)
        elif kind == "text":
            sys.stdout.write(body.decode("utf-8", "replace"))
        else:
            target = Path(filename)
            target.write_bytes(body)
            print(f"saved {len(body)} bytes ({mime}) to {target}")
    fmt = output_format
    _run(action, fmt)


@app.command("mkdir")
def mkdir(
    path: str = typer.Argument(..., help="要创建的远端目录路径。"),
    output_format: str = typer.Option("human", "--format", callback=_format_callback_hj, help="输出格式：human 或 json。"),
) -> None:
    """[WebDAV] 创建远端目录（MKCOL）。"""
    fmt = output_format
    _run(lambda: emit({"transport": "WebDAV MKCOL", "path": path, "status": _webdav_client().mkdir(path)[0]}, fmt), fmt)


@app.command("delete")
def delete(
    path: str = typer.Argument(..., help="要删除的远端文件/目录 path 或 id。"),
    output_format: str = typer.Option(
        "human", "--format", callback=_format_callback, help=_format_option()
    ),
) -> None:
    """[REST] 删除远端文件或目录，并返回 API 结果。"""
    fmt = output_format
    _run(lambda: emit(_client()[2].delete(path), fmt), fmt)


@app.command("doctor")
def doctor(
    output_format: str = typer.Option(
        "human", "--format", callback=_format_callback_hj, help="输出格式：human 或 json。"
    ),
) -> None:
    """检查 [REST] endpoint 与 token 状态，不发起远端写操作。"""
    fmt = output_format
    _run(lambda: emit({"base_url": _client()[0], "token_set": bool(_client()[1])}, fmt), fmt)


def _webdav_client() -> WebDavClient:
    base, token = credentials()
    return WebDavClient(base + "/dav/", token)


@webdav_app.command("doctor")
def webdav_doctor(
    output_format: str = typer.Option("human", "--format", callback=_format_callback_hj, help="输出格式：human 或 json。"),
) -> None:
    """[WebDAV] 执行 OPTIONS/PROPFIND 能力探测，不执行写操作。"""
    fmt = output_format
    def action() -> None:
        endpoint = _client()[0] + "/dav/"
        client = _webdav_client()
        options_error = None
        try:
            options = client.options()
        except CfbedError as exc:
            options = {"status": None, "allow": (), "dav": ()}
            options_error = str(exc)
        propfind_status = None
        propfind_error = None
        if options_error is None:
            try:
                propfind_status = client.propfind()["status"]
            except CfbedError as exc:
                propfind_error = str(exc)
        emit({
            "endpoint": endpoint,
            "configured": bool(_client()[1]),
            "authentication": "api_token" if _client()[1] else "not_configured",
            "enabled": options["status"] is not None and options["status"] < 400,
            "options_status": options["status"],
            "propfind_status": propfind_status,
            "allow": options["allow"],
            "dav": options["dav"],
            "options_error": options_error,
            "propfind_error": propfind_error,
        }, fmt)
    _run(action, fmt)


def main(argv: Optional[List[str]] = None) -> int:
    """Console-script entry point, retaining the historical integer return value."""
    if argv is None:
        argv = sys.argv[1:]
    try:
        app(args=list(argv), prog_name="cfbed", standalone_mode=False)
    except (typer.Exit, SystemExit) as exc:
        code = getattr(exc, "exit_code", None)
        if code is None:
            code = getattr(exc, "code", 0)
        return int(code or 0)
    except NoArgsIsHelpError as exc:
        # With standalone_mode=False Typer does not render this normal help
        # flow itself; it propagates the parser exception to the caller.
        # Render it through Click's normal formatter instead of Rich's
        # pretty-exception traceback.  This also covers nested groups.
        exc.show()
        return 0
    except UsageError as exc:
        # Missing arguments, unknown commands/options, and bad parameter
        # values are all ordinary CLI usage errors and should keep Click's
        # usage/error/exit-code contract.
        exc.show()
        return int(exc.exit_code)
    except ClickException as exc:
        exc.show()
        return int(exc.exit_code)
    except typer.Abort:
        print("Aborted!", file=sys.stderr)
        return 1
    except (CfbedError, OSError, ValueError) as exc:
        code = getattr(exc, "code", 1)
        if not getattr(exc, "_cfbed_rendered", False):
            print(f"cfbed: {exc}", file=sys.stderr)
        return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

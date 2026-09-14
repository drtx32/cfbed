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
import sys
import urllib.parse
from email.message import Message
from pathlib import Path
from typing import List, Optional

import typer
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
app.add_typer(config_app, name="config")
app.add_typer(auth_app, name="auth")


def emit(value, fmt: str = "human") -> None:
    """Print a result without changing the machine-readable contract."""
    if fmt == "json":
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))


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
    file: Path = typer.Argument(..., exists=True, readable=True, help="要上传的任意文件路径。"),
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
    """上传任意文件，并返回 public URL、编码 URL、MIME 与大小。"""
    if name_type not in (None, "default", "origin", "index", "short"):
        raise typer.BadParameter("must be one of: default, origin, index, short", param_hint="--name-type")
    fmt = output_format
    _run(lambda: emit(_client()[2].upload(file, directory, filename, name_type, channel), fmt), fmt)


@app.command("list")
def list_files(
    path: Optional[str] = typer.Argument(None, help="远端目录路径；可省略以列出根目录。"),
    output_format: str = typer.Option(
        "human", "--format", callback=_format_callback, help=_format_option()
    ),
) -> None:
    """列出远端目录；path 接受 ImgBed 目录路径或 slash 形式。"""
    fmt = output_format
    _run(lambda: emit(_client()[2].list(path), fmt), fmt)


@app.command("info")
def info(
    path: str = typer.Argument(..., help="远端文件 path 或 file/id 形式。"),
    output_format: str = typer.Option(
        "human", "--format", callback=_format_callback, help=_format_option()
    ),
) -> None:
    """查看远端文件 metadata（HTTP status、MIME、大小和 headers）。"""
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
    """解析远端文件的 public URL，可选返回安全编码后的 URL。"""
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
    """读取或下载远端内容；二进制默认保存到文件，文本按 UTF-8 输出。"""
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


@app.command("move")
def move(
    src: str = typer.Argument(..., help="源文件/目录 path 或 id。"),
    dst: str = typer.Argument(..., help="目标文件/目录 path 或 id。"),
    output_format: str = typer.Option(
        "human", "--format", callback=_format_callback_hj, help="输出格式：human 或 json。"
    ),
) -> None:
    """移动远端文件或目录。"""
    fmt = output_format
    _run(lambda: emit(_client()[2].move(src, dst), fmt), fmt)


@app.command("rename")
def rename(
    path: str = typer.Argument(..., help="要重命名的远端文件/目录 path 或 id。"),
    new_name: str = typer.Argument(..., help="新的 basename。"),
    output_format: str = typer.Option(
        "human", "--format", callback=_format_callback_hj, help="输出格式：human 或 json。"
    ),
) -> None:
    """在原目录中重命名远端文件或目录。"""
    fmt = output_format
    _run(
        lambda: emit(
            _client()[2].move(path, str(Path(path).parent / new_name)),
            fmt,
        ),
        fmt,
    )


@app.command("delete")
def delete(
    path: str = typer.Argument(..., help="要删除的远端文件/目录 path 或 id。"),
    output_format: str = typer.Option(
        "human", "--format", callback=_format_callback, help=_format_option()
    ),
) -> None:
    """删除远端文件或目录，并返回 API 结果。"""
    fmt = output_format
    _run(lambda: emit(_client()[2].delete(path), fmt), fmt)


@app.command("doctor")
def doctor(
    output_format: str = typer.Option(
        "human", "--format", callback=_format_callback_hj, help="输出格式：human 或 json。"
    ),
) -> None:
    """检查本地 endpoint 与 token 状态，不发起远端写操作。"""
    fmt = output_format
    _run(lambda: emit({"base_url": _client()[0], "token_set": bool(_client()[1])}, fmt), fmt)


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
        print(f"cfbed: {exc}", file=sys.stderr)
        return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

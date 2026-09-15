import io
import json
import sys

import pytest
from typer.testing import CliRunner

from cfbed import cli
from cfbed.cli import app


runner = CliRunner()


def test_set_token_uses_hidden_prompt(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "set_token", seen.append)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: seen.append(prompt) or "prompt-token")
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    assert cli.main(["auth", "set-token"]) == 0
    assert seen == ["Token: ", "prompt-token"]


def test_set_token_reads_non_tty_stdin(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "set_token", seen.append)
    monkeypatch.setattr(sys, "stdin", io.StringIO("stdin-token\n"))

    assert cli.main(["auth", "set-token"]) == 0
    assert seen == ["stdin-token"]


@pytest.mark.parametrize("argv", [["auth", "set-token", "positional-token"], ["auth", "set-token", "--token", "legacy-token"]])
def test_set_token_accepts_token_forms(monkeypatch, argv):
    seen = []
    monkeypatch.setattr(cli, "set_token", seen.append)

    assert cli.main(argv) == 0
    assert seen == [argv[-1]]


def test_set_token_rejects_duplicate_forms(monkeypatch, capsys):
    monkeypatch.setattr(cli, "set_token", lambda token: pytest.fail("token should not be saved"))

    assert cli.main(["auth", "set-token", "one", "--token", "two"]) == 1
    assert "specify the token once" in capsys.readouterr().err


def test_set_token_help_documents_optional_token():
    assert cli.main(["auth", "set-token", "--help"]) == 0
    result = runner.invoke(app, ["auth", "set-token", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "[token]" in output
    assert "deprecated" in output


class FakeBinaryClient:
    def __init__(self, *args):
        pass

    def download(self, path):
        return 200, {"Content-Type": "image/jpeg"}, b"\xff\xd8JFIF"


def test_binary_get_defaults_to_safe_file(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "credentials", lambda: ("https://api.test", None))
    monkeypatch.setattr(cli, "Client", FakeBinaryClient)

    assert cli.main(["get", "static-sites/每日复盘.jpg"]) == 0
    assert (tmp_path / "每日复盘.jpg").read_bytes() == b"\xff\xd8JFIF"
    assert "saved 6 bytes (image/jpeg)" in capsys.readouterr().out


def test_binary_stdout_refuses_tty(monkeypatch, capsys):
    monkeypatch.setattr(cli, "credentials", lambda: ("https://api.test", None))
    monkeypatch.setattr(cli, "Client", FakeBinaryClient)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)

    assert cli.main(["get", "demo.mp4", "--stdout"]) == 1
    assert "refusing to write binary data to a TTY" in capsys.readouterr().err


class TextClient(FakeBinaryClient):
    def download(self, path):
        return 200, {"Content-Type": "text/markdown; charset=utf-8"}, "# 每日复盘".encode()


def test_text_get_remains_text(monkeypatch, capsys):
    monkeypatch.setattr(cli, "credentials", lambda: ("https://api.test", None))
    monkeypatch.setattr(cli, "Client", TextClient)

    assert cli.main(["get", "daily.md"]) == 0
    assert capsys.readouterr().out == "# 每日复盘"


def test_top_level_help_has_sections_and_descriptions():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert "Options:" in result.stdout
    assert "Commands:" in result.stdout
    for command in ("config", "auth", "upload", "list", "info", "get", "url", "mkdir", "delete", "doctor"):
        assert command in result.stdout


def test_representative_nested_help_is_documented():
    for args, expected in (
        (("config", "--help"), "set-base-url"),
        (("auth", "set-token", "--help"), "--token"),
        (("upload", "--help"), "--directory"),
        (("get", "--help"), "--output"),
    ):
        result = runner.invoke(app, list(args))
        assert result.exit_code == 0
        assert expected in result.stdout


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "0.1.0"


@pytest.mark.parametrize("argv", [[], ["config"], ["auth"]])
def test_bare_groups_render_help_without_traceback(capsys, argv):
    assert cli.main(argv) == 0
    output = capsys.readouterr()
    combined = output.out + output.err
    assert "Usage:" in combined
    assert "Traceback" not in combined
    assert "NoArgsIsHelpError" not in combined


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["upload"], "Missing argument 'file'"),
        (["info"], "Missing argument 'path'"),
        (["url"], "Missing argument 'path'"),
        (["get"], "Missing argument 'path'"),
        (["mkdir"], "Missing argument 'path'"),
        (["delete"], "Missing argument 'path'"),
        (["config", "set-base-url"], "Missing argument 'url'"),
    ],
)
def test_missing_required_arguments_are_normal_usage_errors(capsys, argv, expected):
    assert cli.main(argv) == 2
    output = capsys.readouterr()
    combined = output.out + output.err
    assert expected in combined
    assert "Traceback" not in combined
    assert "Error" in combined


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["not-a-command"], "No such command"),
        (["doctor", "--not-an-option"], "No such option"),
        (["upload", "--name-type", "invalid", "missing.file"], "Invalid value"),
    ],
)
def test_invalid_cli_input_has_no_traceback(capsys, argv, expected):
    assert cli.main(argv) == 2
    output = capsys.readouterr()
    combined = output.out + output.err
    assert expected in combined
    assert "Traceback" not in combined
    assert "Usage:" in combined


class FakeListClient:
    def __init__(self, *args):
        pass

    def list(self, path=None):
        return {
            "directories": ["static-sites", "downloads", "blog", "nsfw"],
            "files": [
                {"Name": "nsfw/每日复盘.mp4", "Directory": "nsfw", "metadata": {"FileType": "video/mp4", "FileSizeBytes": 38996945, "TimeStamp": 1785541047349}},
                {"Name": "unknown.bin", "metadata": {}},
            ],
        }


def test_list_human_table_is_the_tree_fallback_and_json_contract_is_unchanged(monkeypatch, capsys):
    monkeypatch.setattr(cli, "credentials", lambda: ("https://api.test", None))
    monkeypatch.setattr(cli, "Client", FakeListClient)

    for args in (["list"], ["list", "--format", "human"]):
        assert cli.main(args) == 0
        human = capsys.readouterr().out
        # Flat entries are not a useful Tree, so Table is the required
        # human-readable fallback and must never become a JSON dump.
        assert "Name" in human
        assert "Type" in human
        assert "Size" in human
        assert "Uploaded" in human
        assert "Path: /" in human
        assert "📁 static-sites/" in human
        assert "📄 每日复盘.mp4" in human
        assert "video/mp4" in human
        assert "37.2 MB" in human
        assert "unknown.bin" in human and "—" in human
        assert not human.lstrip().startswith("{")

    assert cli.main(["list", "nsfw", "--format", "human"]) == 0
    nested = capsys.readouterr().out
    assert "Path: /nsfw/" in nested
    assert "nsfw/每日复盘.mp4" not in nested

    assert cli.main(["list", "--format", "json"]) == 0
    machine = capsys.readouterr().out
    assert '"Name": "unknown.bin"' in machine
    assert machine.lstrip().startswith("{")


def test_list_human_alias_is_a_normal_usage_error(capsys):
    assert cli.main(["list", "--human"]) == 2
    output = capsys.readouterr()
    combined = output.out + output.err
    assert "Error: No such option: --human" in combined
    assert "Traceback" not in combined
    assert "NoSuchOption" not in combined


def test_upload_content_mode_reads_utf8_and_cleans_temp(monkeypatch, tmp_path, capsys):
    seen = {}

    class FakeUploadClient:
        def __init__(self, *args): pass

        def upload(self, path, directory, filename, name_type, channel, **kwargs):
            seen.update(path=path, directory=directory, filename=filename, body=path.read_bytes(), kwargs=kwargs)
            return {"source_file": kwargs["source_file"], "size": len(seen["body"])}

    monkeypatch.setattr(cli, "credentials", lambda: ("https://api.test", None))
    monkeypatch.setattr(cli, "Client", FakeUploadClient)
    assert cli.main(["upload", "页面.html", "--content", "<h1>你好</h1>", "--directory", "static-sites", "--format", "json"]) == 0
    assert seen["filename"] == "页面.html"
    assert seen["body"] == "<h1>你好</h1>".encode()
    assert not seen["path"].exists()
    assert json.loads(capsys.readouterr().out)["source_file"] == "页面.html"


def test_upload_content_stdin_mode_is_real_cli_boundary(monkeypatch, capsys):
    seen = {}

    class FakeUploadClient:
        def __init__(self, *args): pass

        def upload(self, path, directory, filename, name_type, channel, **kwargs):
            seen.update(filename=filename, body=path.read_bytes())
            return {"size": len(seen["body"])}

    monkeypatch.setattr(cli, "credentials", lambda: ("https://api.test", None))
    monkeypatch.setattr(cli, "Client", FakeUploadClient)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("<main>你好\n世界</main>"))
    assert cli.main(["upload", "site.html", "--content", "-"]) == 0
    assert seen == {"filename": "site.html", "body": "<main>你好\n世界</main>".encode()}
    assert '"size"' in capsys.readouterr().out


def test_upload_mcp_format_is_an_envelope(monkeypatch, capsys):
    class FakeUploadClient:
        def __init__(self, *args): pass
        def upload(self, path, directory, filename, name_type, channel, **kwargs):
            return {"source_file": kwargs["source_file"], "size": path.stat().st_size}

    monkeypatch.setattr(cli, "credentials", lambda: ("https://api.test", None))
    monkeypatch.setattr(cli, "Client", FakeUploadClient)
    assert cli.main(["upload", "report.html", "--content", "hello", "--format", "mcp"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["result"]["content"][0]["type"] == "text"
    assert json.loads(envelope["result"]["content"][0]["text"])["source_file"] == "report.html"


def test_upload_url_failure_is_printed_once_without_traceback(monkeypatch, capsys):
    monkeypatch.setattr(cli, "download_to_temp", lambda url: (_ for _ in ()).throw(cli.CfbedError("download failed: test", 2)))
    assert cli.main(["upload", "https://example.test/report.md"]) == 2
    error = capsys.readouterr().err
    assert error == "cfbed: download failed: test\n"
    assert "Traceback" not in error

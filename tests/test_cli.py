import io
import sys

import pytest

from cfbed import cli


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


def test_set_token_help_documents_optional_token(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["auth", "set-token", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
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

from __future__ import annotations
import argparse, base64, json, mimetypes, sys
from pathlib import Path
from . import __version__
from .core import Client, CfbedError, config_public, credentials, clear_token, encoded_url, set_base_url, set_token

def emit(value, fmt="human"):
    if fmt == "json": print(json.dumps(value, ensure_ascii=False, indent=2))
    else: print(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))

def parser():
    p=argparse.ArgumentParser(prog="cfbed", description="CLI for CloudFlare ImgBed")
    p.add_argument("--version", action="version", version=__version__)
    sub=p.add_subparsers(dest="command", required=True)
    c=sub.add_parser("config"); cs=c.add_subparsers(dest="config_command", required=True)
    x=cs.add_parser("set-base-url"); x.add_argument("url")
    cs.add_parser("show")
    a=sub.add_parser("auth"); ass=a.add_subparsers(dest="auth_command", required=True); t=ass.add_parser("set-token"); t.add_argument("--token"); ass.add_parser("clear")
    u=sub.add_parser("upload"); u.add_argument("file", type=Path); u.add_argument("--directory"); u.add_argument("--filename"); u.add_argument("--name-type", choices=["default","origin","index","short"]); u.add_argument("--channel"); u.add_argument("--format", choices=["human","json","mcp"], default="human")
    for name in ["info","get","url","delete"]:
        x=sub.add_parser(name); x.add_argument("path"); x.add_argument("--format", choices=["human","json","mcp"], default="human")
    x=sub.add_parser("list"); x.add_argument("path", nargs="?", default=None); x.add_argument("--format", choices=["human","json","mcp"], default="human")
    sub.choices["url"].add_argument("--encoded", action="store_true")
    m=sub.add_parser("move"); m.add_argument("src"); m.add_argument("dst"); m.add_argument("--format", choices=["human","json"], default="human")
    r=sub.add_parser("rename"); r.add_argument("path"); r.add_argument("new_name"); r.add_argument("--format", choices=["human","json"], default="human")
    d=sub.add_parser("doctor"); d.add_argument("--format", choices=["human","json"], default="human")
    return p

def main(argv=None):
    args=parser().parse_args(argv)
    try:
        if args.command == "config":
            if args.config_command == "set-base-url": set_base_url(args.url); emit({"base_url":args.url.rstrip("/")}, "json")
            else: emit(config_public(), "json")
            return 0
        if args.command == "auth":
            if args.auth_command == "clear": clear_token(); emit({"token_set":False}, "json")
            else: set_token(args.token or sys.stdin.read().strip()); emit({"token_set":True}, "json")
            return 0
        base, token = credentials(); client=Client(base, token)
        if args.command == "upload": emit(client.upload(args.file, args.directory, args.filename, args.name_type, args.channel), args.format); return 0
        if args.command == "list": emit(client.list(args.path), args.format); return 0
        if args.command == "url":
            url=args.path if args.path.startswith("http") else base + "/file/" + "/".join(__import__("urllib.parse", fromlist=["quote"]).quote(p, safe="") for p in args.path.strip("/").split("/"))
            emit(encoded_url(url) if getattr(args, "encoded", False) else url, args.format); return 0
        if args.command == "get":
            _, headers, body=client.download(args.path); mime=headers.get("Content-Type", "application/octet-stream").split(";",1)[0]
            if args.format == "mcp":
                if mime.startswith("image/"): emit({"result":{"content":[{"type":"image","mimeType":mime,"data":base64.b64encode(body).decode()}]}}, "json")
                elif mime.startswith("text/"): emit({"result":{"content":[{"type":"text","text":body.decode("utf-8")}]}}, "json")
                else: emit({"result":{"content":[{"type":"resource","resource":{"uri":base+"/file/"+args.path,"mimeType":mime}}]}}, "json")
            else: sys.stdout.buffer.write(body)
            return 0
        if args.command == "delete": emit(client.delete(args.path), args.format); return 0
        if args.command in ("move","rename"): emit(client.move(args.src, args.dst) if args.command=="move" else client.move(args.path, str(Path(args.path).parent / args.new_name)), args.format); return 0
        if args.command == "info":
            status, headers, _=client.request("HEAD", "/file/"+args.path.lstrip("/")); emit({"status":status,"content_type":headers.get("Content-Type"),"size":headers.get("Content-Length"),"headers":headers}, args.format); return 0
        if args.command == "doctor": emit({"base_url":base,"token_set":bool(token)}, args.format); return 0
    except (CfbedError, OSError, ValueError) as exc:
        if getattr(args, "format", "human") == "json": print(json.dumps({"error":str(exc),"code":getattr(exc,"code",1)}), file=sys.stderr)
        else: print(f"cfbed: {exc}", file=sys.stderr)
        return getattr(exc,"code",1)

if __name__ == "__main__": raise SystemExit(main())

# cfbed

`cfbed` is a standalone, gateway-agnostic CLI for the official CloudFlare ImgBed file API. It handles arbitrary files (HTML, Markdown, CSS, JavaScript, PDF, text, and images), preserves the server-returned public URL, and offers stable JSON and MCP-style output for automation.

## Install

```bash
pipx install cfbed
# or, from a checkout:
uv tool install .
```

## Secure setup

```bash
cfbed config set-base-url https://your-imgbed.example
cfbed auth set-token                 # reads token from stdin; never echoes it
cfbed config show                    # never prints the token
cfbed doctor --format json
cfbed auth clear
```

Configuration is stored under `~/.cache/cfbed/` with mode `0700`. The token is encrypted with AES-256-GCM and the local key is separately mode `0600`; a system keyring can be integrated by deployments that provide one. `CFBED_BASE_URL` and `CFBED_API_TOKEN` are supported only as ephemeral/CI overrides.

## Examples

```bash
cfbed upload site/index.html --directory static-sites --name-type origin --format json
cfbed list static-sites --format json
cfbed info static-sites/index.html --format json
cfbed url static-sites/index.html --encoded
cfbed get image/猫 图.png --format mcp
cfbed move old/name.pdf archive/name.pdf
cfbed rename archive/name.pdf final.pdf
cfbed delete archive/final.pdf
```

`upload` returns `source_file`, `directory`, `stored_name`, `public_url` exactly as returned by ImgBed (or its documented `src` fallback), `encoded_url`, `content_type`, and `size`. URL encoding applies to path segments only, so schemes, hosts, and query strings remain intact. `--name-type`, `--channel`, and `--directory` are command flags, not persistent defaults.

## Output contracts

`--format human` is for people, `--format json` emits deterministic JSON, and `--format mcp` emits an envelope shaped as `{ "result": { "content": [...] } }`. Image downloads use an MCP image block with the response MIME type and base64 data; UTF-8 text uses a text block; other binary files use a resource reference and are never coerced into text. External agent gateways can consume this machine-readable/MCP-formatted output without being a dependency of this project.

Exit code `0` means success, `1` means local validation/configuration failure, and `2` means an API or network failure. Errors in JSON mode are written to stderr and contain `error` and `code`; tokens are never included.

The implementation follows the official [ImgBed upload](https://cfbed.sanyue.de/api/upload.html) and [read](https://cfbed.sanyue.de/api/file.html) API documentation.

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
cfbed auth set-token                 # hidden interactive prompt; never echoes it
printf '%s' "$TOKEN" | cfbed auth set-token  # safe non-interactive setup
cfbed config show                    # never prints the token
cfbed doctor --format json
cfbed auth clear
```

For convenience, `cfbed auth set-token TOKEN` is also supported, but token values
in command arguments can be exposed in shell history and process listings. The
older `cfbed auth set-token --token TOKEN` form remains available temporarily
for compatibility and is deprecated; prefer the hidden prompt or stdin.

Configuration is stored under `~/.cache/cfbed/` with mode `0700`. The token is encrypted with AES-256-GCM and the local key is separately mode `0600`; a system keyring can be integrated by deployments that provide one. `CFBED_BASE_URL` and `CFBED_API_TOKEN` are supported only as ephemeral/CI overrides.

## Examples

```bash
cfbed upload site/index.html --directory static-sites --name-type origin --format json
cfbed list static-sites --format json
cfbed info static-sites/index.html --format json
cfbed url static-sites/index.html --encoded
cfbed get image/猫 图.png --format mcp
cfbed get video/demo.mp4                  # saves video/demo.mp4 in the current directory
cfbed get video/demo.mp4 --output demo.mp4
cfbed get video/demo.mp4 --stdout > demo.mp4
cfbed move old/name.pdf archive/name.pdf
cfbed rename archive/name.pdf final.pdf
cfbed delete archive/final.pdf
```

`upload` returns `source_file`, `directory`, `stored_name`, `public_url` exactly as returned by ImgBed (or its documented `src` fallback), `encoded_url`, `content_type`, and `size`. URL encoding applies to path segments only, so schemes, hosts, and query strings remain intact. `--name-type`, `--channel`, and `--directory` are command flags, not persistent defaults.

## Output contracts

`--format human` is for people, `--format json` emits deterministic JSON, and `--format mcp` emits an envelope shaped as `{ "result": { "content": [...] } }`. Text-like downloads are printed as UTF-8 text. Binary downloads default to a file named from the server's `Content-Disposition` or URL path and report the saved path; `--output` selects a path explicitly. `--stdout` is an explicit raw-byte mode and refuses to write when stdout is a TTY, so it is safe for piping or redirection. Image downloads use an MCP image block with the response MIME type and base64 data; other binary files use a resource reference and are never coerced into text. External agent gateways can consume this machine-readable/MCP-formatted output without being a dependency of this project.

Exit code `0` means success, `1` means local validation/configuration failure, and `2` means an API or network failure. Errors in JSON mode are written to stderr and contain `error` and `code`; tokens are never included.

The implementation follows the official [ImgBed upload](https://cfbed.sanyue.de/api/upload.html) and [read](https://cfbed.sanyue.de/api/file.html) API documentation.

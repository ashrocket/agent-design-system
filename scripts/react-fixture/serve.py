"""Preview three fixture assets locally; this helper does not attest hashes."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ASSETS = {"/": "index.html", "/app.js": "app.js", "/app.css": "app.css"}
MEDIA_TYPES = {
    "index.html": "text/html",
    "app.js": "text/javascript",
    "app.css": "text/css",
}
RED_OVERRIDE = b"\n.primary-button{background-color:#ff0000!important}\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--inject-css", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    if {path.name for path in root.iterdir()} != set(ASSETS.values()):
        raise SystemExit("fixture preview must contain exactly three assets")
    assets = {name: (root / name).read_bytes() for name in ASSETS.values()}
    if args.inject_css:
        assets["app.css"] += RED_OVERRIDE

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            name = ASSETS.get(path)
            if name is None:
                self.send_error(404)
                return
            content = assets[name]
            self.send_response(200)
            self.send_header("Content-Type", MEDIA_TYPES[name] + "; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'none'; img-src 'none'; font-src 'none'; base-uri 'none'; form-action 'none'",
            )
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format: str, *values: object) -> None:
            print(
                json.dumps(
                    {"client": self.client_address[0], "message": format % values}
                ),
                flush=True,
            )

    server = HTTPServer(("127.0.0.1", args.port), Handler)
    print(
        json.dumps(
            {"serving": str(root), "port": args.port, "inject_css": args.inject_css}
        ),
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()

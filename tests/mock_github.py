"""Tiny in-process fake of the GitHub REST endpoints the release engine uses."""
import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class MockGitHub:
    def __init__(self):
        self.src_releases = []            # upstream releases (dicts as the real API returns them)
        self.src_default_branch = {}      # "owner/repo" -> branch
        self.files = {}                   # download name -> bytes served for upstream assets
        self.dest = {}                    # release id -> release dict
        self.next_id = 1000
        self.asset_data = {}
        self.calls = []                   # (method, path, payload)
        self.fail_upload_names = set()
        self.download_status = None       # e.g. 504 makes /download/* fail
        self.asset_files = {}             # asset id -> key in self.files
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def mutations(self):
        return [c for c in self.calls if c[0] in ("POST", "PATCH", "DELETE")]

    def add_source_release(self, rid, tag, assets=(), prerelease=False, body="notes", published="2026-01-01T00:00:00Z"):
        rel = {"id": rid, "tag_name": tag, "name": tag, "body": body, "draft": False, "prerelease": prerelease,
               "published_at": published, "created_at": published, "html_url": f"https://github.com/src/app/releases/tag/{tag}",
               "assets": []}
        for i, (name, data, with_digest) in enumerate(assets):
            self.files[f"{rid}-{name}"] = data
            rel["assets"].append({
                "id": rid * 100 + i, "name": name, "size": len(data), "state": "uploaded",
                "content_type": "application/octet-stream", "updated_at": published,
                "digest": ("sha256:" + hashlib.sha256(data).hexdigest()) if with_digest else None,
                "url": f"{self.base}/repos/src/app/releases/assets/{rid * 100 + i}",
                "browser_download_url": f"{self.base}/download/{rid}-{name}"})
            self.asset_files[rid * 100 + i] = f"{rid}-{name}"
        self.src_releases.append(rel)
        return rel

    def _handler(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, obj=None, raw=None):
                body = raw if raw is not None else (json.dumps(obj).encode() if obj is not None else b"")
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _page(self, items, qs):
                return items if qs.get("page", ["1"])[0] == "1" else []

            def do_GET(self):
                u = urlsplit(self.path)
                qs = parse_qs(u.query)
                outer.calls.append(("GET", u.path, None))
                m = re.match(r"^/repos/[^/]+/[^/]+/releases/assets/(\d+)$", u.path)
                if m:
                    return self._send(200, raw=outer.files[outer.asset_files[int(m.group(1))]])
                m = re.match(r"^/repos/([^/]+/[^/]+)$", u.path)
                if m and m.group(1) in outer.src_default_branch:
                    return self._send(200, {"default_branch": outer.src_default_branch[m.group(1)]})
                if u.path == "/repos/src/app/releases" or re.match(r"^/repos/[^/]+/[^/]+/releases$", u.path) and \
                        u.path != "/repos/dst/repo/releases":
                    return self._send(200, self._page(outer.src_releases, qs))
                if u.path == "/repos/dst/repo/releases":
                    return self._send(200, self._page(list(outer.dest.values()), qs))
                if u.path.startswith("/download/"):
                    if outer.download_status:
                        return self._send(outer.download_status, {"message": "gateway"})
                    name = u.path[len("/download/"):]
                    return self._send(200, raw=outer.files[name])
                self._send(404, {"message": "not found"})

            def do_POST(self):
                u = urlsplit(self.path)
                data = self._body()
                if u.path == "/repos/dst/repo/releases":
                    payload = json.loads(data)
                    outer.calls.append(("POST", u.path, payload))
                    outer.next_id += 1
                    rid = outer.next_id
                    rel = dict(payload, id=rid, assets=[], html_url=f"https://github.com/dst/repo/releases/tag/{payload['tag_name']}",
                               upload_url=f"{outer.base}/uploads/repos/dst/repo/releases/{rid}/assets{{?name,label}}")
                    outer.dest[rid] = rel
                    return self._send(201, rel)
                m = re.match(r"^/uploads/repos/dst/repo/releases/(\d+)/assets$", u.path)
                if m:
                    name = parse_qs(u.query)["name"][0]
                    outer.calls.append(("POST", u.path + "?name=" + name, None))
                    if name in outer.fail_upload_names:
                        return self._send(422, {"message": "boom"})
                    rel = outer.dest[int(m.group(1))]
                    outer.next_id += 1
                    asset = {"id": outer.next_id, "name": name, "size": len(data),
                             "digest": "sha256:" + hashlib.sha256(data).hexdigest()}
                    outer.asset_data[asset["id"]] = data
                    rel["assets"].append(asset)
                    return self._send(201, asset)
                self._send(404, {"message": "not found"})

            def do_PATCH(self):
                u = urlsplit(self.path)
                payload = json.loads(self._body())
                outer.calls.append(("PATCH", u.path, payload))
                m = re.match(r"^/repos/dst/repo/releases/(\d+)$", u.path)
                if m:
                    rel = outer.dest[int(m.group(1))]
                    rel.update({k: v for k, v in payload.items() if k != "make_latest"})
                    return self._send(200, rel)
                self._send(404, {"message": "not found"})

            def do_DELETE(self):
                u = urlsplit(self.path)
                outer.calls.append(("DELETE", u.path, None))
                m = re.match(r"^/repos/dst/repo/releases/assets/(\d+)$", u.path)
                if m:
                    aid = int(m.group(1))
                    for rel in outer.dest.values():
                        rel["assets"] = [a for a in rel["assets"] if a["id"] != aid]
                    return self._send(204)
                self._send(404, {"message": "not found"})

        return H

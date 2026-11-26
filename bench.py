"""本地设备协议回放台。"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent


def read(name):
    with (ROOT / name).open(encoding="utf-8") as source:
        return json.load(source)


def load_scene(path=None):
    path = Path(path) if path else ROOT / "observations.ndjson"
    with path.open(encoding="utf-8") as source:
        rows = [json.loads(line) for line in source if line.strip()]
    if [row["seq"] for row in rows] != list(range(1, len(rows) + 1)):
        raise ValueError("观测序号须连续")
    if any(not row["device"] or not row["kind"] for row in rows):
        raise ValueError("观测须有设备和事件类型")
    return rows


def make_server(rows, port):
    class Handler(BaseHTTPRequestHandler):
        def output(self, status, contents):
            data = json.dumps(contents, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/devices":
                self.output(200, read("devices.json"))
                return
            if url.path != "/events":
                self.output(404, {"error": "路径不存在"})
                return
            query = parse_qs(url.query)
            try:
                after = int(query.get("after", ["0"])[0])
                limit = int(query.get("limit", ["16"])[0])
                if after < 0 or not 1 <= limit <= 100:
                    raise ValueError
            except ValueError:
                self.output(400, {"error": "游标或数量超出范围"})
                return
            page = rows[after:after + limit]
            self.output(200, {"events": page, "cursor": page[-1]["seq"] if page else after})

        def do_POST(self):
            if self.path != "/commands":
                self.output(404, {"error": "路径不存在"})
                return
            try:
                length = int(self.headers["Content-Length"])
                if not 0 < length <= 65536:
                    raise ValueError
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict):
                    raise ValueError
                if (request["device"] not in read("devices.json") or
                        not all(isinstance(request[k], str) and request[k] for k in ("id", "action"))):
                    raise ValueError
            except (ValueError, TypeError, KeyError):
                self.output(400, {"error": "消息没有合法设备或请求标识"})
                return
            self.output(202, {"id": request["id"], "device": request["device"], "receipt": "transport-only"})

        def log_message(self, fmt, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    cli = argparse.ArgumentParser(description="地面设备资料回放台")
    cli.add_argument("--listen", type=int, help="侦听的本机端口")
    cli.add_argument("--scene", type=Path, help="其他符合协议的观测记录")
    args = cli.parse_args()
    rows = load_scene(args.scene)
    if args.listen is None:
        print(f"读取 {len(read('devices.json'))} 个设备或来源和 {len(rows)} 条观测")
        return
    with make_server(rows, args.listen) as server:
        print(f"监听 127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()

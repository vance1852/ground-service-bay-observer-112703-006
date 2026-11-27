"""接入服务：把回放台观测送入判定引擎，并向值班台与班组提供查询。

服务只读回放记录、只写自己的判定结果。命令订阅回放通道的收件凭据，不控制
真实设备；`POST /commands` 的返回同样只表示通道收件，不代表物理动作已完成。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .engine import YardEngine
from .scene import Observation, load_devices, load_policy, load_scene

INDEX_HTML = """<!doctype html>
<meta charset="utf-8">
<title>工位接入服务</title>
<h1>相邻检修位接入服务</h1>
<p>只读接口：<code>/bays</code>、<code>/jobs</code>、<code>/explain?bay=N</code>、
<code>/check?bay=N&amp;type=sweep</code>。<br>
回放接入：<code>POST /ingest</code> 提交一条观测；<code>POST /ingest/advance</code>
按下一条回放记录推进。</p>
"""


class IngestService:
    """顺序推进回放，并对外暴露判定结果。线程安全。"""

    def __init__(self, rows: list[Observation] | None = None,
                 devices: dict | None = None, policy: dict | None = None):
        self.rows = load_scene() if rows is None else rows
        self.devices = load_devices() if devices is None else devices
        self.policy = load_policy() if policy is None else policy
        self._lock = threading.RLock()
        self.cursor = 0
        self.engine = YardEngine([], self.devices, self.policy)

    # ---------------------------------------------------------- 回放接入

    def advance(self, count: int = 1) -> list[dict]:
        with self._lock:
            out = []
            for _ in range(max(0, count)):
                if self.cursor >= len(self.rows):
                    break
                row = self.rows[self.cursor]
                self.cursor += 1
                out.append(self.engine.ingest(row))
            return out

    def ingest(self, obs: Observation) -> dict:
        with self._lock:
            return self.engine.ingest(obs)

    def ingest_mapping(self, payload: dict) -> dict:
        required = ("seq", "device", "kind")
        if not all(key in payload for key in required):
            raise ValueError("观测须含 seq、device、kind")
        seq = payload.pop("seq")
        kind = payload.pop("kind")
        device = payload.pop("device")
        at = payload.pop("at", 0)
        obs = Observation(int(seq), str(device), str(kind), float(at), payload)
        return self.ingest(obs)

    def catch_up(self) -> list[dict]:
        with self._lock:
            return self.advance(len(self.rows) - self.cursor)

    # ---------------------------------------------------------- 查询

    def snapshot(self) -> dict:
        with self._lock:
            data = self.engine.snapshot()
            data["cursor"] = self.cursor
            data["total"] = len(self.rows)
            return data

    def bays(self) -> dict:
        with self._lock:
            return {bay: self.engine.view(bay) for bay in self.engine.bays}

    def jobs(self) -> dict:
        with self._lock:
            return {job_id: self.engine.job_view(job) for job_id, job in self.engine.jobs.items()}

    def explain(self, bay: str) -> dict:
        with self._lock:
            return self.engine.explain_bay(bay)

    def check(self, bay: str, kind: str, unit: str | None = None) -> dict:
        with self._lock:
            return self.engine.check(bay, kind, unit)

    def shift_check(self) -> dict:
        """当班开工检查：逐位结论、受影响作业和可见故障。"""
        with self._lock:
            engine = self.engine
            return {
                "at": engine.now,
                "bays": {bay: engine.view(bay) for bay in engine.bays},
                "jobs": {job_id: engine.job_view(job) for job_id, job in engine.jobs.items()},
                "degraded_sources": [
                    {
                        "device": fault.device,
                        "bay": fault.bay,
                        "status": fault.status,
                        "since": fault.at,
                        "recovered_at": fault.recovered_at,
                    }
                    for fault in engine.faults
                    if fault.recovered_at is None
                ],
                "recent_notices": engine.notices[-20:],
                "rejected": engine.rejected,
            }


def make_server(service: IngestService, port: int):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, contents) -> None:
            data = json.dumps(contents, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 65536:
                raise ValueError("空消息")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("消息须为对象")
            return payload

        def do_GET(self):
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            try:
                if url.path in ("/", "/index.html"):
                    data = INDEX_HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if url.path == "/devices":
                    self._json(200, service.devices)
                    return
                if url.path == "/events":
                    after = int(query.get("after", ["0"])[0])
                    limit = int(query.get("limit", ["16"])[0])
                    if after < 0 or not 1 <= limit <= 100:
                        raise ValueError("游标或数量超出范围")
                    page = service.rows[after:after + limit]
                    rows = [row_to_dict(row) for row in page]
                    self._json(200, {"events": rows,
                                     "cursor": rows[-1]["seq"] if rows else after})
                    return
                if url.path == "/snapshot":
                    self._json(200, service.snapshot())
                    return
                if url.path == "/bays":
                    self._json(200, service.bays())
                    return
                if url.path == "/jobs":
                    self._json(200, service.jobs())
                    return
                if url.path == "/shift":
                    self._json(200, service.shift_check())
                    return
                if url.path == "/explain":
                    bay = query.get("bay", [""])[0]
                    if not bay:
                        raise ValueError("须指定 bay")
                    self._json(200, service.explain(bay))
                    return
                if url.path == "/check":
                    bay = query.get("bay", [""])[0]
                    kind = query.get("type", [""])[0]
                    unit = query.get("unit", [None])[0]
                    if not bay or not kind:
                        raise ValueError("须指定 bay 与 type")
                    self._json(200, {"bay": bay, "type": kind, "unit": unit,
                                     **service.check(bay, kind, unit)})
                    return
            except (KeyError, ValueError) as error:
                self._json(400, {"error": str(error)})
                return
            self._json(404, {"error": "路径不存在"})

        def do_POST(self):
            url = urlsplit(self.path)
            try:
                payload = self._body()
                if url.path == "/ingest/advance":
                    count = int(payload.get("count", 1))
                    if not 1 <= count <= 100:
                        raise ValueError("count 超出范围")
                    self._json(200, {"ingested": service.advance(count)})
                    return
                if url.path == "/ingest/catch-up":
                    self._json(200, {"ingested": service.catch_up()})
                    return
                if url.path == "/ingest":
                    result = service.ingest_mapping(dict(payload))
                    self._json(200, {"result": result})
                    return
                if url.path == "/commands":
                    if (payload.get("device") not in service.devices or
                            not all(isinstance(payload.get(key), str) and payload[key]
                                    for key in ("id", "action"))):
                        raise ValueError("消息没有合法设备或请求标识")
                    self._json(202, {"id": payload["id"], "device": payload["device"],
                                     "receipt": "transport-only",
                                     "note": "回放通道收件凭据；具体动作以随后的设备观测为准"})
                    return
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self._json(400, {"error": str(error) or "消息无效"})
                return
            self._json(404, {"error": "路径不存在"})

        def log_message(self, fmt, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def row_to_dict(row: Observation) -> dict:
    data = {"seq": row.seq, "device": row.device, "kind": row.kind, "at": row.at}
    data.update(row.data)
    return data


def main(argv: list[str] | None = None) -> int:
    import argparse

    cli = argparse.ArgumentParser(description="相邻检修位接入服务")
    cli.add_argument("--listen", type=int, help="侦听的本机端口")
    cli.add_argument("--scene", type=Path, help="其他符合协议的观测记录")
    cli.add_argument("--catch-up", action="store_true", help="启动时先接入全部回放记录")
    args = cli.parse_args(argv)
    rows = load_scene(args.scene) if args.scene else load_scene()
    service = IngestService(rows, load_devices(), load_policy())
    if args.catch_up:
        service.catch_up()
    if args.listen is None:
        print(f"读取 {len(service.devices)} 个设备或来源和 {len(rows)} 条观测")
        return 0
    with make_server(service, args.listen) as server:
        print(f"监听 127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

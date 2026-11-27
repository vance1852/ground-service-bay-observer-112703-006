"""本地设备协议回放台。

底层仍按原协议提供 `/devices`、`/events`、`/commands`；接入服务在其上增加
工位占用、作业准入与复核查询，见 `yard/service.py`。
"""

import argparse
import json
from pathlib import Path

from yard.scene import Observation, load_devices, load_policy
from yard.service import IngestService, make_server as _make_server

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


def as_observations(rows):
    """把回放原始字典转成判定引擎的观测对象。"""
    return [
        Observation(row["seq"], row["device"], row["kind"], float(row.get("at", 0)),
                    {key: value for key, value in row.items()
                     if key not in ("seq", "device", "kind", "at")})
        for row in rows
    ]


def make_server(rows, port):
    """接收原始观测字典并返回带接入服务的回放台。"""
    service = IngestService(as_observations(rows), load_devices(), load_policy())
    return _make_server(service, port)


def main(argv=None):
    cli = argparse.ArgumentParser(description="地面设备资料回放台")
    cli.add_argument("--listen", type=int, help="侦听的本机端口")
    cli.add_argument("--scene", type=Path, help="其他符合协议的观测记录")
    cli.add_argument("--catch-up", action="store_true", help="启动时先接入全部回放记录")
    args = cli.parse_args(argv)
    rows = load_scene(args.scene)
    service = IngestService(as_observations(rows), load_devices(), load_policy())
    if args.catch_up:
        service.catch_up()
    if args.listen is None:
        print(f"读取 {len(service.devices)} 个设备或来源和 {len(rows)} 条观测")
        return
    with _make_server(service, args.listen) as server:
        print(f"监听 127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    main()

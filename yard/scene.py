"""资料读取：设备表、业务配置与回放观测。

观测按回放顺序（seq）排列。设备时间 at 可能与回放顺序不一致，后补的旧观察
arrived 晚于 at，推断时必须按 at 与 arrived 分别处理，而不是把 seq 当时间。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import json

ROOT = Path(__file__).resolve().parent.parent


def _read_json(name: str) -> Any:
    with (ROOT / name).open(encoding="utf-8") as source:
        return json.load(source)


def load_devices() -> dict[str, dict]:
    return _read_json("devices.json")


def load_policy() -> dict:
    return _read_json("policy.json")


@dataclass(frozen=True)
class Observation:
    seq: int
    device: str
    kind: str
    at: float
    data: dict = field(default_factory=dict)

    @property
    def arrived(self) -> float:
        """回放中实际交付该记录的时刻；缺省与 at 相同。"""
        return float(self.data.get("arrived", self.at))

    @property
    def refs(self) -> tuple[int, ...]:
        return tuple(self.data.get("refs", ()))

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key: str):
        return self.data[key]

    def __contains__(self, key: str) -> bool:
        return key in self.data


def load_scene(path: str | Path | None = None) -> list[Observation]:
    path = Path(path) if path else ROOT / "observations.ndjson"
    with path.open(encoding="utf-8") as source:
        raw = [json.loads(line) for line in source if line.strip()]
    if [row["seq"] for row in raw] != list(range(1, len(raw) + 1)):
        raise ValueError("观测序号须连续")
    if any(not row.get("device") or not row.get("kind") for row in raw):
        raise ValueError("观测须有设备和事件类型")
    rows: list[Observation] = []
    for row in raw:
        data = {key: value for key, value in row.items() if key not in ("seq", "device", "kind", "at")}
        rows.append(Observation(row["seq"], row["device"], row["kind"], float(row.get("at", 0)), data))
    return rows

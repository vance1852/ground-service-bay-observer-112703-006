"""占用归属、作业准入与复核的判定引擎。

判定不保存一条总的“在线”状态，而是在每次查询时按物理时间重算：

* 结构证据（门侧确认的进出、夹具实际连接、负责人核定的对象）决定“是哪台车”；
* 压力只证明“有人占用”，不证明身份；
* 近场读头的候选位和强度是弱证据，不能单独归属一辆车；
* 故障声明只取消对应源族从声明时刻起的证明力，不影响其它源已经确认的事实；
* 旧 source_epoch 的观察补达时只作历史，不回写当前现场。

空的判定需要空位依据（低压或同一实物确证离位），并且不能被更晚的占用观察
或可能正在进入的车辆覆盖。负责人空位凭据只覆盖其载明的工位与时间范围。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from .scene import Observation

EMPTY_DEPARTURE = "confirmed_departure"
EMPTY_LOW = "low_pressure"
EMPTY_MANUAL = "manual_absence"
OPEN_JOB = ("pending", "held", "active", "pending_review", "suspended")
CLOSED_JOB = ("completed", "stopped", "cancelled")


@dataclass(frozen=True)
class Evidence:
    seq: int
    at: float
    arrived: float
    source: str
    kind: str
    effect: str
    strength: str
    detail: str
    bay: str | None = None
    unit: str | None = None
    window: tuple[float, float] | None = None

    def ref(self) -> dict:
        return {
            "seq": self.seq,
            "at": self.at,
            "arrived": self.arrived,
            "source": self.source,
            "effect": self.effect,
            "strength": self.strength,
            "detail": self.detail,
            "bay": self.bay,
            "unit": self.unit,
            "window": list(self.window) if self.window else None,
        }


@dataclass
class Fault:
    seq: int
    device: str
    bay: str
    source_epoch: float
    status: str
    at: float
    arrived: float
    recovered_at: float | None = None

    def active(self, at: float) -> bool:
        if at < self.at:
            return False
        return self.recovered_at is None or at < self.recovered_at


@dataclass(frozen=True)
class Structure:
    unit: str | None
    bay: str
    kind: str  # arrive | depart | bond
    at: float
    seq: int
    counterpart: str | None = None

    @property
    def key(self) -> tuple[float, int, int]:
        # 同一时刻的到达/连接优先于离位，避免把刚接上的车误判为已离开。
        return (self.at, self.seq, 1 if self.kind in ("arrive", "bond") else 0)

    def ref(self) -> dict:
        effect = {"arrive": "move_arrival", "depart": "move_departure", "bond": "bond_ok"}[self.kind]
        detail = {
            "arrive": f"门侧确认 {self.unit or '未识别车辆'} 进入 {self.bay}",
            "depart": f"门侧确认 {self.unit or '未识别车辆'} 离开 {self.bay}",
            "bond": f"{self.bay} 接地夹实际连到 {self.unit}",
        }[self.kind]
        return {"seq": self.seq, "at": self.at, "source": "gate", "effect": effect,
                "strength": "confirmed", "detail": detail, "bay": self.bay, "unit": self.unit}


@dataclass(frozen=True)
class ManualClaim:
    seq: int
    at: float
    arrived: float
    label: str
    bay: str
    kind: str  # empty | clear_surface | present
    unit: str | None
    window: tuple[float, float]

    @property
    def key(self) -> tuple[float, int]:
        return (self.at, self.seq)

    def valid(self, at: float) -> bool:
        return self.window[0] <= at <= self.window[1]


@dataclass(frozen=True)
class RadioReport:
    seq: int
    at: float
    arrived: float
    unit: str
    rssi: dict[str, float]
    strong: tuple[str, ...]

    @property
    def key(self) -> tuple[float, int]:
        return (self.at, self.seq)

    def ref(self) -> dict:
        return {"seq": self.seq, "at": self.at, "arrived": self.arrived, "source": "reader",
                "effect": "radio_candidate", "strength": "observed",
                "detail": f"读头读到 {self.unit}：{', '.join(f'{b} {v:.0f} dBm' for b, v in self.rssi.items())}",
                "bay": self.strong[0] if self.strong else None, "unit": self.unit}


@dataclass
class Job:
    id: str
    bay: str
    unit: str | None
    type: str
    requested_at: float
    requested_seq: int
    status: str = "pending"
    history: list[dict] = field(default_factory=list)

    @property
    def started(self) -> bool:
        return any(item["to"] == "active" for item in self.history)

    def transition(self, status: str, at: float, seq: int, reason: str,
                   checks: list[dict], evidence: list[int]) -> bool:
        if status == self.status:
            return False
        self.history.append(
            {
                "from": self.status,
                "to": status,
                "at": at,
                "seq": seq,
                "reason": reason,
                "checks": checks,
                "evidence": evidence,
            }
        )
        self.status = status
        return True


class YardEngine:
    """接入观测并维护工位与作业的当前判定。"""

    def __init__(self, rows: list[Observation], devices: dict[str, dict], policy: dict):
        self.devices = devices
        self.policy = policy
        self.bays: list[str] = list(policy["bays"])
        self.max_age = float(policy.get("auto_obs_max_age", 50))
        self.reader_margin = float(policy.get("reader_margin", 6))
        self.now = 0.0
        self.seq = 0
        self.evidence: dict[str, list[Evidence]] = {bay: [] for bay in self.bays}
        self.structure: list[Structure] = []
        self.manual: list[ManualClaim] = []
        self.radio: list[RadioReport] = []
        self.faults: list[Fault] = []
        self.attempts: list[Evidence] = []
        self.latest_epoch: dict[str, tuple[float, float]] = {}
        self.jobs: dict[str, Job] = {}
        self.notices: list[dict] = []
        self.rejected: list[dict] = []
        for row in rows:
            self.ingest(row)

    # ---------------------------------------------------------------- 接入

    def ingest(self, obs: Observation) -> dict:
        """接入一条观测，并在结算后重估所有已开工/已申请的作业。"""
        if obs.seq > self.seq:
            self.seq = obs.seq
        self.now = max(self.now, obs.arrived)
        device = self.devices.get(obs.device, {})
        kind = obs.kind
        if kind in ("pressure", "radio", "move", "bond", "fault"):
            if not device:
                return self._reject(obs, "设备未登记")
            covered = set(device.get("observes", []))
            if kind == "move":
                involved = {obs.get("from"), obs.get("to")} - {"yard", None}
                if not involved <= covered:
                    return self._reject(obs, "门侧设备未覆盖涉及的工位")
            elif kind == "radio":
                if not set(obs.get("rssi", {})) <= covered:
                    return self._reject(obs, "读头未覆盖回报的候选位")
            elif kind in ("pressure", "fault", "bond") and obs.get("bay") not in covered:
                return self._reject(obs, "设备未覆盖该工位")
        if kind in ("pressure", "radio", "fault") and self._is_stale(obs):
            self._remember_epoch(obs)
            return self._reject(obs, "旧 source_epoch 的观察在更新会话之后补达，只作历史，不改现场")
        handler = {
            "pressure": self._pressure,
            "radio": self._radio,
            "move": self._move,
            "bond": self._bond,
            "fault": self._fault,
            "site_check": self._site_check,
            "work": self._work,
        }.get(kind)
        if handler is None:
            return self._reject(obs, f"未知观测类型 {kind}")
        try:
            result = handler(obs) or {"seq": obs.seq, "accepted": True, "kind": kind}
        except (KeyError, TypeError, ValueError) as error:
            return self._reject(obs, f"观测字段不完整：{error}")
        self._recheck_all(self.now, obs.seq)
        return result

    def refresh(self) -> None:
        """时间推进后重估（例如负责人凭据的时间范围已过）。"""
        self._recheck_all(self.now, 0)

    def _reject(self, obs: Observation, reason: str) -> dict:
        entry = {"seq": obs.seq, "accepted": False, "kind": obs.kind, "reason": reason}
        self.rejected.append(entry)
        return entry

    def _remember_epoch(self, obs: Observation) -> None:
        epoch = obs.get("source_epoch")
        if epoch is None:
            return
        current = self.latest_epoch.get(obs.device)
        if current is None or float(epoch) >= current[0]:
            self.latest_epoch[obs.device] = (float(epoch), obs.arrived)

    def _is_stale(self, obs: Observation) -> bool:
        epoch = obs.get("source_epoch")
        if epoch is None:
            return False
        epoch = float(epoch)
        current = self.latest_epoch.get(obs.device)
        if current and epoch < current[0] and obs.arrived > current[1]:
            return True
        for fault in self.faults:
            if fault.device == obs.device and fault.source_epoch >= epoch and obs.arrived > fault.at:
                return True
        return False

    # ------------------------------------------------------------ 传感器

    def _pressure(self, obs: Observation) -> dict:
        bay = obs["bay"]
        self._remember_epoch(obs)
        self._maybe_recover(obs)
        present = bool(obs.get("present"))
        ev = Evidence(
            obs.seq, obs.at, obs.arrived, obs.device, "pressure",
            "pressure_present" if present else "pressure_low",
            "observed",
            "压力接触" if present else "压力释放",
            bay=bay,
        )
        self.evidence[bay].append(ev)
        return {"seq": obs.seq, "accepted": True, "kind": "pressure", "bay": bay, "present": present}

    def _maybe_recover(self, obs: Observation) -> None:
        epoch = obs.get("source_epoch")
        if epoch is None:
            return
        epoch = float(epoch)
        for fault in self.faults:
            if (
                fault.device == obs.device
                and fault.bay == obs.get("bay")
                and fault.recovered_at is None
                and epoch > fault.source_epoch
            ):
                fault.recovered_at = float(obs.arrived)
                self.notices.append(
                    {
                        "kind": "source_recovered",
                        "at": obs.arrived,
                        "seq": obs.seq,
                        "device": obs.device,
                        "bay": fault.bay,
                        "reason": f"source_epoch {epoch:g} 晚于故障声明 {fault.source_epoch:g}，该源恢复证明力",
                    }
                )

    def _apply_site_check(self, obs: Observation, claims: dict) -> list[dict]:
        """把负责人凭据应用到受影响的作业；只在这些作业载明的工位范围内。"""
        applied = []
        for job in list(self.jobs.values()):
            if job.bay not in claims or job.status in CLOSED_JOB:
                continue
            applied.append({"job": job.id, "bay": job.bay, "seq": obs.seq,
                            "result": self.check(job.bay, job.type, job.unit)})
        return applied

    def _radio(self, obs: Observation) -> dict:
        rssi = {bay: float(value) for bay, value in obs.get("rssi", {}).items() if bay in self.bays}
        if not rssi:
            raise ValueError("没有覆盖范围内的候选位")
        unit = obs.get("unit")
        if not unit:
            raise ValueError("读头记录缺少车辆标识")
        top = max(rssi.values())
        strong = tuple(sorted(bay for bay, value in rssi.items() if value >= top - self.reader_margin))
        self._remember_epoch(obs)
        self.radio.append(RadioReport(obs.seq, obs.at, obs.arrived, unit, rssi, strong))
        for bay, value in rssi.items():
            self.evidence[bay].append(
                Evidence(
                    obs.seq, obs.at, obs.arrived, obs.device, "radio", "radio_candidate",
                    "observed" if bay in strong else "weak",
                    f"近场读头读到 {unit}，{bay} 强度 {value:.0f} dBm",
                    bay=bay, unit=unit,
                )
            )
        result = {"seq": obs.seq, "accepted": True, "kind": "radio", "unit": unit,
                  "candidates": list(strong)}
        location = self.unit_location(unit, obs.at)
        if location and location not in strong:
            result["note"] = f"读头与已确认位置 {location} 不一致，按结构证据处理"
        elif len(strong) > 1 and not location:
            result["ambiguous"] = True
            result["note"] = "候选位强度接近，强度没有唯一归属含义，保留冲突"
        elif not location and not self._bay_has_occupancy(strong[0], obs.at):
            result["weak_only"] = True
            result["note"] = "读头单独读数不能作为占用或身份证据"
        return result

    def _move(self, obs: Observation) -> dict:
        start, target = obs.get("from"), obs.get("to")
        unit = obs.get("unit")
        if start == target:
            raise ValueError("move 起止相同")
        if (start not in self.bays and start != "yard") or (target not in self.bays and target != "yard"):
            raise ValueError("move 缺少有效起止工位")
        if not obs.get("present"):
            text = f"门侧报告 {unit or '未知车辆'} 从 {start} 进 {target} 未完成"
            for bay, side in ((target, "进"), (start, "出")):
                if bay in self.bays:
                    ev = Evidence(obs.seq, obs.at, obs.arrived, obs.device, "move", "move_attempt",
                                  "weak", text, bay=bay, unit=unit)
                    self.evidence[bay].append(ev)
                    if side == "进":
                        self.attempts.append(ev)
            return {"seq": obs.seq, "accepted": True, "kind": "move", "confirmed": False,
                    "note": "未确认的迁位不改变占用，只记为可能正在进出"}

        if target in self.bays:
            self.structure.append(Structure(unit, target, "arrive", obs.at, obs.seq,
                                            counterpart=start if start in self.bays else None))
            self.evidence[target].append(
                Evidence(obs.seq, obs.at, obs.arrived, obs.device, "move", "move_arrival",
                         "confirmed", f"门侧确认 {unit or '未识别车辆'} 进入 {target}",
                         bay=target, unit=unit)
            )
        if start in self.bays:
            self.structure.append(Structure(unit, start, "depart", obs.at, obs.seq,
                                            counterpart=target if target in self.bays else None))
            self.evidence[start].append(
                Evidence(obs.seq, obs.at, obs.arrived, obs.device, "move", "move_departure",
                         "confirmed", f"门侧确认 {unit or '未识别车辆'} 离开 {start}",
                         bay=start, unit=unit)
            )
        return {"seq": obs.seq, "accepted": True, "kind": "move", "confirmed": True,
                "from": start, "to": target, "unit": unit}

    def _bond(self, obs: Observation) -> dict:
        bay = obs["bay"]
        unit = obs.get("unit")
        if not unit:
            raise ValueError("bond 缺少车辆标识")
        if not obs.get("ok"):
            self.evidence[bay].append(
                Evidence(obs.seq, obs.at, obs.arrived, obs.device, "bond", "bond_failed",
                         "observed", f"{bay} 接地夹未连到 {unit}", bay=bay, unit=unit)
            )
            return {"seq": obs.seq, "accepted": True, "kind": "bond", "bay": bay,
                    "unit": unit, "ok": False}
        previous = self.unit_location(unit, obs.at)
        if previous and previous != bay:
            self.structure.append(Structure(unit, previous, "depart", obs.at, obs.seq,
                                            counterpart=bay))
            self.evidence[previous].append(
                Evidence(obs.seq, obs.at, obs.arrived, obs.device, "bond", "move_departure",
                         "confirmed", f"{unit} 的接地夹已在 {bay}，{previous} 不再有该车",
                         bay=previous, unit=unit)
            )
        self.structure.append(Structure(unit, bay, "bond", obs.at, obs.seq))
        self.evidence[bay].append(
            Evidence(obs.seq, obs.at, obs.arrived, obs.device, "bond", "bond_ok",
                     "confirmed", f"{bay} 接地夹实际连到 {unit}", bay=bay, unit=unit)
        )
        return {"seq": obs.seq, "accepted": True, "kind": "bond", "bay": bay,
                "unit": unit, "ok": True}

    def _fault(self, obs: Observation) -> dict:
        bay = obs["bay"]
        device = obs.device
        epoch = float(obs.get("source_epoch", 0))
        if epoch < self.latest_epoch.get(device, (0.0, 0.0))[0]:
            return {"seq": obs.seq, "accepted": False,
                    "reason": "故障声明早于该源当前会话，按补达旧观察处理"}
        self._remember_epoch(obs)
        self.faults.append(
            Fault(obs.seq, device, bay, epoch, obs.get("status", "degraded"), obs.at, obs.arrived)
        )
        self.evidence[bay].append(
            Evidence(obs.seq, obs.at, obs.arrived, device, "fault", "source_fault",
                     "confirmed", f"{device} 在 {bay} 声明 {obs.get('status', 'degraded')}", bay=bay)
        )
        self.notices.append(
            {
                "kind": "source_fault",
                "at": obs.arrived,
                "seq": obs.seq,
                "device": device,
                "bay": bay,
                "status": obs.get("status", "degraded"),
                "reason": f"该源自 {obs.at:g} 起不再作为 {bay} 的证据，须按剩余证据重估",
            }
        )
        return {"seq": obs.seq, "accepted": True, "kind": "fault", "device": device, "bay": bay}

    # ------------------------------------------------------------ 人工凭据

    def _site_check(self, obs: Observation) -> dict:
        claims = obs.get("claims")
        if not isinstance(claims, dict) or not claims:
            raise ValueError("site_check 须载明 claims")
        window = (float(obs.get("from", obs.at)), float(obs.get("until", obs.at)))
        if window[1] < window[0]:
            raise ValueError("site_check 时间范围颠倒")
        label = obs.get("ref") or f"lead-{obs.seq}"
        touched, ignored = [], []
        for bay, claim in claims.items():
            if bay not in self.bays or not isinstance(claim, dict):
                ignored.append(bay)
                continue
            unit = claim.get("unit")
            if claim.get("empty"):
                self.manual.append(ManualClaim(obs.seq, obs.at, obs.arrived, label, bay,
                                               "empty", unit, window))
                self.evidence[bay].append(
                    Evidence(obs.seq, obs.at, obs.arrived, "lead", "site_check", "manual_absence",
                             "confirmed", f"{label} 核定 {bay} 在 {window[0]:g}–{window[1]:g} 为空",
                             bay=bay, unit=unit, window=window)
                )
            if claim.get("clear_surface"):
                self.manual.append(ManualClaim(obs.seq, obs.at, obs.arrived, label, bay,
                                               "clear_surface", unit, window))
                self.evidence[bay].append(
                    Evidence(obs.seq, obs.at, obs.arrived, "lead", "site_check", "manual_clear",
                             "confirmed", f"{label} 核定 {bay} 台面已扫除（{window[0]:g}–{window[1]:g}）",
                             bay=bay, unit=unit, window=window)
                )
            if claim.get("occupied") or unit:
                self.manual.append(ManualClaim(obs.seq, obs.at, obs.arrived, label, bay,
                                               "present", unit, window))
                self.evidence[bay].append(
                    Evidence(obs.seq, obs.at, obs.arrived, "lead", "site_check", "manual_presence",
                             "confirmed",
                             f"{label} 核定 {bay} 有{(' ' + unit) if unit else ''}车占用",
                             bay=bay, unit=unit, window=window)
                )
            touched.append(bay)
        return {
            "seq": obs.seq,
            "accepted": True,
            "kind": "site_check",
            "touched": touched,
            "ignored": ignored,
            "window": list(window),
            "affected": self._apply_site_check(obs, {bay: claims[bay] for bay in touched}),
            "note": "复核凭据只覆盖其载明的工位与时间范围，未载明的工位保持原状",
        }

    # ------------------------------------------------------------ 作业请求

    def _work(self, obs: Observation) -> dict:
        job_id = obs.get("id")
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("work 缺少作业标识")
        stage = obs.get("stage", "start")
        if stage in ("stop", "complete", "cancel"):
            job = self.jobs.get(job_id)
            if not job:
                return {"seq": obs.seq, "accepted": False, "reason": "没有该作业"}
            target = {"stop": "stopped", "complete": "completed", "cancel": "cancelled"}[stage]
            if job.transition(target, obs.arrived, obs.seq, "班组结束作业", [], []):
                self.notices.append({"kind": "work", "at": obs.arrived, "seq": obs.seq,
                                     "job": job_id, "to": target, "reason": "班组结束作业"})
            return {"seq": obs.seq, "accepted": True, "kind": "work", "job": job_id,
                    "status": job.status}
        bay = obs.get("bay")
        if bay not in self.bays:
            raise ValueError("作业工位不在配置内")
        if job_id in self.jobs and self.jobs[job_id].status not in CLOSED_JOB:
            raise ValueError("作业标识已存在")
        job = Job(job_id, bay, obs.get("unit"), obs.get("type", ""), obs.arrived, obs.seq)
        self.jobs[job_id] = job
        verdict = self.check(bay, job.type, job.unit, at=obs.arrived)
        status = "active" if verdict["ok"] else "held"
        job.transition(status, obs.arrived, obs.seq, verdict["reason"], verdict["checks"],
                       verdict["evidence"])
        self._announce(job, obs.arrived, obs.seq, verdict)
        return {"seq": obs.seq, "accepted": True, "kind": "work", "job": job_id,
                "status": status, "verdict": verdict}

    def _announce(self, job: Job, at: float, seq: int, verdict: dict) -> None:
        self.notices.append(
            {
                "kind": "work",
                "at": at,
                "seq": seq,
                "job": job.id,
                "to": job.status,
                "reason": verdict["reason"],
                "checks": verdict["checks"],
                "evidence": verdict["evidence"],
            }
        )

    def _recheck_all(self, at: float, seq: int) -> None:
        for job in list(self.jobs.values()):
            if job.status not in OPEN_JOB:
                continue
            if job.requested_seq == seq and seq != 0:
                continue
            self._recheck(job, at, seq)

    def _recheck(self, job: Job, at: float, seq: int) -> None:
        if job.status in CLOSED_JOB:
            return
        verdict = self.check(job.bay, job.type, job.unit, at=at)
        if verdict["ok"]:
            target = "active"
        elif job.started:
            proposed = "pending_review" if verdict["review"] else "suspended"
            severity = {"pending_review": 1, "suspended": 2}
            if job.status == "suspended" and severity[proposed] < severity["suspended"]:
                return
            target = proposed
        else:
            target = "held"
        if job.transition(target, at, seq, verdict["reason"], verdict["checks"], verdict["evidence"]):
            self._announce(job, at, seq, verdict)

    # ------------------------------------------------------------ 结构推导

    def unit_location(self, unit: str, at: float | None = None) -> str | None:
        at = self.now if at is None else at
        events = [item for item in self.structure if item.unit == unit and item.at <= at]
        if not events:
            return None
        latest = max(events, key=lambda item: item.key)
        return latest.bay if latest.kind in ("arrive", "bond") else None

    def units_at(self, bay: str, at: float) -> list[tuple[str, Structure]]:
        found = []
        units = {item.unit for item in self.structure if item.unit and item.at <= at}
        for unit in units:
            events = [item for item in self.structure if item.unit == unit and item.at <= at]
            latest = max(events, key=lambda item: item.key)
            if latest.bay == bay and latest.kind in ("arrive", "bond"):
                found.append((unit, latest))
        return sorted(found, key=lambda pair: pair[0])

    def _last(self, bay: str, at: float, kinds: tuple[str, ...]) -> Structure | None:
        events = [item for item in self.structure if item.bay == bay and item.at <= at and item.kind in kinds]
        return max(events, key=lambda item: item.key) if events else None

    def _pressure_at(self, bay: str, at: float) -> tuple[str, Evidence] | None:
        readings = [item for item in self.evidence[bay] if item.kind == "pressure" and item.at <= at]
        if not readings:
            return None
        latest = max(readings, key=lambda item: (item.at, item.seq))
        if not self._reading_alive(latest, at):
            return None
        if latest.effect == "pressure_present":
            # 已声明故障的源仍报接触时，只能作为“接触尚未澄清”，不能继续证明谁在位。
            faulted = any(
                fault.device == latest.source and fault.bay == bay and fault.at >= latest.at
                for fault in self.faults
            )
            return ("faulted_contact" if faulted else "present", latest)
        # 低压读数须在其后没有更晚的占用观察时才能作为当前空位依据。
        latest_presence = self._latest_presence(bay, at)
        if latest_presence is not None and latest_presence.at > latest.at:
            faulted = any(
                fault.device == latest_presence.source and fault.bay == bay
                and fault.at >= latest_presence.at
                for fault in self.faults
            )
            return ("faulted_contact" if faulted else "present", latest_presence)
        return ("low", latest)

    def _latest_presence(self, bay: str, at: float) -> Evidence | None:
        readings = [
            item for item in self.evidence[bay]
            if item.kind == "pressure" and item.effect == "pressure_present" and item.at <= at
            and self._reading_alive(item, at)
        ]
        if not readings:
            return None
        return max(readings, key=lambda item: (item.at, item.seq))

    def _reading_alive(self, reading: Evidence, at: float) -> bool:
        """压力读数在时限内且未被源故障取消时仍然作数。"""
        if at - reading.at > self.max_age:
            return False
        return not any(
            fault.device == reading.source
            and fault.bay == reading.bay
            and fault.at >= reading.at
            and fault.at <= at
            for fault in self.faults
        )

    def _pressure_reading(self, bay: str) -> Evidence | None:
        readings = [item for item in self.evidence[bay] if item.kind == "pressure"]
        return max(readings, key=lambda item: (item.at, item.seq)) if readings else None

    def _manual_at(self, bay: str, at: float, kind: str) -> ManualClaim | None:
        claims = [item for item in self.manual if item.bay == bay and item.kind == kind and item.valid(at)]
        return max(claims, key=lambda item: item.key) if claims else None

    def _attempt_at(self, bay: str, at: float) -> Evidence | None:
        attempts = [
            item for item in self.attempts
            if item.bay == bay and at - item.arrived <= self.max_age
        ]
        if not attempts:
            return None
        latest = max(attempts, key=lambda item: (item.arrived, item.seq))
        for item in self.structure:
            if item.bay == bay and item.seq > latest.seq:
                return None
        for claim in self.manual:
            if claim.bay == bay and claim.seq > latest.seq:
                return None
        return latest

    def _radio_issue(self, bay: str, at: float) -> dict | None:
        reports = [item for item in self.radio if item.at <= at]
        if not reports:
            return None
        latest_per_unit: dict[str, RadioReport] = {}
        for report in reports:
            current = latest_per_unit.get(report.unit)
            if current is None or report.key > current.key:
                latest_per_unit[report.unit] = report
        issue = None
        for unit, report in latest_per_unit.items():
            if bay not in report.strong:
                continue
            if self.unit_location(unit, at):
                continue
            if len(report.strong) > 1:
                issue = {
                    "unit": unit,
                    "bays": list(report.strong),
                    "reason": f"读头候选位 {', '.join(report.strong)} 强度接近，强度没有唯一归属含义",
                    "seq": report.seq,
                }
            elif issue is None:
                issue = {
                    "unit": unit,
                    "bays": [bay],
                    "reason": f"读头单独读到 {unit}，缺少独立位置证据",
                    "seq": report.seq,
                }
        return issue

    def _bay_has_occupancy(self, bay: str, at: float) -> bool:
        if self.units_at(bay, at):
            return True
        pressure = self._pressure_at(bay, at)
        return bool(pressure and pressure[0] == "present")

    # ------------------------------------------------------------ 工位判定

    def view(self, bay: str, at: float | None = None) -> dict:
        at = self.now if at is None else at
        if bay not in self.evidence:
            raise KeyError(bay)
        evidence = self.evidence[bay]
        degraded = {
            fault.device: f"{fault.status}，自 {fault.at:g} 起不再作为该位证据"
            for fault in self.faults
            if fault.bay == bay and fault.active(at)
        }
        conflicts: list[dict] = []
        presence = self.units_at(bay, at)
        manual_empty = self._manual_at(bay, at, "empty")
        manual_present = self._manual_at(bay, at, "present")
        pressure = self._pressure_at(bay, at)
        last_arrival = self._last(bay, at, ("arrive", "bond"))
        last_departure = self._last(bay, at, ("depart",))
        last_move = max(
            [item for item in (last_arrival, last_departure) if item],
            key=lambda item: item.key,
            default=None,
        )

        def base(status: str, **extra) -> dict:
            return {"bay": bay, "at": at, "status": status, "degraded": degraded,
                    "conflicts": conflicts, **extra}

        if len(presence) > 1:
            units = ", ".join(unit for unit, _ in presence)
            return base("conflict", identified=False, unit=None,
                        conflict=f"同一工位出现多台确认车辆：{units}",
                        evidence=[item.ref() for _, item in presence])
        if manual_empty and presence and manual_empty.seq < presence[0][1].seq:
            conflicts.append({"kind": "manual_vs_structure", "seq": manual_empty.seq,
                              "text": "负责人空位凭据早于之后进入的结构占用，按新占用处理"})
        elif manual_empty and presence and not (
            manual_empty.at >= presence[0][1].at and manual_empty.valid(at)
        ):
            conflicts.append({"kind": "manual_vs_structure", "seq": manual_empty.seq,
                              "text": "负责人空位凭据与结构占用并存，保留占用"})
        if (
            presence and last_departure is not None and last_departure.unit is None
            and last_departure.key > presence[0][1].key
            and (last_arrival is None or last_departure.key > last_arrival.key)
        ):
            conflicts.append({"kind": "unidentified_departure", "seq": last_departure.seq,
                              "text": "门侧报告有车离开但未识别车辆，不能据此释放已在位的车"})
        if (
            presence and last_arrival is not None and last_arrival.unit is None
            and last_arrival.key > presence[0][1].key
        ):
            conflicts.append({"kind": "unidentified_arrival", "seq": last_arrival.seq,
                              "text": "门侧报告有第二台车进入但未识别车辆，不能确定该位只有一台车"})
        attempt = self._attempt_at(bay, at)
        if presence and attempt:
            conflicts.append({"kind": "possible_entry", "seq": attempt.seq,
                              "text": "门侧报告可能有另一台车正在进入，该位仍按已确认占用处理"})
        if presence:
            unit, event = presence[0]
            # 负责人按位置、车辆与时段核定“有另一台车”时，晚于结构观察的凭据可以更正归属。
            if (
                manual_present and manual_present.unit
                and manual_present.unit != unit
                and manual_present.at >= event.at
                and manual_present.seq > event.seq
                and manual_present.valid(at)
            ):
                return base("occupied", identified=True, unit=manual_present.unit,
                            evidence=[self._manual_ref(bay, manual_present)], source="lead",
                            note=f"负责人凭据更正了先前的 {unit} 归属")
            # 负责人按位置、车辆与时段核定的空位凭据，晚于结构占用时可以作为复核结果；
            # 但只能作用于它载明的这一位，不能扩及其他工位。
            if manual_empty and manual_empty.seq > event.seq and manual_empty.at >= event.at and manual_empty.valid(at):
                return base("empty", identified=False, unit=None, basis=EMPTY_MANUAL,
                            evidence=[self._manual_ref(bay, manual_empty)],
                            note=f"负责人凭据已复核先前的 {unit} 占用，按核定结果处理")
            refs = [event.ref()] + [
                item.ref() for item in evidence
                if item.effect in ("radio_candidate", "bond_ok") and item.unit == unit
            ]
            unique = {ref["seq"]: ref for ref in refs}
            return base("occupied", identified=True, unit=unit, evidence=list(unique.values()))
        if manual_present:
            refs = [self._manual_ref(bay, manual_present)]
            if pressure:
                refs.append(pressure[1].ref())
            return base("occupied", identified=bool(manual_present.unit), unit=manual_present.unit,
                        evidence=refs, source="lead")
        if pressure and pressure[0] == "faulted_contact":
            reading = pressure[1]
            if last_departure is not None and (
                last_arrival is None or last_departure.key > last_arrival.key
            ):
                return base("conflict", identified=False, unit=None,
                            conflict="压力源已声明故障但仍报接触，与门侧离位矛盾，须保留两方证据",
                            evidence=[reading.ref(), last_departure.ref()])
            return base("unknown", identified=False, unit=None,
                        note="压力源已声明故障，读数不能作为当前占用或身份证据",
                        evidence=[reading.ref()])
        if pressure and pressure[0] == "present":
            reading = pressure[1]
            if manual_empty and manual_empty.seq > reading.seq:
                conflicts.append({"kind": "manual_vs_pressure", "seq": manual_empty.seq,
                                  "text": "负责人空位凭据与尚未释放的压力占用并存，保留压力占用"})
            if last_move and last_move.kind == "depart" and last_departure and last_departure.key > (
                last_arrival.key if last_arrival else (-1, -1)
            ):
                conflicts.append({"kind": "pressure_vs_departure", "seq": reading.seq,
                                  "text": "压力仍报占用，但门侧已确认同一位置离位；疑似卡高，保留冲突"})
                return base("conflict", identified=False, unit=None,
                            conflict="压力占用与门侧确认离位矛盾，需保留两方证据",
                            evidence=[reading.ref(), last_departure.ref()])
            return base("occupied", identified=False, unit=None,
                        note="压力证明占用，但没有独立读头或门侧证据确认是哪台车",
                        evidence=[reading.ref()])
        if last_move and last_move.kind == "arrive" and last_move.unit is None:
            return base("occupied", identified=False, unit=None,
                        note="门侧看到有车进入但未识别车辆",
                        evidence=[last_move.ref()])
        raw_pressure = self._pressure_reading(bay)
        faulted_contact = (
            raw_pressure is not None
            and raw_pressure.effect == "pressure_present"
            and raw_pressure.at <= at
            and at - raw_pressure.at <= self.max_age
            and any(
                fault.device == raw_pressure.source and fault.bay == bay and fault.at >= raw_pressure.at
                for fault in self.faults
            )
            and not (
                last_arrival is not None
                and last_departure is not None
                and last_arrival.key > last_departure.key
                and last_arrival.at >= raw_pressure.at
            )
        )
        departed = (
            last_departure is not None
            and last_departure.unit is not None
            and (last_arrival is None or last_departure.key > last_arrival.key)
        )
        unidentified_departure = (
            last_departure is not None
            and last_departure.unit is None
            and (last_arrival is None or last_departure.key > last_arrival.key)
        )
        manual_verdict = None
        if manual_empty:
            refs = [self._manual_ref(bay, manual_empty)]
            if pressure and pressure[0] == "low":
                refs.append(pressure[1].ref())
            manual_verdict = base("empty", identified=False, unit=None, basis=EMPTY_MANUAL,
                                  evidence=refs)
        if departed and faulted_contact:
            # 声明卡高的源在离位后仍报接触时，门侧与压力的证据互相矛盾；只有在负责人按
            # 位置、车辆与时段给出凭据，或之后有明确结构观察时才解除这一冲突。
            if not (manual_empty and manual_empty.seq > raw_pressure.seq
                    and manual_empty.seq > last_departure.seq and manual_empty.valid(at)):
                return base("conflict", identified=False, unit=None,
                            conflict="压力源在声明卡高前后都报接触，与门侧离位矛盾，须保留两方证据",
                            evidence=[last_departure.ref(), raw_pressure.ref()])
            conflicts.append({"kind": "manual_resolves_pressure", "seq": manual_empty.seq,
                              "text": "负责人凭据解除了卡高压力与门侧离位的冲突"})
        if attempt:
            return base("conflict", identified=False, unit=None,
                        conflict="门侧报告可能有车正在进入，暂不能视为空位",
                        evidence=[attempt.ref()])
        if manual_verdict and departed:
            return manual_verdict
        if departed:
            # 迁位同时影响旧位与新位：只有同一实物被看到真正离开，旧位才可释放。
            crossing = None
            if last_departure.counterpart and last_departure.unit:
                crossing = [
                    item for item in self.radio
                    if item.at <= at
                    and item.at >= last_departure.at
                    and item.unit == last_departure.unit
                    and last_departure.counterpart in item.strong
                    and bay in item.strong
                ]
            if crossing:
                if manual_verdict:
                    return manual_verdict
                return base("conflict", identified=False, unit=None,
                            conflict=f"读头在迁位后同时报旧位 {bay} 与新位 {last_departure.counterpart}，"
                                     "不能单凭门侧离位释放旧位",
                            evidence=[last_departure.ref()] + [read.ref() for read in crossing])
        if manual_verdict:
            if self._radio_issue(bay, at):
                conflicts.append({"kind": "weak_vs_manual", "seq": manual_verdict["evidence"][0]["seq"],
                                  "text": "负责人已核定空位，其后到达的弱读头读数不改写这一状态"})
            return manual_verdict
        if unidentified_departure:
            return base("unknown", identified=False, unit=None,
                        note="门侧看到有车离开但未识别车辆，无法证明先前的车已经离位",
                        evidence=[last_departure.ref()])
        if departed:
            refs = [last_departure.ref()]
            return base("empty", identified=False, unit=None, basis=EMPTY_DEPARTURE,
                        evidence=refs)
        issue = self._radio_issue(bay, at)
        if pressure and pressure[0] == "low":
            independent = last_move is not None and last_move.key >= pressure[1].key
            if independent:
                return base("empty", identified=False, unit=None, basis=EMPTY_LOW,
                            evidence=[pressure[1].ref(), last_move.ref()])
            return base("unknown", identified=False, unit=None,
                        note="压力释放，但缺少独立的门侧或限定时段人工观察",
                        evidence=[pressure[1].ref()])
        if issue:
            refs = [
                item.ref() for item in evidence
                if item.effect == "radio_candidate" and item.unit == issue["unit"]
            ]
            return base("conflict", identified=False, unit=None, conflict=issue["reason"],
                        evidence=refs)
        readers = [item.ref() for item in evidence if item.effect == "radio_candidate"]
        return base("unknown", identified=False, unit=None,
                    note="没有足以判定占用或空位的独立观察",
                    evidence=readers)

    def _manual_ref(self, bay: str, claim: ManualClaim) -> dict:
        for item in self.evidence[bay]:
            if item.seq == claim.seq and item.kind == "site_check" and item.effect in (
                "manual_absence", "manual_clear", "manual_presence"
            ):
                if (claim.kind == "empty" and item.effect == "manual_absence") or (
                    claim.kind == "present" and item.effect == "manual_presence"
                ) or (claim.kind == "clear_surface" and item.effect == "manual_clear"):
                    return item.ref()
        raise KeyError("缺少人工凭据证据")

    # ------------------------------------------------------------ 作业判定

    def check(self, bay: str, kind: str, unit: str | None = None,
              at: float | None = None) -> dict:
        """核对一项作业此刻是否具备条件；不创建作业、不改变现场。"""
        at = self.now if at is None else at
        if bay not in self.evidence:
            return {"ok": False, "reason": f"工位 {bay} 未接入", "checks": [], "evidence": [],
                    "review": False}
        needs = self.policy.get("work", {}).get(kind, {}).get("needs")
        if needs is None:
            return {"ok": False, "reason": f"作业类型 {kind or '(空)'} 尚未配置准入条件",
                    "checks": [{"need": "configured", "ok": False, "reason": "policy.work 中没有该类型"}],
                    "evidence": [], "review": False}
        attempt = self._attempt_at(bay, at)
        if attempt:
            return {"ok": False, "reason": "门侧报告可能有车正在进入，暂缓该项作业",
                    "checks": [{"need": "clear_entry", "ok": False,
                                "reason": "存在未确认的进入观察", "evidence": [attempt.seq]}],
                    "evidence": [attempt.seq], "review": True}
        checks: list[dict] = []
        evidence: list[int] = []
        review = False
        for need in needs:
            checker = getattr(self, f"_need_{need}", None)
            if checker is None:
                checks.append({"need": need, "ok": False, "reason": "未实现的准入条件"})
                review = True
                continue
            result = checker(bay, unit, at)
            checks.append({"need": need, **result})
            evidence.extend(result.get("evidence", []))
            review = review or bool(result.get("review"))
            if not result["ok"]:
                return {"ok": False, "reason": result.get("reason", "条件不足"),
                        "checks": checks, "evidence": sorted(set(evidence)), "review": review}
        return {"ok": True, "reason": "各项现场条件成立", "checks": checks,
                "evidence": sorted(set(evidence)), "review": False}

    def _need_exact_vehicle(self, bay: str, unit: str | None, at: float) -> dict:
        state = self.view(bay, at)
        seqs = [item["seq"] for item in state.get("evidence", [])]
        if state["status"] == "conflict":
            return {"ok": False, "reason": "该位存在未消解的占用冲突，无法确认车辆",
                    "evidence": seqs, "review": True}
        if state["status"] != "occupied":
            return {"ok": False, "reason": f"该位不是已确认占用（{state['status']}）",
                    "evidence": seqs, "review": state["status"] == "unknown"}
        if not state.get("identified"):
            return {"ok": False, "reason": "证明有位车占用，但无法确认是哪台车",
                    "evidence": seqs, "review": True}
        if unit and state.get("unit") != unit:
            return {"ok": False, "reason": f"该位确认车辆为 {state.get('unit')}，不是申请的 {unit}",
                    "evidence": seqs, "review": False}
        return {"ok": True, "reason": f"该位确认为 {state.get('unit')}", "evidence": seqs}

    def _need_bond(self, bay: str, unit: str | None, at: float) -> dict:
        state = self.view(bay, at)
        target = unit or state.get("unit")
        if not target:
            return {"ok": False, "reason": "尚未确认车辆，无法核对同工位接地夹",
                    "evidence": [item["seq"] for item in state.get("evidence", [])], "review": True}
        bonds = [
            item for item in self.evidence[bay]
            if item.effect == "bond_ok" and item.unit == target and item.at <= at
        ]
        if not bonds:
            return {"ok": False, "reason": f"没有 {target} 与 {bay} 接地夹的确认连接",
                    "evidence": [item["seq"] for item in state.get("evidence", [])], "review": True}
        bond = max(bonds, key=lambda item: (item.at, item.seq))
        failed = [
            item for item in self.evidence[bay]
            if item.effect == "bond_failed" and item.unit == target
            and (item.at, item.seq) > (bond.at, bond.seq)
        ]
        if failed:
            return {"ok": False, "reason": "接地夹已报告未连接", "evidence": [bond.seq],
                    "review": True}
        if self.unit_location(target, at) != bay:
            return {"ok": False, "reason": "车辆已不在该工位", "evidence": [bond.seq],
                    "review": False}
        return {"ok": True, "reason": f"{target} 的接地夹仍连在 {bay}", "evidence": [bond.seq]}

    def _need_empty(self, bay: str, unit: str | None, at: float) -> dict:
        state = self.view(bay, at)
        seqs = [item["seq"] for item in state.get("evidence", [])]
        if state["status"] == "empty":
            return {"ok": True, "reason": "该位有确证离位或独立核定的空位凭据", "evidence": seqs}
        if state["status"] == "conflict":
            return {"ok": False, "reason": state.get("conflict", "空位证据存在冲突"),
                    "evidence": seqs, "review": True}
        if state["status"] == "occupied":
            return {"ok": False, "reason": "该位仍有占用，不能作为已真正空出的区域",
                    "evidence": seqs, "review": False}
        return {"ok": False, "reason": "缺少足够的独立空位证据", "evidence": seqs,
                "review": True}

    def _need_clear_surface(self, bay: str, unit: str | None, at: float) -> dict:
        claim = self._manual_at(bay, at, "clear_surface")
        if claim:
            return {"ok": True, "reason": "限定时段内有台面已扫除的现场凭据",
                    "evidence": [claim.seq]}
        return {"ok": False, "reason": "没有该工位在有效时段内的已扫除凭据",
                "evidence": [], "review": True}

    def _need_clear_entry(self, bay: str, unit: str | None, at: float) -> dict:
        attempt = self._attempt_at(bay, at)
        if attempt:
            return {"ok": False, "reason": "门侧报告可能有车正在进入该位",
                    "evidence": [attempt.seq], "review": True}
        return {"ok": True, "reason": "没有未确认的进入观察", "evidence": []}

    # ------------------------------------------------------------ 输出

    def job_view(self, job: Job) -> dict:
        verdict = self.check(job.bay, job.type, job.unit)
        return {
            "id": job.id,
            "bay": job.bay,
            "unit": job.unit,
            "type": job.type,
            "status": job.status,
            "requested_at": job.requested_at,
            "requirements": verdict["checks"],
            "reason": verdict["reason"],
            "continuation": self._continuation(job, verdict),
            "history": list(job.history),
        }

    def _continuation(self, job: Job, verdict: dict) -> dict:
        if job.status == "active":
            return {"decision": "continue", "allowed": ["continue_work", "report_evidence",
                                                        "finish", "stop"],
                    "reason": "现场证据仍支持已开工的作业"}
        if job.status == "pending_review":
            return {"decision": "pause", "allowed": ["stop", "await_lead_review"],
                    "reason": "需要负责人按位置、车辆和时间范围复核后才能恢复"}
        if job.status == "suspended":
            return {"decision": "stop", "allowed": ["stop", "await_lead_review"],
                    "reason": "原有现场条件已不成立，不能继续"}
        if job.status == "held":
            return {"decision": "wait", "allowed": ["await_conditions", "cancel"],
                    "reason": verdict["reason"]}
        return {"decision": "closed", "allowed": [], "reason": "作业已结束"}

    def snapshot(self) -> dict:
        self.refresh()
        return {
            "at": self.now,
            "bays": {bay: self.view(bay) for bay in self.bays},
            "jobs": {job_id: self.job_view(job) for job_id, job in self.jobs.items()},
            "notices": list(self.notices),
            "rejected": list(self.rejected),
        }

    def explain_bay(self, bay: str) -> dict:
        state = self.view(bay)
        return {
            **state,
            "timeline": [item.ref() for item in self.evidence[bay]],
            "degraded_sources": state.get("degraded", {}),
            "reader_reports": [
                report.ref() for report in self.radio if bay in report.rssi or bay in report.strong
            ],
        }


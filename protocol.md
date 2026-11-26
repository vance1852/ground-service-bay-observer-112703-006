# 设备资料

`devices.json` 记录端点和设备能力。`policy.json` 记录已约定的业务配置。`observations.ndjson` 是按回放顺序逐行保存的原始记录。每行有 `seq`、`device`、`kind`。从 1 开始的 `seq` 只标识回放顺序，独立于设备时间。`refs` 是原始记录间已知的引用，用来定位同批材料。

回放台只在回环地址侦听。`GET /devices` 读取设备表。`GET /events?after=0&limit=16` 取 `seq` 大于游标的接续记录，返回事件和新游标。`POST /commands` 接收含设备、动作与命令标识的 JSON 对象，字段为 `device`、`action`、`id`，其他字段随动作约定。它返回的是回放通道的收件凭据，设备物理动作由回放中的设备观测给出。接收任意命令不会改变记录。

场站时间以秒计，传感器会话有 source_epoch。压力值为占用接触，门侧 move 在观测到的两个工位之间报告方向及证据范围。reader 回报多个候选位与接收强度，强度没有唯一归属含义。fault 指明某一源族什么时候失去证明力。bond 给出工位及夹具实际连到的设备，site_check 的 from、until 和 claims 是现场负责人具体核定的范围与结果，离场后提交的复核凭据只能覆盖其中载明的对象。事件上的 arrived 和 at 分别是到达与物理时刻。

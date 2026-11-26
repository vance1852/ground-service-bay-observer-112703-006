# 相邻检修位的占用归属与工作准入

回放目录使用本地合成设备和事件。约定见 [设备资料](protocol.md)，其中说明实体、事件以及如何解释相应的现场观测。

需要 Python 3.11 标准库。运行 `python3 bench.py` 查看设备与观测数量，运行 `python3 -m unittest discover -s tests -v` 核验资料关联和本地 HTTP 往返。用 `python3 bench.py --listen 8765` 启动回放台，终止进程即可关停。可用 `--scene` 指定同格式的另一份回放。

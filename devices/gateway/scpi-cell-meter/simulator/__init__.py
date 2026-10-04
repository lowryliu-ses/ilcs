"""假的 SCPI 电芯检测仪表（Keithley 2450 / 2400、Hioki BT3562 系列），走真实的文本命令协议，驱动宿主用
`line_command` 插件连它们（ILCS 经 SiLA 2 接驱动宿主）。仪表模型在 `keithley.py`、`hioki.py`，公共的命令解析在 `scpi.py`，TCP 口与统一控制口在 `server.py`。"""

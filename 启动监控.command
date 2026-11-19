#!/bin/zsh
cd "$(dirname "$0")"
# caffeinate -i 让 Mac 在监控期间不进入睡眠，关闭终端窗口即停止监控
exec /usr/bin/caffeinate -i /usr/bin/env python3 monitor.py

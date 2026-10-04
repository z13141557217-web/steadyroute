#!/bin/bash
# 稳航 SteadyRoute 安装 / 升级 / 迁移（macOS）。在 Finder 里双击，或在终端运行 ./install.command
cd "$(dirname "$0")" || exit 1
pause() {
  if [ -t 0 ]; then
    echo
    read -r -n 1 -p "按任意键关闭窗口"
    echo
  fi
}
if ! xcode-select -p >/dev/null 2>&1; then
  echo "这台 Mac 还没有 Apple 命令行工具（稳航需要其中的 python3）。"
  echo "即将弹出安装窗口：点“安装”，完成后再运行一次 install.command。"
  xcode-select --install >/dev/null 2>&1
  pause
  exit 1
fi
/usr/bin/python3 scripts/installer.py install "$@"
code=$?
pause
exit $code

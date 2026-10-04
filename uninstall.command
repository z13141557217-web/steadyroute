#!/bin/bash
# 稳航 SteadyRoute 卸载（macOS）。稳航写进 Clash Verge 的专线和规则会一并去掉，恢复原样。
cd "$(dirname "$0")" || exit 1
if [ -t 0 ]; then
  read -r -p "确定卸载稳航？(y/N) " answer
  case "$answer" in
    y|Y) ;;
    *) echo "已取消。"; exit 0 ;;
  esac
fi
/usr/bin/python3 scripts/installer.py uninstall "$@"
code=$?
if [ -t 0 ]; then
  echo
  read -r -n 1 -p "按任意键关闭窗口"
  echo
fi
exit $code

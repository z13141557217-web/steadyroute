#!/bin/bash
# 稳航 SteadyRoute：回到升级前的版本（macOS）。先撤销稳航写入 Clash 的专线和规则，再启动旧版本。
cd "$(dirname "$0")" || exit 1
if [ -t 0 ]; then
  read -r -p "确定回到升级前的版本？(y/N) " answer
  case "$answer" in
    y|Y) ;;
    *) echo "已取消。"; exit 0 ;;
  esac
fi
/usr/bin/python3 scripts/installer.py rollback "$@"
code=$?
if [ -t 0 ]; then
  echo
  read -r -n 1 -p "按任意键关闭窗口"
  echo
fi
exit $code

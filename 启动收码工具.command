#!/bin/zsh
cd -- "$(dirname -- "$0")" || exit 1
python3 mailtool.py --open
if [ "$?" -ne 0 ]; then
  printf '\n启动失败，按回车关闭窗口。'
  read -r
fi

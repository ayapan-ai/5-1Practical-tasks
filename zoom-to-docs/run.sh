#!/bin/bash
# launchd / cron から呼び出す実行ラッパー。
# venv のPythonを使い、実行ログを run.log に残す。

set -euo pipefail
cd "$(dirname "$0")"

if [ -d "venv" ]; then
  source venv/bin/activate
fi

python3 main.py >> run.log 2>&1

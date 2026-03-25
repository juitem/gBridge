#!/bin/bash
cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
    echo "가상환경 생성 중..."
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
else
    source venv/bin/activate
fi

echo "gbridge 시작: http://0.0.0.0:8765"
python3 -m backend.main

#!/bin/bash

uv --version
if [ $? -ne 0 ]
then
   echo "- missing uv."
   exit 0
fi

cd "$(dirname "$0")"

PYTHON="$(uv python find)"
if [ $? -ne 0 ]
then
   uv python install python3.13
fi
PYTHON="$(uv python find)"
echo "using: ${PYTHON}"

uv venv venv
source venv/bin/activate
uv pip install -r requirements.txt

uvicorn main:app --host 0.0.0.0 --port 8000
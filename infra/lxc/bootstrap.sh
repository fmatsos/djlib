#!/usr/bin/env bash
set -Eeuo pipefail
apt-get update
apt-get install -y python3 python3-venv python3-pip exiftool ffmpeg libchromaprint-tools
python3 -m venv /opt/djlib-venv
/opt/djlib-venv/bin/python -m pip install --upgrade pip

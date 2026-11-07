#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Existing task checkouts are isolated: do not create a Git worktree.
export GOPATH="${GOPATH:-/workspace/.cache/go}"
export GOCACHE="${GOCACHE:-/workspace/.cache/go-build}"
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-/workspace/.cache/pip}"
tools_dir="${GUITAR_RADAR_TOOLS:-/workspace/.tools}"
go_bin="$tools_dir/go/bin/go"
if [[ ! -x "$go_bin" ]] || [[ "$("$go_bin" version)" != *'go1.25.5 '* ]]; then
  if [[ "$(uname -s)/$(uname -m)" != Linux/x86_64 ]]; then
    echo 'Automatic Go installation supports Linux/x86_64. Install Go 1.25.5 and set GUITAR_RADAR_TOOLS.' >&2
    exit 1
  fi
  archive="$(mktemp /tmp/guitar-radar-go.XXXXXX)"
  trap 'rm -f "$archive"' EXIT
  curl -fsSL --retry 2 --max-time 180 https://go.dev/dl/go1.25.5.linux-amd64.tar.gz -o "$archive"
  # SHA-256 from official https://go.dev/dl/?mode=json&include=all
  echo "9e9b755d63b36acf30c12a9a3fc379243714c1c6d3dd72861da637f336ebb35b  $archive" | sha256sum -c -
  mkdir -p "$tools_dir"
  tar -xzf "$archive" -C "$tools_dir"
fi
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
if [[ ! -x .venv/bin/python ]]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install -r collectors/fbgql/requirements.txt
mkdir -p .bin
(cd collectors/facebook-go && "$go_bin" mod download && "$go_bin" build -mod=readonly -o ../../.bin/facebook-go .)
echo 'Dependencies installed and Go collector built. Run make test and make demo.'

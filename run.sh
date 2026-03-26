#!/bin/bash
# Quick launcher for BLASTGRID
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/.venv/bin/activate"
blastgrid "$@"

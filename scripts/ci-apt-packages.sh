#!/usr/bin/env bash
# Native libs required to build/install pycairo (xhtml2pdf → PDF drafts).
set -euo pipefail
sudo apt-get update
sudo apt-get install -y libcairo2-dev pkg-config

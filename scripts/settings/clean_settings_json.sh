#!/usr/bin/env bash

set -euo pipefail

jq 'del(.model)'

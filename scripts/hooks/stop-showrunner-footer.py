#!/usr/bin/env python3
"""Retired Stop hook: it checks nothing and exits 0.

It once blocked a showrunner reply with an unreviewed question, and before that it checked the
reply footer. The user removed it (2026-10-19). The file stays only while settings.json still
names it, so no session gets a missing-file error; it goes when that entry is dropped.
"""

import sys

if __name__ == "__main__":
    sys.exit(0)

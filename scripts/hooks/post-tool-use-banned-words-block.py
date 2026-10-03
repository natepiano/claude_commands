#!/usr/bin/env python3
"""Retired: merged into post-tool-use-banned-words.py, which now blocks too.

Kept as a no-op that exits 0. A session started before the merge still runs
this path after every tool call from its hooks snapshot, and a missing script
makes python exit 2, which would block every one of those calls. Delete it once
every session has restarted.
"""

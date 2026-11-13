"""Print the AWS spend per day with its analysis from the local cost ledger, without an AWS call."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import sys

from scripts.buildlog import index, report
from scripts.lib.user_zone import user_zone


def main(argv: list[str]) -> int:
    if argv:
        print('usage: aws_spend.py', file=sys.stderr)
        return 2
    with closing(index.read_only()) as connection:
        print('\n'.join(report.aws_section(connection, datetime.now(user_zone()).date().isoformat())))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))

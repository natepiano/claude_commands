"""Format the local Spot snapshot for a terminal."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import shutil

from scripts.aws_target import ledger
from scripts.aws_target.aws_target import largest_gate_vcpus, read_snapshot
from scripts.lib import conf


def render(directory: Path, now: int, *, width: int | None = None, settings_path: Path | None = None) -> str:
    """Show regional minimum prices, read ages, quotas, and box counts."""
    values = conf.read_or_repository('cloud_gate')
    configuration = conf.read(settings_path) if settings_path else conf.read_or_repository('aws_target')
    regions = tuple(dict.fromkeys(values.words('spot_candidate_regions')))
    standard = values.words('instance_types')
    gpu = values.words('gpu_instance_types')
    sizes = tuple(dict.fromkeys((*standard, *gpu)))
    document = read_snapshot(directory)
    readings = {ledger.text(row, 'region'): row for row in ledger.rows(document, 'regions')} if 'regions' in document else {}
    minimum: dict[tuple[str, str], Decimal] = {}
    for row in ledger.rows(document, 'prices'):
        key = ledger.text(row, 'region'), ledger.text(row, 'instance_type')
        price = conf.decimal(ledger.text(row, 'usd_per_hour'))
        minimum[key] = min(minimum.get(key, price), price)
    label_width = max(len('type'), *(len(size) for size in sizes))
    column_width = max(8, *(len(region) for region in regions))
    terminal_width = width if width is not None else shutil.get_terminal_size().columns
    count = max(1, (terminal_width - label_width) // (column_width + 2))
    lines = ['Spot prices (USD/h; lowest zone)']
    for start in range(0, len(regions), count):
        columns = regions[start:start + count]
        lines.append('type'.ljust(label_width) + ''.join('  ' + region.rjust(column_width) for region in columns))
        for size in sizes:
            cells: list[str] = []
            for region in columns:
                price = minimum.get((region, size))
                cells.append('-' if price is None else f'{price:.4f}')
            lines.append(size.ljust(label_width) + ''.join('  ' + cell.rjust(column_width) for cell in cells))
        lines.append('')
    for region in regions:
        reading = readings.get(region)
        if reading is None:
            lines.extend((f'{region}: snapshot age unknown', '  Standard quota: - vCPUs', '  G and VT quota: - vCPUs'))
        else:
            age = now - ledger.instant(reading, 'read_at')
            stale = ' (stale)' if age < 0 or age > configuration.whole('price_max_age_seconds') else ''
            lines.append(f'{region}: snapshot age {age}s{stale}')
            for name, field in (('Standard', 'standard_vcpus'), ('G and VT', 'gpu_vcpus')):
                quota = conf.decimal(ledger.text(reading, field))
                lines.append(f'  {name} quota: {quota.normalize():f} vCPUs')
        for size in sizes:
            vcpus = largest_gate_vcpus((size,))
            field = 'gpu_vcpus' if size in gpu else 'standard_vcpus'
            fit = str(int(conf.decimal(ledger.text(reading, field)) // vcpus)) if reading and vcpus else '-'
            lines.append(f'  {size} ({vcpus or "?"} vCPUs): {fit} boxes')
    return '\n'.join(lines)

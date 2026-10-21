#!/usr/bin/env python3
from __future__ import annotations
import argparse
import csv
import gzip
import json
from pathlib import Path

COLS = [
    'source', 'source_record_id',
    'passenger_name', 'date_of_birth', 'passport', 'loyalty_ids',
    'flight_number', 'departure_date', 'departure_time',
    'arrival_date', 'arrival_time',
    'origin_airport', 'destination_airport',
    'origin_city', 'destination_city',
    'origin_country', 'destination_country',
]


def open_csv(path, mode):
    if str(path).endswith('.gz'):
        return gzip.open(path, mode+'t', encoding='utf-8-sig', newline='')
    return open(path, mode, encoding='utf-8-sig', newline='')




def csv_delimiter(handle):
    at = handle.tell()
    header = handle.readline()
    handle.seek(at)
    return ';' if header.count(';') > header.count(',') else ','

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source_dir', type=Path, help='Folder created by step 1')
    parser.add_argument('--out', type=Path, default=Path('result/02_merged.csv'))
    args = parser.parse_args()

    if not args.source_dir.is_dir():
        parser.error('Source directory not found')
    sources = sorted(args.source_dir.rglob('*.csv'))
    if not sources:
        parser.error('There are no source CSV files. Run step 1 first.')
    args.out.parent.mkdir(parents=True, exist_ok=True)
    out_resolved = args.out.resolve()
    sources = [p for p in sources if p.resolve() != out_resolved]
    counts = {}
    with open_csv(args.out, 'w') as dst:
        writer = csv.DictWriter(dst, fieldnames=COLS, delimiter=';')
        writer.writeheader()
        for file_path in sources:
            with open(file_path, 'r', encoding='utf-8-sig', newline='') as inp:
                reader = csv.DictReader(inp, delimiter=csv_delimiter(inp))
                if reader.fieldnames is None or set(reader.fieldnames) != set(COLS):
                    raise ValueError(f'Incompatible columns in {file_path}: {reader.fieldnames}')
                n = 0
                for row in reader:
                    writer.writerow({k: row[k] for k in COLS})
                    n += 1
            counts[str(file_path.relative_to(args.source_dir))] = n
    report = {'input_files':len(counts),'total_rows':sum(counts.values()),'source_counts':counts}
    report_path = args.out.parent / '02_merge_report.json'
    report_path.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print(f"Merged {len(counts)} files and {report['total_rows']:,} rows -> {args.out}")


if __name__=='__main__':
    main()

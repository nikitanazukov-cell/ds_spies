from __future__ import annotations
import argparse
from collections import Counter
import csv
from datetime import date
import gzip
import hashlib
import json
from pathlib import Path
import re
import sqlite3

COLS = [
    'source', 'source_record_id', 'passenger_name', 'date_of_birth',
    'passport', 'loyalty_ids', 'flight_number', 'departure_date',
    'departure_time', 'arrival_date', 'arrival_time',
    'origin_airport', 'destination_airport', 'origin_city',
    'destination_city', 'origin_country', 'destination_country',
]
CLEAN_COLS = ['person_id'] + COLS + ['observation_count']
COMPARE_FIELDS = ('date_of_birth', 'departure_time', 'arrival_date', 'arrival_time',
                  'origin_airport', 'destination_airport')
FILL_FIELDS = [x for x in COLS if x not in ('source', 'source_record_id')]


def open_csv(path, mode):
    if str(path).endswith('.gz'):
        return gzip.open(path, mode+'t', encoding='utf-8-sig', newline='')
    return open(path, mode, encoding='utf-8-sig', newline='')




def csv_delimiter(handle):
    at = handle.tell()
    header = handle.readline()
    handle.seek(at)
    return ';' if header.count(';') > header.count(',') else ','

def pieces(value):
    return sorted({x.strip().upper() for x in (value or '').split('|') if x.strip()})


def is_valid_date(value):
    try:
        date.fromisoformat(value)
        return bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}',value))
    except (ValueError,TypeError):
        return False


def norm_time(value):
    value = (value or '').strip()
    if not value: return ''
    m = re.fullmatch(r'(\d{1,2}):(\d{2})(?::(\d{2}))?', value)
    if m and 0 <= int(m[1]) <= 23 and 0 <= int(m[2]) <= 59 and (m[3] is None or int(m[3])<=59):
        return f'{int(m[1]):02d}:{int(m[2]):02d}' + (f':{m[3]}' if m[3] is not None else '')
    return ''


def make_person_id(row, loyalty_to_passport, ambiguous_loyalty, named_cards, identity_mode):
    docs = pieces(row['passport'])
    cards = pieces(row['loyalty_ids'])
    if len(docs) > 1:
        return None,'multiple_passport_numbers'
    if docs:
        return 'P:'+docs[0],''
    if not cards:
        return None,'no_reliable_identity'
    if identity_mode == 'named-linked' and not row['passenger_name'] and not any(
            card in named_cards for card in cards):
        return None, 'anonymous_loyalty_without_named_profile'
    if identity_mode == 'passport-backed' and not row['passenger_name']:
        linked_docs={loyalty_to_passport[c] for c in cards if c in loyalty_to_passport}
        if len(linked_docs)!=1:
            return None, 'anonymous_loyalty_without_unique_passport_link'
    if any(card in ambiguous_loyalty for card in cards):
        return None,'ambiguous_loyalty_id'
    links = {loyalty_to_passport[x] for x in cards if x in loyalty_to_passport}
    if len(links)>1:
        return None,'conflicting_loyalty_passport_links'
    if len(links)==1:
        return 'P:'+next(iter(links)),''
    if len(cards)==1:
        return 'L:'+cards[0],''
    digest = hashlib.sha256('|'.join(cards).encode()).hexdigest()[:20]
    return 'LSET:'+digest,''


def compatible(old, new):
    for field in COMPARE_FIELDS:
        if old[field] and new[field] and old[field] != new[field]:
            return False
    for airport,city,country in (
        ('origin_airport','origin_city','origin_country'),
        ('destination_airport','destination_city','destination_country')):
        if not old[airport] and not new[airport]:
            if old[city] and new[city] and old[city].casefold()!=new[city].casefold():
                return False
            if old[country] and new[country] and old[country].casefold()!=new[country].casefold():
                return False
    return True


def merge_rows(old,new):
    merged=dict(old)
    for f in FILL_FIELDS:
        if not merged[f] and new[f]:
            merged[f]=new[f]
    if len(new['passenger_name']) > len(old['passenger_name']):
        merged['passenger_name'] = new['passenger_name']
    merged['source'] = '|'.join(sorted(set(old['source'].split('|')) | set(new['source'].split('|'))))
    merged['loyalty_ids'] = '|'.join(sorted(set(pieces(old['loyalty_ids']))|set(pieces(new['loyalty_ids']))))
    old_refs = old['source_record_id'].split('|') if old['source_record_id'] else []
    ref = new['source']+':'+new['source_record_id']
    if ref not in old_refs and len(old_refs)<5:
        old_refs.append(ref)
    merged['source_record_id']='|'.join(old_refs)
    merged['observation_count']=int(old['observation_count'])+1
    return merged


def build_loyalty_links(input_csv, connection):
    connection.execute('''CREATE TABLE loyalty_link(
        loyalty TEXT NOT NULL, passport TEXT NOT NULL,
        PRIMARY KEY(loyalty,passport)) WITHOUT ROWID''')
    buffer=[]
    named_cards=set()
    with open_csv(input_csv,'r') as f:
        reader=csv.DictReader(f, delimiter=csv_delimiter(f))
        for row in reader:
            docs=pieces(row.get('passport',''))
            cards=pieces(row.get('loyalty_ids',''))
            if row.get('passenger_name','').strip() and cards:
                named_cards.update(cards)
            if len(docs)==1 and cards:
                buffer.extend((card,docs[0]) for card in cards)
            if len(buffer)>=25000:
                connection.executemany('INSERT OR IGNORE INTO loyalty_link VALUES (?,?)',buffer)
                buffer.clear()
    if buffer:
        connection.executemany('INSERT OR IGNORE INTO loyalty_link VALUES (?,?)',buffer)
    connection.commit()
    groups=connection.execute('''SELECT loyalty, MIN(passport) AS passport,
                    COUNT(*) AS n FROM loyalty_link GROUP BY loyalty''')
    unique={};ambiguous=[]
    for card,psp,n in groups:
        if n==1: unique[card]=psp
        else: ambiguous.append((card,n))
    return unique,ambiguous,named_cards


def process(args):
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.rejected.parent.mkdir(parents=True,exist_ok=True)
    args.incomplete.parent.mkdir(parents=True,exist_ok=True)
    args.report.parent.mkdir(parents=True,exist_ok=True)
    db_path=args.out.parent/'03_clean_work.sqlite'
    if db_path.exists(): db_path.unlink()
    con=sqlite3.connect(str(db_path))
    con.execute('PRAGMA journal_mode=OFF')
    con.execute('PRAGMA synchronous=OFF')
    con.execute('PRAGMA temp_store=FILE')
    con.execute('PRAGMA cache_size=-32000')
    stats=Counter();reason_counts=Counter()
    try:
        print('Step 3: resolving unique loyalty -> passport links...',flush=True)
        links, ambiguous, named_cards=build_loyalty_links(args.input,con)
        stats['unique_loyalty_passport_links']=len(links)
        stats['ambiguous_loyalty_cards']=len(ambiguous)
        stats['cards_associated_with_a_name']=len(named_cards)
        ambiguous_cards={x[0] for x in ambiguous}
        con.execute('''CREATE TABLE unique_flights(
            pk INTEGER PRIMARY KEY,
            candidate_key TEXT NOT NULL,
            person_id TEXT NOT NULL,
            source TEXT, source_record_id TEXT, passenger_name TEXT,
            date_of_birth TEXT, passport TEXT, loyalty_ids TEXT,
            flight_number TEXT, departure_date TEXT, departure_time TEXT,
            arrival_date TEXT, arrival_time TEXT,
            origin_airport TEXT, destination_airport TEXT,
            origin_city TEXT, destination_city TEXT,
            origin_country TEXT, destination_country TEXT,
            observation_count INTEGER)''')
        con.execute('CREATE INDEX idx_flight_candidate ON unique_flights(candidate_key)')
        columns=['person_id']+COLS+['observation_count']
        sql_insert = ('INSERT INTO unique_flights(candidate_key,'+','.join(columns)+')'
                      +' VALUES ('+','.join('?' for _ in range(len(columns)+1))+')')
        sql_update=('UPDATE unique_flights SET '+','.join(k+'=?' for k in COLS)
                    +',observation_count=? WHERE pk=?')
        table_columns=[r[1] for r in con.execute('PRAGMA table_info(unique_flights)')]
        print('Step 3: validating and deduplicating source observations...',flush=True)
        with open_csv(args.input,'r') as inp,open_csv(args.rejected,'w') as rej:
            reader=csv.DictReader(inp, delimiter=csv_delimiter(inp))
            if set(reader.fieldnames or [])!=set(COLS):
                raise ValueError('Invalid merged CSV headers; run scripts 01 and 02')
            rejected_writer=csv.DictWriter(rej,fieldnames=COLS+['rejection_reason'], delimiter=';')
            rejected_writer.writeheader()
            for raw in reader:
                stats['input_rows']+=1
                row={k: str(raw.get(k) or '').strip() for k in COLS}
                row['passport']='|'.join(pieces(row['passport']))
                row['loyalty_ids']='|'.join(pieces(row['loyalty_ids']))
                row['flight_number']=''.join(row['flight_number'].upper().split())
                for f in ('origin_airport','destination_airport'):
                    row[f]=row[f].strip().upper()
                row['passenger_name']=' '.join(row['passenger_name'].split())
                for time_col in ('departure_time','arrival_time'):
                    raw_time=row[time_col]
                    row[time_col]=norm_time(raw_time)
                    if raw_time and not row[time_col]:
                        stats['invalid_'+time_col+'_values_removed']+=1

                pid,reason=make_person_id(row,links,ambiguous_cards, named_cards, args.identity_mode)
                if not reason and args.year and not row['departure_date'].startswith(str(args.year)+'-'):
                    reason='outside_requested_year'
                if not reason and not row['flight_number']:
                    reason='missing_flight_number'
                if not reason and not is_valid_date(row['departure_date']):
                    reason='missing_or_invalid_departure_date'
                if not reason and not any(row[x] for x in ('origin_airport', 'destination_airport',
                    'origin_city','destination_city','origin_country','destination_country')):
                    reason='no_route_information'
                if reason:
                    reason_counts[reason]+=1
                    rejected_writer.writerow({**row,'rejection_reason':reason})
                    continue
                candidate_key='|'.join((pid,row['departure_date'],row['flight_number']))
                possible=con.execute('SELECT * FROM unique_flights WHERE candidate_key=?',(candidate_key,)).fetchall()
                match=None
                if possible:
                    for existing in possible:
                        old=dict(zip(table_columns,existing))
                        if compatible(old,row):
                            match=old
                            break
                if match:
                    merged=merge_rows(match,row)
                    con.execute(sql_update, [merged[k] for k in COLS]+[
                        merged['observation_count'],match['pk']])
                    stats['duplicate_observations_merged']+=1
                else:
                    if possible:
                        stats['conflicting_same_flight_records_preserved']+=1
                    row['source_record_id']=row['source']+':'+row['source_record_id']
                    con.execute(sql_insert,[candidate_key,pid]+[row[k] for k in COLS]+[1])
                    stats['unique_records_inserted']+=1
                if stats['input_rows']%25000==0:
                    con.commit()
                if stats['input_rows']%500000==0:
                    print(f"  examined {stats['input_rows']:,} observations",flush=True)
        con.commit()
        print('Step 3: writing cleaned, person-sorted CSV...',flush=True)
        with open_csv(args.out,'w') as dst, open_csv(args.incomplete,'w') as inc:
            writer=csv.DictWriter(dst,fieldnames=CLEAN_COLS, delimiter=';')
            writer.writeheader()
            inc_writer=csv.DictWriter(inc,fieldnames=CLEAN_COLS+['rejection_reason'],delimiter=';')
            inc_writer.writeheader()
            for row in con.execute('''SELECT person_id,'''+','.join(COLS)+''',observation_count
                    FROM unique_flights ORDER BY person_id,departure_date,departure_time,flight_number'''):
                record=dict(zip(CLEAN_COLS,row))
                has_from = any(record[x] for x in ('origin_airport','origin_city','origin_country'))
                has_to = any(record[x] for x in ('destination_airport','destination_city','destination_country'))
                if args.route_policy=='both' and not (has_from and has_to):
                    inc_writer.writerow({**record,'rejection_reason':'incomplete_route_after_merge'})
                    stats['incomplete_routes_quarantined']+=1
                    continue
                writer.writerow(record)
                stats['cleaned_rows']+=1
        stats['rejected_rows']=sum(reason_counts.values())+stats['incomplete_routes_quarantined']
        if stats['incomplete_routes_quarantined']:
            reason_counts['incomplete_route_after_merge']=stats['incomplete_routes_quarantined']
        report={'counts':dict(stats),'rejection_reasons':dict(reason_counts),
                'identity_rule':'passport, a unique link from loyalty to passport, or named loyalty-linked profile (anonymous card policy is configurable)',
                'dedup_rule':'same person/date/flight AND non-conflicting details; conflicting records are preserved',
                'identity_mode':args.identity_mode, 'route_policy':args.route_policy,
                'caveat':'These are source claims, not confirmed boarding records. Route-incomplete records are in a separate quarantine file.'}
        args.report.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
        print(json.dumps(report,indent=2,ensure_ascii=False))
    finally:
        con.close()
        if not args.keep_db:
            db_path.unlink(missing_ok=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('input',type=Path,help='Merged CSV from step 2')
    p.add_argument('--out',type=Path,default=Path('result/03_cleaned.csv'))
    p.add_argument('--rejected',type=Path,default=Path('result/03_rejected.csv.gz'))
    p.add_argument('--report',type=Path,default=Path('result/03_clean_report.json'))
    p.add_argument('--keep-db',action='store_true',help='Keep temporary SQLite database')
    p.add_argument('--incomplete',type=Path,default=Path('result/03_incomplete_routes.csv.gz'),
                   help='Records that still lack either end of the route after deduplication')
    p.add_argument('--identity-mode',choices=['passport-backed','named-linked','card-only'],default='passport-backed',
                   help='passport-backed (default): anonymous cards require unique passport link; named-linked: allow a named linked profile; card-only: legacy')
    p.add_argument('--route-policy',choices=['both','partial'],default='both',
                   help='both requires both ends of an itinerary after deduplication')
    p.add_argument('--year',type=int,default=0,help='Optional departure year filter; zero means no filter')
    args=p.parse_args()
    process(args)

if __name__=='__main__':
    main()

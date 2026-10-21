#!/usr/bin/env python3
from __future__ import annotations
import argparse
import csv
from collections import Counter
from datetime import datetime
import gzip
import io
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
import zipfile
import xml.etree.ElementTree as ET

COLS = [
    'source', 'source_record_id',
    'passenger_name', 'date_of_birth', 'passport', 'loyalty_ids',
    'flight_number', 'departure_date', 'departure_time',
    'arrival_date', 'arrival_time',
    'origin_airport', 'destination_airport',
    'origin_city', 'destination_city',
    'origin_country', 'destination_country',
]

MISSING = {'', 'none', 'n/a', 'not presented', 'null', 'nan', 'unknown'}
NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'


def clean(v):
    if v is None: return ''
    v = str(v).strip()
    return '' if v.lower() in MISSING else v


def upper(v): return clean(v).upper()


def code(v): return re.sub(r'\s+', '', upper(v))


def passport(v): return re.sub(r'\s+', '', upper(v))


def loyalty(v, program=''):
    v = upper(v)
    if not v: return ''
    prog = upper(program)
    if re.fullmatch(r'\d+',v) and prog: return prog+' '+v
    if re.fullmatch(r'[A-Z]{2}\s*\d+',v):
        m=re.fullmatch(r'([A-Z]{2})\s*(\d+)',v)
        return m.group(1)+' '+m.group(2)
    return v


def iso_date(v, fmt=None):
    v=clean(v)
    if not v: return ''
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}',v): return v
    if fmt:
        try: return datetime.strptime(v,fmt).strftime('%Y-%m-%d')
        except ValueError: return v
    return v


class Sink:
    def __init__(self, file_path):
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.file_path.open('w', encoding='utf-8-sig', newline='')
        self.writer = csv.DictWriter(self.file, fieldnames=COLS, delimiter=';')
        self.writer.writeheader()
        self.counts = Counter()

    def emit(self, record):
        row = {key: clean(record.get(key, '')) for key in COLS}
        row['passenger_name'] = ' '.join(row['passenger_name'].split())
        for key in ('flight_number', 'origin_airport', 'destination_airport'):
            row[key] = code(row[key])
        row['passport'] = passport(row['passport'])
        if row['loyalty_ids']:
            tokens = [loyalty(x) for x in row['loyalty_ids'].split('|')]
            row['loyalty_ids'] = '|'.join(dict.fromkeys(x for x in tokens if x))
        for key in ('departure_date', 'arrival_date', 'date_of_birth'):
            row[key] = iso_date(row[key])
        self.writer.writerow(row)
        self.counts[row['source']] += 1

    def close(self):
        self.file.close()

def csv_boarding(path, sink, limit):
    src='boarding_data'
    with path.open('r',encoding='utf-8-sig',newline='') as f:
        for i,row in enumerate(csv.DictReader(f,delimiter=';'),start=2):
            if not any(row.values()):continue
            sink.emit(dict(
                source=src,source_record_id=str(i),
                passenger_name=' '.join(filter(None,(
                    clean(row.get('PassengerFirstName')),
                    clean(row.get('PassengerSecondName')),
                    clean(row.get('PassengerLastName'))))),
                date_of_birth=iso_date(row.get('PassengerBirthDate'),'%m/%d/%Y'),
                passport=row.get('PassengerDocument'),
                flight_number=row.get('FlightNumber'),
                departure_date=row.get('FlightDate'),
                departure_time=row.get('FlightTime'),
                destination_city=row.get('Destination')))
            if limit and sink.counts[src]>=limit:break


SIRENA_COLUMNS = [
    ('PaxName',0),('PaxBirthDate',60),('DepartDate',72),('DepartTime',84),
    ('ArrivalDate',96),('ArrivalTime',108),('Flight',120),('CodeSh',126),
    ('From',132),('Dest',138),('Code',144),('e-Ticket',150),('TravelDoc',168),
    ('Seat',180),('Meal',186),('TrvCls',192),('Fare',198),('Baggage',204),
    ('PaxAdditionalInfo',216),('AgentInfo',276)
]


def read_sirena_line(line):
    return {name: clean(line[start: SIRENA_COLUMNS[i+1][1] if i+1<len(SIRENA_COLUMNS) else None])
            for i,(name,start) in enumerate(SIRENA_COLUMNS)}


def sirena(path,sink,limit):
    src='reservation'
    with path.open('r',encoding='utf-8') as f:
        header=f.readline()
        if not header.startswith('PaxName') or 'PaxBirthDate' not in header:
            raise ValueError('Unknown Sirena layout; fixed columns cannot safely be inferred')
        for line_no,line in enumerate(f,start=2):
            if not line.strip():continue
            r=read_sirena_line(line)
            ff=re.search(r'FF#\s*([A-Z]{2})\s*(\d+)',r['PaxAdditionalInfo'],flags=re.I)
            sink.emit(dict(
                source=src,source_record_id=str(line_no),
                passenger_name=r['PaxName'],date_of_birth=r['PaxBirthDate'],
                passport=r['TravelDoc'],
                loyalty_ids=(loyalty(ff.group(2),ff.group(1)) if ff else ''),
                flight_number=r['Flight'],
                departure_date=r['DepartDate'],departure_time=r['DepartTime'],
                arrival_date=r['ArrivalDate'],arrival_time=r['ArrivalTime'],
                origin_airport=r['From'],destination_airport=r['Dest']))
            if limit and sink.counts[src]>=limit:break


def xml_pointz(path,sink,limit):
    src='loyalty_activity'
    for _,user in ET.iterparse(str(path),events=('end',)):
        if user.tag!='user':continue
        uid=clean(user.get('uid'));name=user.find('name')
        first=clean(name.get('first')) if name is not None else ''
        last=clean(name.get('last')) if name is not None else ''
        for card in user.findall('./cards/card'):
            ff=loyalty(card.get('number'))
            for j,a in enumerate(card.findall('./activities/activity'),start=1):
                if a.get('type')!='Flight':continue
                sink.emit(dict(
                    source=src,source_record_id=f'{uid}:{ff}:{j}',
                    passenger_name=' '.join(x for x in (first,last) if x),
                    loyalty_ids=ff,flight_number=a.findtext('Code'),
                    departure_date=a.findtext('Date'),
                    origin_airport=a.findtext('Departure'),
                    destination_airport=a.findtext('Arrival')))
                if limit and sink.counts[src]>=limit:return
        user.clear()


def yamlexchange(path,sink,limit):
    src='loyalty_flight_record'
    current_date='';flight='';props={};member=[]
    def flush():
        if flight:
            for seq,ff in enumerate(member,start=1):
                sink.emit(dict(source=src,
                    source_record_id=f'{current_date}:{flight}:{ff}:{seq}',
                    loyalty_ids=ff,flight_number=flight,departure_date=current_date,
                    origin_airport=props.get('FROM'),destination_airport=props.get('TO')))
                if limit and sink.counts[src]>=limit:return True
        return False
    with path.open('r',encoding='utf-8') as file:
        for line_number,line in enumerate(file,start=1):
            line=line.rstrip('\r\n')
            if not line.strip():continue
            if not line.startswith(' '):
                if flush():return
                match=re.fullmatch(r"'?(\d{4}-\d{2}-\d{2})'?:\s*",line)
                if not match:raise ValueError(f'Unexpected YAML date line {line_number}: {line[:90]}')
                current_date=match.group(1);flight='';props={};member=[]
            elif line.startswith('  ') and not line.startswith('   '):
                if flush():return
                flight=clean(line.strip().removesuffix(':'));props={};member=[]
            elif line.startswith('    ') and not line.startswith('     '):
                t=line.strip()
                if t=='FF:':continue
                mt=re.fullmatch(r'(FROM|TO|STATUS):\s*(.*)',t)
                if mt:props[mt.group(1)]=clean(mt.group(2))
                else:raise ValueError(f'Unexpected YAML flight property {line_number}: {t[:80]}')
            elif line.startswith('      ') and not line.startswith('       '):
                t=line.strip()
                m=re.fullmatch(r'([^:]+):\s*\{CLASS:\s*([^,}]*),\s*FARE:\s*([^}]*)\}',t)
                if m:member.append(loyalty(m.group(1)))
                else:raise ValueError(f'Unexpected YAML FF entry {line_number}: {t[:80]}')
            else:raise ValueError(f'Unexpected YAML nesting at line {line_number}')
    flush()


def iter_json_array(path):
    dec=json.JSONDecoder();block=2*1024*1024
    with path.open('r',encoding='utf-8') as f:
        buf=f.read(block)
        m=re.search(r'"Forum Profiles"\s*:\s*\[',buf)
        if not m:raise ValueError('Expected "Forum Profiles" JSON array')
        pos=m.end()
        while True:
            while True:
                while pos<len(buf) and buf[pos] in ' \t\r\n,':pos+=1
                if pos<len(buf):break
                buf=f.read(block);pos=0
                if not buf:raise ValueError('Truncated JSON file')
            if buf[pos]==']':break
            while True:
                try:
                    obj,end=dec.raw_decode(buf,pos)
                    break
                except json.JSONDecodeError:
                    new=f.read(block)
                    if not new:raise
                    buf=buf[pos:]+new;pos=0
            yield obj
            pos=end
            if pos>block:
                buf=buf[pos:];pos=0


def forum(path,sink,limit):
    src='forum_registered_flight'
    for i,profile in enumerate(iter_json_array(path),start=1):
        nm=profile.get('Real Name') or {}
        first=clean(nm.get('First Name'));last=clean(nm.get('Last Name'))
        loyalty_ids=[]
        for program in (profile.get('Loyality Programm') or []):
            if not isinstance(program,dict):continue
            ff=loyalty(program.get('Number'),program.get('programm'))
            if ff:loyalty_ids.append(ff)
        loyalty_ids=list(dict.fromkeys(loyalty_ids))
        passports=[]
        for doc in (profile.get('Travel Documents') or []):
            if not isinstance(doc,dict):continue
            v=doc.get('Passports')
            if isinstance(v,str):passports.append(passport(v))
            elif isinstance(v,list):
                for val in v:
                    if isinstance(val,str):passports.append(passport(val))
                    elif isinstance(val,dict):passports.extend(passport(q) for q in val.values() if isinstance(q,str))
        psp='|'.join(x for x in dict.fromkeys(passports) if x)
        for j,fl in enumerate(profile.get('Registered Flights') or [],start=1):
            dept=fl.get('Departure') or {};arr=fl.get('Arrival') or {}
            sink.emit(dict(source=src,source_record_id=f'{i}:{j}',
                passenger_name=' '.join(x for x in (first,last) if x),
                passport=psp,loyalty_ids='|'.join(loyalty_ids),
                flight_number=fl.get('Flight'),departure_date=fl.get('Date'),
                origin_airport=dept.get('Airport'),destination_airport=arr.get('Airport'),
                origin_city=dept.get('City'),destination_city=arr.get('City'),
                origin_country=dept.get('Country'),destination_country=arr.get('Country')))
            if limit and sink.counts[src]>=limit:return


def xlsx_shared(z):
    xml=z.read('xl/sharedStrings.xml')
    root=ET.fromstring(xml)
    return [''.join(node.itertext()) for node in root.findall(NS+'si')]


def xlsx_sheet_fields(data,sst):
    root=ET.fromstring(data);out={}
    relevant={'A1','B3','F3','A5','D5','H5','D7','H7','A9','C9'}
    for c in root.iter(NS+'c'):
        cell=c.attrib.get('r','')
        if cell not in relevant:continue
        v=c.find(NS+'v')
        if v is not None:
            val=v.text or ''
            if c.attrib.get('t')=='s':val=sst[int(val)]
        else:
            inline=c.find(NS+'is')
            val=''.join(inline.itertext()) if inline is not None else ''
        out[cell]=val
    return out


def split_xlsx_archive(path, out_folder, limit, max_workbooks):
    manifest = []
    with zipfile.ZipFile(path) as archive:
        excel_files = sorted(x for x in archive.namelist() if x.lower().endswith('.xlsx'))
        if max_workbooks:
            excel_files = excel_files[:max_workbooks]
        for i, member in enumerate(excel_files, start=1):
            filename = Path(member).stem + '.csv'
            target = out_folder / 'boarding_passes' / filename
            sink = Sink(target)
            try:
                with zipfile.ZipFile(io.BytesIO(archive.read(member))) as workbook:
                    sst = xlsx_shared(workbook)
                    sheets = [x for x in workbook.namelist()
                              if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', x)]
                    for sheet in sheets:
                        c = xlsx_sheet_fields(workbook.read(sheet), sst)
                        if upper(c.get('A1')) != 'BOARDING PASS':
                            raise ValueError('Unknown boarding worksheet '+member+'/'+sheet)
                        record_id = f'{member}:{sheet.split("/")[-1][:-4]}'
                        sink.emit(dict(
                            source='boarding_pass', source_record_id=record_id,
                            passenger_name=c.get('B3'), loyalty_ids=c.get('F3'),
                            flight_number=c.get('A5'), departure_date=c.get('A9'),
                            departure_time=c.get('C9'), origin_airport=c.get('D7'),
                            destination_airport=c.get('H7'), origin_city=c.get('D5'),
                            destination_city=c.get('H5')))
                        if limit and sink.counts['boarding_pass'] >= limit:
                            break
            finally:
                sink.close()
            count = sum(sink.counts.values())
            manifest.append({'input_file':member, 'csv':str(target.relative_to(out_folder)), 'rows':count})
            if i % 25 == 0 or i == len(excel_files):
                print(f'  boarding workbooks: {i}/{len(excel_files)}', flush=True)
    return manifest


def main():
    ap = argparse.ArgumentParser(description='Step 1. Convert each source file into a separate unified CSV.')
    ap.add_argument('archive', type=Path)
    ap.add_argument('--out', type=Path, default=Path('result/01_sources'))
    ap.add_argument('--limit', type=int, default=0, help='Rows per input source for testing (0 = all)')
    ap.add_argument('--max-workbooks', type=int, default=0,
                    help='How many daily Excel workbooks to parse (0 = all)')
    ap.add_argument('--overwrite', action='store_true',
                    help='Clear previously generated CSVs from the source output folder')
    a = ap.parse_args()
    a.out.mkdir(parents=True,exist_ok=True)
    stale = list(a.out.rglob('*.csv'))
    if stale and not a.overwrite:
        ap.error('Output folder already contains CSVs. Use an empty folder or --overwrite.')
    if a.overwrite:
        for old_csv in stale:
            old_csv.unlink()
    sources = [('BoardingData.csv', csv_boarding),
               ('Sirena-export-fixed.tab', sirena),
               ('PointzAggregator-AirlinesData.xml', xml_pointz),
               ('SkyTeam-Exchange.yaml', yamlexchange),
               ('FrequentFlyerForum-Profiles.json', forum)]
    report=[]
    with zipfile.ZipFile(a.archive) as outer, TemporaryDirectory() as tempdir:
        for basename, parser in sources:
            member = next((x for x in outer.namelist() if x.endswith('/'+basename)),None)
            if member is None:
                raise FileNotFoundError(f'Missing {basename}')
            temporary = Path(tempdir) / basename
            with outer.open(member) as inp, temporary.open('wb') as out:
                import shutil
                shutil.copyfileobj(inp,out,1024*1024)
            target = a.out / (Path(basename).stem + '.csv')
            sink = Sink(target)
            print('Parsing', basename, '->', target.name, flush=True)
            try:
                parser(temporary,sink,a.limit)
            finally:
                sink.close()
                temporary.unlink(missing_ok=True)
            count=sum(sink.counts.values())
            report.append({'input_file':member,'csv':str(target.relative_to(a.out)),'rows':count})
            print('  rows:',count,flush=True)
        member = next((x for x in outer.namelist() if x.endswith('/YourBoardingPassDotAero.zip')), None)
        if member is None:
            raise FileNotFoundError('Missing YourBoardingPassDotAero.zip')
        temporary=Path(tempdir)/'YourBoardingPassDotAero.zip'
        with outer.open(member) as inp,temporary.open('wb') as out:
            import shutil
            shutil.copyfileobj(inp,out,1024*1024)
        report.extend(split_xlsx_archive(temporary,a.out,a.limit,a.max_workbooks))
        temporary.unlink(missing_ok=True)
    all_files={i.filename for i in zipfile.ZipFile(a.archive).infolist() if not i.is_dir()}
    skipped = [x for x in all_files if x.lower().endswith('.pdf')]
    result={'files_converted':len(report), 'records':sum(x['rows'] for x in report),
            'files':report, 'skipped_pdf':skipped,
            'why_skipped':'Skyteam_Timetable.pdf contains a 2018-2019 timetable, not passenger records for 2017.'}
    (a.out/'manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Converted:',result['files_converted'],'source files; rows:',result['records'])
    if skipped:
        print('Excluded timetable PDF (different period and no passenger records).')

if __name__ == '__main__':
    main()

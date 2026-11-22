"""Leak check: re-harvest a sample of output files and report identity-field values
that are identical to the input (i.e. were not de-identified).

python -m deid.leakcheck <report.jsonl> <input_dir> <output_dir> <n>
"""
import collections
import json
import os
import random
import sys
from multiprocessing import Pool

from . import harvest

FIELDS_SKIP = ('zipname', 'err', 'step:PRODUCT', 'step:fn_time', 'step:fn_preproc', 'step:fn_origsys', 'step:desc',
               'swhdr', 'textline', 'fc:Label', 'sat:hdr', 'stl:solid', 'stl:hdr', 'iges:preproc', 'iges:native_sys',
               'iges:filename', 'iges:start', 'svgattr', 'fc:License', 'fc:LicenseURL', 'xt:FRU', 'xt:APPL', 'xt:MC',
               'xt:MC_ID', 'step:PERSON_AND_ORGANIZATION_ROLE', 'url')


def scan(path):
    rows = []

    def emit(where, field, value):
        rows.append((field, str(value)[:200]))
    try:
        d = open(path, 'rb').read()
        harvest.scan_bytes(os.path.basename(path), d, emit, '')
    except Exception:
        pass
    return rows


def work(pair):
    a, b = pair
    ra = set(scan(a))
    rb = set(scan(b))
    same = [r for r in ra & rb if not r[0].startswith(FIELDS_SKIP) and not r[0].startswith('swprop:SW-') or r[0] in (
        'swprop:SW-Author', 'swprop:SW-Last Saved By')]
    return a, same


def main():
    rep, inp, out, n = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    rows = [json.loads(l) for l in open(rep, encoding='utf-8')]
    random.seed(11)
    rows = random.sample(rows, min(n, len(rows)))
    pairs = [(os.path.join(inp, r['in']), os.path.join(out, r['out'])) for r in rows]
    c = collections.Counter()
    ex = {}
    with Pool(24) as p:
        for a, same in p.imap_unordered(work, pairs, chunksize=4):
            for f, v in same:
                c[(f, v)] += 1
                ex.setdefault((f, v), os.path.relpath(a, inp)[:100])
    byf = collections.Counter()
    for (f, v), k in c.items():
        byf[f] += k
    print(byf.most_common(40))
    for (f, v), k in c.most_common(150):
        print(k, f, '|', v[:80], '|', ex[(f, v)])


if __name__ == '__main__':
    main()

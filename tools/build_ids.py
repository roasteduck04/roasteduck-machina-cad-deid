"""Merge labelled recall-pass identities (lab_*.tsv: file, type, identifier) into deid/ids_pdf.dat.

python tools/build_ids.py OUT.dat lab_0.tsv [lab_3.tsv ...]
Filters: identifiers shorter than 3 characters, common 3-letter words/abbreviations, and software names are dropped.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from deid import pdfids  # noqa: E402

try:
    from wordfreq import zipf_frequency as zf
except Exception:
    zf = None

SKIP = set('solidworks autocad inventor adobe microsoft kicad altium onshape fusion freecad catia creo nx word excel '
           'acrobat windows linux github gitlab google youtube wikipedia'.split())


def common(w):
    if zf is None:
        return False
    return max(zf(w.lower(), l) for l in ('en', 'de', 'fr', 'es', 'it', 'pt', 'nl')) >= 3.0


def main():
    out, files = sys.argv[1], sys.argv[2:]
    seen = set()
    rows = []
    for f in files:
        for i, line in enumerate(open(f, encoding='utf-8')):
            p = line.rstrip('\n').split('\t')
            if i == 0 or len(p) < 3:
                continue
            typ, ident = p[1].strip().lower(), re.sub(r'\s+', ' ', p[2]).strip()
            low = ident.lower()
            if len(ident) < 3 or low in SKIP or low in seen:
                continue
            if len(ident) == 3 and (not re.search(r'\d', ident)) and (common(ident) or not ident.isupper()):
                continue
            if typ in ('person', 'org', 'vendor', 'project') and ' ' not in ident and len(ident) <= 5 and common(ident):
                continue
            seen.add(low)
            rows.append([typ, ident])
    pdfids.save_blob(rows, out)
    print('identities', len(rows))


if __name__ == '__main__':
    main()

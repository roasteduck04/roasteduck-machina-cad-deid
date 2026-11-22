"""Scan SVG files for creator/rights/publisher agents (RDF metadata) -> svgmeta.jsonl."""
import json
import os
import re
import sys
from multiprocessing import Pool

AGENT_RE = re.compile(rb'<dc:(creator|rights|publisher|contributor)>\s*<cc:Agent[^>]*>\s*<dc:title>([^<]{2,200})</dc:title>', re.S)
ATTR_RE = re.compile(rb'(?:inkscape:export-filename|sodipodi:docname|sodipodi:absref)="([^"]{2,300})"')


def work(p):
    out = []
    try:
        with open(p, 'rb') as h:
            b = h.read(4_000_000)
    except Exception:
        return out
    for m in AGENT_RE.finditer(b):
        out.append((m.group(1).decode(), m.group(2).decode('utf-8', 'replace')))
    return out


def main():
    root, dst = sys.argv[1], sys.argv[2]
    files = []
    for dp, dn, fn in os.walk(root):
        for f in fn:
            if f.lower().endswith('.svg'):
                files.append(os.path.join(dp, f))
    with Pool(32) as pool, open(dst, 'w', encoding='utf-8') as fo:
        for rows in pool.imap_unordered(work, files, chunksize=16):
            for r in rows:
                fo.write(json.dumps(r, ensure_ascii=False) + '\n')


if __name__ == '__main__':
    main()

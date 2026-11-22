"""Minimal OLE property-set (MS-OLEPS) reader/writer for identity fields.

Finds string properties that hold accounts / people / companies and lets the caller
rewrite them in place with an equal-length value (stream size never changes).
"""
import struct
import uuid

FMTID_SUMMARY = 'F29F85E0-4FF9-1068-AB91-08002B27B3D9'      # Author 4, LastAuthor 8
FMTID_DOCSUMMARY = 'D5CDD502-2E9C-101B-9397-08002B2CF9AE'   # Manager 14, Company 15
FMTID_INV_DESIGN = '32853F0F-3444-11D1-9E93-0060B03C1CA6'   # Inventor Design Tracking Properties
IDENTITY_PIDS = {
    FMTID_SUMMARY: {4: 'author', 8: 'lastauthor'},
    FMTID_DOCSUMMARY: {14: 'manager', 15: 'company'},
    '3D38DE39-0588-4C14-BB37-18F4D5DD31C7': {4: 'author', 8: 'lastauthor'},      # Inventor Summary Information
    '8CF58000-DA66-4AE6-8FF0-7B58406FB049': {14: 'manager', 15: 'company'},       # Inventor Document Summary
    'D861FB30-3136-11D1-9E92-0060B03C1CA6': {16: 'user'},                          # Inventor Design Tracking Control
    'CC024FA2-6EB5-11CE-8AA2-08003601E988': {8: 'lastauthor', 20: 'author'},      # Solid Edge summary
    FMTID_INV_DESIGN: {10: 'checkedby', 12: 'engapprovedby', 34: 'mfgapprovedby', 41: 'designer', 42: 'engineer',
                       43: 'authority'},
}


def string_props(data):
    """Yield (fmtid, pid, value_offset, value_nbytes, encoding) for string properties."""
    if len(data) < 48 or struct.unpack_from('<H', data, 0)[0] != 0xFFFE:
        return
    nsec = struct.unpack_from('<I', data, 24)[0]
    for s in range(min(nsec, 8)):
        base = 28 + 20 * s
        if base + 20 > len(data):
            return
        fid = str(uuid.UUID(bytes_le=bytes(data[base:base + 16]))).upper()
        off = struct.unpack_from('<I', data, base + 16)[0]
        if off + 8 > len(data):
            continue
        size, count = struct.unpack_from('<II', data, off)
        if count > 4096:
            continue
        props = []
        for i in range(count):
            q = off + 8 + 8 * i
            if q + 8 > len(data):
                break
            props.append(struct.unpack_from('<II', data, q))
        cp = 1252
        for pid, poff in props:
            if pid == 1 and off + poff + 6 <= len(data):
                cp = struct.unpack_from('<H', data, off + poff + 4)[0]
        for pid, poff in props:
            p = off + poff
            if p + 8 > len(data):
                continue
            typ = struct.unpack_from('<H', data, p)[0]
            n = struct.unpack_from('<I', data, p + 4)[0]
            if typ == 0x1E:
                enc = 'utf-16le' if cp == 1200 else ('utf-8' if cp == 65001 else 'cp%d' % cp)
                nb = n
            elif typ == 0x1F:
                enc, nb = 'utf-16le', 2 * n
            else:
                continue
            if nb <= 0 or p + 8 + nb > len(data) or nb > 100000:
                continue
            yield fid, pid, p + 8, nb, enc


def decode(raw, enc):
    try:
        s = raw.decode(enc, 'replace')
    except LookupError:
        s = raw.decode('cp1252', 'replace')
    return s.split('\x00')[0]


def identity_fields(data):
    """Yield (label, value, offset, nbytes, enc) for identity-bearing properties."""
    for fid, pid, off, nb, enc in string_props(data):
        lab = IDENTITY_PIDS.get(fid, {}).get(pid)
        if not lab:
            continue
        v = decode(bytes(data[off:off + nb]), enc)
        if v.strip():
            yield lab, v, off, nb, enc


def rewrite(data, off, nb, enc, old, new):
    """Write `new` over the property value (same encoded length, NUL-padded)."""
    try:
        e = new.encode(enc if not enc.startswith('cp') else enc, 'replace')
    except LookupError:
        e = new.encode('cp1252', 'replace')
    try:
        eo = old.encode(enc, 'replace')
    except LookupError:
        eo = old.encode('cp1252', 'replace')
    if len(e) != len(eo):
        unit = 2 if enc == 'utf-16le' else 1
        e = (e + b'x' * len(eo))[:len(eo)] if unit == 1 else (e + 'x'.encode('utf-16le') * len(eo))[:len(eo)]
    seg = bytearray(data[off:off + nb])
    seg[:len(e)] = e
    data[off:off + nb] = seg

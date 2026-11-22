"""Pseudonym generation and same-length identity replacement.

Every identity is a normalised key (lower case).  Its pseudonym is built word
by word from a global word map, so the same word always becomes the same
pseudo-word and a key and its pseudonym always have the same length and the
same separators.  Same length lets binary CAD containers be edited in place
without moving offsets; case is copied character by character from each
occurrence, so 'ACME', 'Acme' and 'acme' map to 'QAVE', 'Qave', 'qave'.
"""
import hashlib
import json
import re
import unicodedata

import ahocorasick

from . import names

SALT = b'machina-deid-v1'

# Words kept unchanged inside multi-word identities (generic org words, TLDs).
KEEP = set("""
university universitat universite universidad universita institute institut instituto of the and for de del la le les du
des van von der den und et y e lab labs laboratory laboratories inc incorporated ltd llc gmbh ag sa sas srl bv kg co corp
corporation company group robotics technology technologies tech engineering systems industries research center centre
foundation college school department dept national community project open source hardware design designs studio works
www com org net edu gov io ac uk fr es it nl ch at be se no dk fi pl cz ru in au ca us eu info biz mil int jp cn kr tw
br mx ar http https ftp mailto html htm php pdf users user home documents settings desktop downloads appdata local roaming temp onedrive
dropbox mnt media var opt library volumes my
""".split())

STOP_KEYS = set()  # filled by registry: generic values never treated as identities


def _h(s):
    return int.from_bytes(hashlib.sha256(SALT + s.encode('utf-8')).digest()[:8], 'big')


SEG_RE = re.compile(r'([^\W\d_]+)|(\d+)|([\W_]+)', re.U)


def strip_accents(s):
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')


class WordMap:
    def __init__(self):
        self.map = {}          # word -> pseudo word (lowercase ascii, same length)
        self.used = set()
        self.forbidden = set()  # words that must never be produced (real identity words)
        pools = {}
        for w in names.FIRST + names.LAST + names.WORDS:
            if w.isalpha() and w.isascii():
                pools.setdefault(len(w), []).append(w)
        for k in pools:
            pools[k] = sorted(set(pools[k]))
        self.pools = pools

    def _gen(self, word, n, salt=0):
        h = _h(word + '#' + str(salt))
        out = []
        cons, vow = names.CONS, names.VOW
        start_v = h & 1
        for i in range(n):
            h, r = divmod(h, 97) if h else (_h(word + str(i) + str(salt)), 7)
            if (i + start_v) % 2 == 0:
                out.append(cons[r % len(cons)])
            else:
                out.append(vow[r % len(vow)])
        return ''.join(out)

    def get(self, word):
        """word: lowercase string of letters (may be non-ascii)."""
        if word in self.map:
            return self.map[word]
        n = len(word)
        cand = None
        pool = self.pools.get(n, [])
        if n >= 3 and pool:
            start = _h(word) % len(pool)
            for i in range(len(pool)):
                c = pool[(start + i) % len(pool)]
                if c not in self.used and c != word and c not in self.forbidden:
                    cand = c
                    break
        if cand is None:
            salt = 0
            while True:
                c = self._gen(word, n, salt)
                if c not in self.used and c != strip_accents(word) and c not in self.forbidden:
                    cand = c
                    break
                salt += 1
                if salt > 200 and n <= 2:
                    cand = c  # tiny words: allow reuse
                    break
                if salt > 2000 and c != strip_accents(word) and c not in self.forbidden:
                    cand = c  # crowded short-word space: allow reuse rather than loop
                    break
                if salt > 5000:
                    cand = c
                    break
        self.map[word] = cand
        self.used.add(cand)
        return cand

    def dump(self):
        return dict(self.map)

    def load(self, d):
        self.map.update(d)
        self.used.update(d.values())


class Pseudonymizer:
    def __init__(self):
        self.words = WordMap()
        self.cache = {}
        self.digit_keys = set()

    def pseudo(self, key):
        """key: lowercase identity string -> lowercase pseudonym of equal length."""
        if key in self.cache:
            return self.cache[key]
        segs = SEG_RE.findall(key)
        letter_words = [s[0] for s in segs if s[0]]
        all_keep = all(w in KEEP for w in letter_words)
        out = []
        for lw, dg, ot in segs:
            if lw:
                if lw in KEEP and not all_keep and len(letter_words) > 1:
                    out.append(lw)
                else:
                    out.append(self.words.get(lw))
            elif dg:
                if key in self.digit_keys or len(dg) >= 4:
                    h = _h('digits:' + dg)
                    nd = ''.join(str((h >> (4 * i)) % 10) for i in range(len(dg)))
                    if nd == dg:
                        nd = ''.join(str((int(c) + 3) % 10) for c in dg)
                    out.append(nd)
                else:
                    out.append(dg)
            else:
                out.append(ot)
        p = ''.join(out)
        assert len(p) == len(key), (key, p)
        self.cache[key] = p
        return p


def pseudo_det(pz, key):
    """Pseudonym for a value that may not be in the frozen registry (e.g. a generic account
    name found in a field).  Known words reuse the global word map; unknown words get a
    hash-derived pronounceable word, so every process produces the same result without
    shared state."""
    key = key.lower()
    if key in pz.cache:
        return pz.cache[key]
    out = []
    for lw, dg, ot in SEG_RE.findall(key):
        if lw:
            w = pz.words.map.get(lw)
            if w is None:
                w = pz.words._gen(lw, len(lw), 7)
                if w == strip_accents(lw):
                    w = pz.words._gen(lw, len(lw), 8)
            out.append(w)
        elif dg:
            h = _h('digits:' + dg)
            nd = ''.join(str((h >> (4 * i)) % 10) for i in range(len(dg)))
            if nd == dg:
                nd = ''.join(str((int(c) + 3) % 10) for c in dg)
            out.append(nd)
        else:
            out.append(ot)
    return ''.join(out)


def apply_case(orig, pseudo):
    """Copy the per-character case of orig onto pseudo (same length)."""
    if len(orig) != len(pseudo):
        return pseudo
    if orig.isupper():
        return pseudo.upper()
    if orig.islower():
        return pseudo
    return ''.join(p.upper() if o.isupper() else p for o, p in zip(orig, pseudo))


def is_alnum_char(c):
    return c.isalnum()


class Matcher:
    """Finds identity keys in text or bytes and replaces them, length-preserving.

    entries: dict key -> mode, where mode is 'strict' (no letter/digit on either
    side), 'loose' (match anywhere) or 'word' (letters may not touch letters,
    digits allowed next to it).
    """

    def __init__(self, pz, entries):
        self.pz = pz
        self.entries = entries
        self.text_ac = ahocorasick.Automaton()
        self.bin_ac = ahocorasick.Automaton()
        for key, mode in entries.items():
            if not key:
                continue
            variants = {key}
            if not key.isascii():
                variants.add(unicodedata.normalize('NFD', key))
                variants.add(unicodedata.normalize('NFC', key))
            for var in variants:
                if not self.text_ac.exists(var):
                    self.text_ac.add_word(var, (var, key, mode))
                forms = set()
                try:
                    forms.add(('u8', var.encode('utf-8').decode('latin-1')))
                except Exception:
                    pass
                try:
                    forms.add(('cp', var.encode('cp1252').decode('latin-1')))
                except Exception:
                    pass
                if len(var) >= 3:
                    forms.add(('u16', var.encode('utf-16le').decode('latin-1')))
                for enc, f in forms:
                    if self.bin_ac.exists(f):
                        continue
                    self.bin_ac.add_word(f, (f, key, mode, enc))
        if len(self.text_ac):
            self.text_ac.make_automaton()
        if len(self.bin_ac):
            self.bin_ac.make_automaton()

    protect = None          # set of lower-case file stems that keep their names (see pipeline.run)
    _REF_CH = frozenset('abcdefghijklmnopqrstuvwxyz0123456789_-.')

    def _in_file_ref(self, hay, st, en):
        """True when the match sits inside a token that names a corpus file whose name is kept, e.g.
        'ab_xyz_003_p_143.prt' inside a Creo assembly: replacing there would break the reference."""
        a, z = st, en
        lim = 120
        while a > 0 and hay[a - 1] in self._REF_CH and st - a < lim:
            a -= 1
        while z < len(hay) and hay[z] in self._REF_CH and z - en < lim:
            z += 1
        tok = hay[a:z].strip('.-_')
        if not tok or tok == hay[st:en]:
            return False
        for _ in range(3):
            if tok in self.protect:
                return True
            if '.' not in tok:
                break
            tok = tok.rsplit('.', 1)[0]
        return tok in self.protect

    @staticmethod
    def _ok_boundary(mode, before, after, key):
        if mode == 'loose':
            return True
        if mode == 'strict':
            return not (before and before.isalnum()) and not (after and after.isalnum())
        # 'word': letters may not touch letters; digits may not touch digits
        if before:
            if before.isalpha() and key[0].isalpha():
                return False
            if before.isdigit() and key[0].isdigit():
                return False
        if after:
            if after.isalpha() and key[-1].isalpha():
                return False
            if after.isdigit() and key[-1].isdigit():
                return False
        return True

    @staticmethod
    def _lower(s):
        lo = s.lower()
        if len(lo) == len(s):
            return lo
        return ''.join(c.lower() if len(c.lower()) == 1 else c for c in s)

    def _select(self, cands):
        # leftmost-longest non-overlapping
        cands.sort(key=lambda t: (t[0], -(t[1] - t[0])))
        out, last = [], -1
        for c in cands:
            if c[0] >= last:
                out.append(c)
                last = c[1]
        return out

    def find_text(self, s):
        if not s or not len(self.text_ac):
            return []
        hay = self._lower(s)
        cands = []
        for end, (pat, key, mode) in self.text_ac.iter(hay):
            st = end - len(pat) + 1
            before = hay[st - 1] if st > 0 else ''
            after = hay[end + 1] if end + 1 < len(hay) else ''
            if self._ok_boundary(mode, before, after, key):
                cands.append((st, end + 1, key))
        return self._select(cands)

    def text(self, s, hits=None):
        """Replace identities in a str. Returns new str (same length)."""
        found = self.find_text(s)
        if not found:
            return s
        out, pos = [], 0
        for st, en, key in found:
            out.append(s[pos:st])
            orig = unicodedata.normalize('NFC', s[st:en])
            out.append(apply_case(orig, self.pz.pseudo(key)))
            if hits is not None:
                hits.append(key)
            pos = en
        out.append(s[pos:])
        return ''.join(out)

    def bytes(self, b, hits=None, min_len_bin=3, min_len_u16=None):
        """Same-length replacement inside raw bytes (utf-8/cp1252/utf-16le)."""
        if not b or not len(self.bin_ac):
            return b
        s = b.decode('latin-1')
        hay = s.lower()
        cands = []
        for end, (pat, key, mode, enc) in self.bin_ac.iter(hay):
            flen = len(pat)
            st = end - flen + 1
            if len(key) < (min_len_u16 if (enc == 'u16' and min_len_u16) else min_len_bin):
                continue
            if enc == 'u16':
                if st % 1:
                    continue
                before = hay[st - 2] if st >= 2 and hay[st - 1] == '\x00' else ''
                after = hay[end + 1] if end + 2 < len(hay) and hay[end + 2] == '\x00' else ''
            else:
                before = hay[st - 1] if st > 0 else ''
                after = hay[end + 1] if end + 1 < len(hay) else ''
            if self._ok_boundary(mode, before, after, key):
                if enc != 'u16' and self.protect and self._in_file_ref(hay, st, end + 1):
                    continue
                cands.append((st, end + 1, key, enc))
        if not cands:
            return b
        sel = self._select(cands)
        out, pos = [], 0
        for st, en, key, enc in sel:
            out.append(b[pos:st])
            raw = b[st:en]
            if enc == 'u16':
                orig = unicodedata.normalize('NFC', raw.decode('utf-16le', 'replace'))
                rep = apply_case(orig, self.pz.pseudo(key)).encode('utf-16le')
            elif enc == 'u8':
                orig = unicodedata.normalize('NFC', raw.decode('utf-8', 'replace'))
                rep = apply_case(orig, self.pz.pseudo(key)).encode('utf-8')
            else:
                orig = raw.decode('cp1252', 'replace')
                rep = apply_case(orig, self.pz.pseudo(key)).encode('cp1252', 'replace')
            if len(rep) != len(raw):
                # non-ascii original: pad/trim ascii pseudonym to the byte length
                p = apply_case(orig, self.pz.pseudo(key))
                p = strip_accents(p)
                unit = 2 if enc == 'u16' else 1
                need = len(raw) // unit
                p = (p + 'x' * need)[:need]
                rep = p.encode('utf-16le' if enc == 'u16' else 'latin-1', 'replace')
            out.append(rep)
            if hits is not None:
                hits.append(key)
            pos = en
        out.append(b[pos:])
        res = b''.join(out)
        assert len(res) == len(b)
        return res

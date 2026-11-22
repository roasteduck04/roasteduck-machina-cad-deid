"""Curated identities that live in folder names and are not discoverable from file metadata
(organisations, people, projects, web domains).  The list itself is stored compressed in curated.dat so
the code folder carries no plain-text corpus identifiers.

Each entry is (string, mode).  mode: 'strict' = whole word only, 'loose' = also inside longer tokens
(only for long, distinctive strings), 'path' = only applied to file and folder names.
"""
import os

from .pdfids import load_blob

ORGS_PEOPLE = [tuple(x) for x in (load_blob(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'curated.dat')) or [])]

"""Normalize separators for catalogue queries and indexed text."""
import re
import unicodedata


SEPARATORS = re.compile(r'[-_/\u2010-\u2015\u2212\s]+')


def search_text(text):
    return SEPARATORS.sub(' ', unicodedata.normalize('NFKC', text).casefold()).strip()

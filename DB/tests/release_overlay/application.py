"""Adapt only the temporary B2 application's query to its renamed column."""
from pathlib import Path
import sys

path = Path(sys.argv[1]) / "DB/people_pubs/api/database.py"
original = '_COLUMNS = sql.SQL(", ").join(sql.Identifier(name) for name in PUBLICATION_FIELDS)'
replacement = ('_COLUMNS = sql.SQL(", ").join(sql.SQL("test_overlay_title AS title") if name == "title" '
               'else sql.Identifier(name) for name in PUBLICATION_FIELDS)')
text = path.read_text(encoding="utf-8")
if text.count(original) != 1:
    raise SystemExit("the temporary application overlay cannot find the column query")
path.write_text(text.replace(original, replacement), encoding="utf-8")

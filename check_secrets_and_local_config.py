#!/usr/bin/env python3
"""Check the publishable tree for committed secrets and local configuration.

Standard library only:

    python3 check_secrets_and_local_config.py              # the publishable tree
    python3 check_secrets_and_local_config.py PATH [PATH]  # only these files
    python3 check_secrets_and_local_config.py --base REV [--head REV]
                                       # the tree and the commits REV..HEAD add
    python3 check_secrets_and_local_config.py --list-rules

Checked files: every file the repository would publish -- the tracked files
plus untracked, nonignored ones, exactly the set that
`git ls-files --cached --others --exclude-standard` prints -- so a new file is
checked before it is staged, and a clean checkout checks the tracked tree.
Ignored paths (virtual environments, caches, local configuration such as
DB/.env, database state and backups) are never opened, and a file deleted from
the working tree is not read. With explicit paths, those files are checked
instead.

Checked commits, with --base: a file that one commit adds and a later one
removes is gone from the tree but stays in the published history, so the
commits a push or a pull request adds are checked as well -- every file each
commit in base..head adds or changes, with the same rules, the files' former
contents included; for a merge, what it changes relative to all of its
parents. Commits that base already contains are not read again. An empty or
all-zero base -- the first push of a branch or repository, where nothing
bounds the new commits -- and a base this clone does not contain (a rewritten
history) mean every commit reachable from head. The commit check needs the
complete history: in a shallow clone, or when an object cannot be read, it
cannot be completed. Findings in commits that are published already, and so
cannot change any more, can be accepted one by one in ACCEPTED_IN_HISTORY.

Every finding is one line, `path:line: rule-id`, or `path: rule-id` for a
finding about the file itself; in a commit, `commit:path:line: rule-id`, the
commit abbreviated as git does. The output is sorted and never contains the
matched text, so a report can be shared without spreading what it found. Exit
status: 0 when nothing was found, 1 on findings, 2 when the check could not be
completed (no Git checkout, an unreadable file, a shallow clone or an
unreadable object for the commit check).

Placeholders. A value is not reported when it is empty; when it is, as a
whole, a template field or a description of one -- `{{ name }}`, `{name}`,
`%(name)s`, `<password>`, `[redacted]` -- or a mask such as `...`, `***` or
`xxxx`; when it is a conventional placeholder word, alone or followed by more
words (`change-me-postgres`, `your-password-here`, `example`, `placeholder`,
`dummy`, `redacted`); or when it refers to its value instead of containing it:
`$NAME`, `${NAME}`, `${NAME:-default}`, `$(command)` or `${{ expression }}`. A
fallback or alternate value written into an expansion -- `${NAME:-value}`,
`${NAME-value}`, `${NAME:=value}`, `${NAME:+value}` and their nested forms --
is a value the expansion can produce, so it has to be a placeholder or a
reference itself: `API_KEY="${API_KEY:-<literal key>}"` is a finding. A
value composed at run time from such references and at most twelve lower-case
letters, digits and separators, as in `smoke-$RUN_ID`, is accepted only in
shell code -- scripts, Dockerfiles, shell snippets in Markdown and
Compose-style `KEY=value` list items -- because only there is it expanded. In
env files (which people_pubs reads without expansion), configuration files,
JSON and Python strings, and as the password of a URL or connection string, a
value has to be a reference or template as a whole. In shell code and env
files single-quoted text is literal, as the shell reads it. A `$` or an
opening bracket somewhere in a value is not enough. A recognisable token
counts as a placeholder when it contains `EXAMPLE` or similar wording or
repeats one character after its prefix, and a private-key header when the
block it opens holds a placeholder.

Which rule applies where:

- file names and paths: every candidate file;
- private keys, token formats, passwords in URLs and connection strings,
  home-directory paths, private network addresses: every text file;
- secret-named settings with a literal value: env files and their templates,
  configuration files (YAML, JSON, TOML, INI and similar), shell scripts,
  Dockerfiles, Markdown, and Python source. Python is read as a syntax tree,
  wherever the statement stands (also in a one-line `if`, `class` or `def`,
  or after a semicolon): the targets of assignments, attributes and subscript
  keys (`API_KEY = ...`, `self.password = ...`, `os.environ["PGPASSWORD"] =
  ...`), keyword arguments (`Client(api_key=...)`), dictionary entries
  (`{"api_key": ...}`), parameter defaults, and the key of a lookup with a
  fallback (`os.environ.get("API_KEY", ...)`, `settings.get("password", ...)`).
  The value counts as literal when it is a string, a concatenation of
  strings, an f-string whose literal text is more than a short affix, either
  operand of `or`, a branch of a conditional expression, or the fallback of
  such a lookup. Values obtained elsewhere -- `token=args.token`,
  `os.environ["API_KEY"]`, a lookup without a fallback or with a placeholder
  fallback, an f-string such as `f"smoke-{run_id}"` -- and comments and
  docstrings are not reported. A file that does not parse is read statement
  by statement, for simple assignments of one string only.

Token-, API-key- and credential-like names are reported only for a value
shaped like a credential (eight or more characters mixing letters and
digits), because such names also hold identifiers (`source_token: crossref`);
password- and secret-like names are reported for any literal. This is a
pattern check with known limits, not proof that no secret is present: a
literal under an unremarkable name, a literal passed positionally, an unusual
token format, a secret in a binary file, and history older than the checked
commits are not recognised.
"""
from __future__ import annotations

import argparse
import ast
import io
import os
import re
import subprocess
import sys
import tokenize
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent

# rule id -> what it means; printed by --list-rules and next to findings.
# Written as pairs rather than a dictionary display: ids such as url-password
# or private-key are secret-like names, and the Python rule below would read
# their descriptions as literal values of such keys.
RULES: Dict[str, str] = dict(
    (
        ("env-file", "an environment file (.env, .env.<name>, <name>.env, .envrc) "
         "other than an .example, .sample or .template file"),
        ("key-file", "a private-key or key-store file (id_rsa and its siblings, "
         ".key, .p12, .pfx, .jks, .keystore, .ppk, .kdbx)"),
        ("credential-file", "a credential store (.pgpass, .netrc, .git-credentials, "
         ".pypirc, .dockercfg, .aws/credentials, .docker/config.json, or anything "
         "under an .ssh directory)"),
        ("database-dump", "a database dump or backup (.dump, .pgdump, .backup, .bak, "
         "compressed .sql, SQLite files, or a file whose contents are a dump)"),
        ("local-state", "machine-local state: virtual environments, caches, "
         "bytecode, logs, editor and OS files, or this repository's runtime "
         "locations DB/data/, DB/backups/ and DB/cron"),
        ("local-symlink", "a symbolic link whose target is absolute or outside the "
         "repository"),
        ("private-key", "private-key material (a PEM, OpenSSH or PGP private-key "
         "block, or a PuTTY key file)"),
        ("aws-access-key-id", "an AWS access key id"),
        ("github-token", "a GitHub access token"),
        ("gitlab-token", "a GitLab personal access token"),
        ("slack-token", "a Slack token"),
        ("slack-webhook", "a Slack incoming-webhook URL"),
        ("stripe-secret-key", "a Stripe live secret or restricted key"),
        ("google-api-key", "a Google API key"),
        ("npm-token", "an npm access token"),
        ("pypi-token", "a PyPI upload token"),
        ("sendgrid-key", "a SendGrid API key"),
        ("huggingface-token", "a Hugging Face access token"),
        ("sk-secret-key", "a secret API key in the common sk- format"),
        ("jwt", "a JSON Web Token"),
        ("url-password", "a URL or connection URI with a literal password "
         "(user:password@host, or a password= query parameter)"),
        ("dsn-password", "a key/value connection string with a literal password= "
         "value"),
        ("secret-assignment", "a password, secret, token or API-key setting with a "
         "literal value, or with a literal fallback such as ${NAME:-value} or "
         "os.environ.get(\"NAME\", \"value\")"),
        ("home-path", "an absolute path into a user's home directory"),
        ("private-address", "an RFC 1918 private IPv4 address"),
    )
)

EXIT_CLEAN, EXIT_FINDINGS, EXIT_ERROR = 0, 1, 2

# --------------------------------------------------------------------------
# Placeholders and references
# --------------------------------------------------------------------------
# A whole value that is a template field, or a description standing in for one.
_TEMPLATE_RE = re.compile(
    r"<[A-Za-z][A-Za-z _.-]*>"
    r"|\{\{[^{}]*\}\}"
    r"|\$\{\{[^{}]*\}\}"
    r"|\{(?:[A-Za-z_][A-Za-z0-9_.]*|[0-9]*)(?:![rsa])?\}"
    r"|%\([A-Za-z_][A-Za-z0-9_]*\)[sdr]"
    r"|[\[(](?:redacted|hidden|removed|omitted|placeholder|secret|none)[\])]",
    re.IGNORECASE,
)
_MASK_RE = re.compile("[*xX.#\u2026-]+")
_PLACEHOLDER_WORD_RE = re.compile(
    r"(?:change[-_ ]?me|replace[-_ ]?me|your|my|example|placeholder|dummy|redacted"
    r"|fake|sample|todo|tbd|none|null|nil|empty)(?:[-_. ][A-Za-z0-9_.-]*)?",
    re.IGNORECASE,
)
_GENERIC_WORDS = frozenset(
    {"password", "passwd", "pass", "pwd", "secret", "token", "key", "apikey",
     "api-key", "api_key", "user", "username"}
)
# $NAME and the positional parameters $1-$9, $@ and $*.
_NAME_REFERENCE_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|[1-9@*])")
_PERCENT_REFERENCE_RE = re.compile(r"%\([A-Za-z_][A-Za-z0-9_]*\)[sdr]")
# Single-quoted shell text is literal; only a variable name spelled out that
# way, such as '$PGPASS', is still no secret.
_SPELLED_REFERENCE_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*|\$\{[A-Za-z_][^{}]*\}")
# What a value composed at run time may keep besides its references: a short
# lower-case prefix or suffix such as "smoke-" in smoke-$RUN_ID.
_COMPOSED_LITERAL_RE = re.compile(r"[a-z0-9._:/@-]{0,12}")
_CLOSING = {"(": ")", "{": "}"}


def _closing_index(text: str, start: int) -> Optional[int]:
    """Index just past the bracket that closes text[start], or None."""
    expected: List[str] = []
    for index in range(start, len(text)):
        char = text[index]
        if char in _CLOSING:
            expected.append(_CLOSING[char])
        elif char in ")}":
            if not expected or char != expected[-1]:
                return None
            expected.pop()
            if not expected:
                return index + 1
    return None


# ${NAME:-word}, ${NAME-word}, ${NAME:=word}, ${NAME=word}, ${NAME:+word} and
# ${NAME+word}: the word is a value the expansion itself can produce, so it is
# judged like any other value.
_EXPANSION_WORD_RE = re.compile(r"\$\{(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[@*]):?[-=+]")


def _references(text: str) -> Optional[Tuple[str, List[str]]]:
    """(remainder, words) for text holding well-formed references, else None.

    remainder is the text without its references; words are the fallback and
    alternate values written inside ${...} expansions. References: $NAME,
    $1-$9, $@, $*, ${...} and $(...) with nesting, ${{ ... }}, {{ ... }} and
    %(name)s.
    """
    kept: List[str] = []
    words: List[str] = []
    found = False
    index = 0
    while index < len(text):
        end: Optional[int] = None
        if text.startswith(("${{", "{{"), index):
            close = text.find("}}", index)
            end = close + 2 if close >= 0 else None
        elif text.startswith(("${", "$("), index):
            end = _closing_index(text, index + 1)
            word = _EXPANSION_WORD_RE.match(text, index) if end is not None else None
            if word is not None and word.end() < end:
                words.append(text[word.end() : end - 1])
        elif text.startswith("%(", index):
            match = _PERCENT_REFERENCE_RE.match(text, index)
            end = match.end() if match else None
        elif text.startswith("$", index):
            match = _NAME_REFERENCE_RE.match(text, index)
            end = match.end() if match else None
        if end is None:
            kept.append(text[index])
            index += 1
        else:
            found = True
            index = end
    return ("".join(kept), words) if found else None


def _split_quotes(value: str) -> Tuple[str, str]:
    """(text, quote): the value without one pair of enclosing quotes, and that quote."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"`":
        return value[1:-1].strip(), value[0]
    return value, ""


def _exposed_literals(value: str, *, shell: bool = False, composed: bool = False) -> List[str]:
    """The literal text a value can produce: [] for a placeholder or a value
    that refers to its content, the value itself for a plain literal, and the
    literal fallback of an expansion such as ${NAME:-literal}."""
    text, quote = _split_quotes(value)
    if not text:
        return []
    if (
        _TEMPLATE_RE.fullmatch(text)
        or _MASK_RE.fullmatch(text)
        or _PLACEHOLDER_WORD_RE.fullmatch(text)
        or text.lower() in _GENERIC_WORDS
    ):
        return []
    if shell and quote == "'":
        return [] if _SPELLED_REFERENCE_RE.fullmatch(text) else [text]
    found = _references(text)
    if found is None:
        return [text]
    remainder, words = found
    exposed: List[str] = []
    for word in words:
        exposed.extend(_exposed_literals(word, shell=shell, composed=composed))
    remainder = remainder.replace('"', "")  # double quotes only group
    if remainder and not (composed and _COMPOSED_LITERAL_RE.fullmatch(remainder)):
        exposed.append(text)
    return exposed


def is_placeholder(value: str, *, shell: bool = False, composed: bool = False) -> bool:
    """True for a documented placeholder or a value that refers to its content.

    shell: the value is shell code or an env-file value, where single-quoted
    text is literal. composed: a value built at run time from references and a
    short literal affix counts too -- only for syntaxes that expand such
    references, which is shell code; everywhere else a value has to be a
    reference or template as a whole. Either way, a fallback written into a
    reference, as in ${NAME:-literal}, has to be a placeholder itself.
    """
    return not _exposed_literals(value, shell=shell, composed=composed)


def _placeholder_token(token: str) -> bool:
    if re.search(r"example|placeholder|dummy|redacted|fake|x{6,}|0{8,}", token, re.IGNORECASE):
        return True
    body = re.sub(r"^[A-Za-z]+[-_.]", "", token)
    return len(set(body.replace("-", "").replace("_", ""))) <= 1


# --------------------------------------------------------------------------
# File-level rules
# --------------------------------------------------------------------------
_TEMPLATE_SUFFIXES = (".example", ".sample", ".template")
_KEY_FILE_NAMES = frozenset(
    {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_ecdsa_sk", "id_ed25519_sk"}
)
_KEY_FILE_SUFFIXES = (".key", ".p12", ".pfx", ".jks", ".keystore", ".ppk", ".kdbx")
_CREDENTIAL_NAMES = frozenset(
    {".pgpass", ".netrc", "_netrc", ".git-credentials", ".pypirc", ".dockercfg"}
)
_CREDENTIAL_PATHS = (".aws/credentials", ".docker/config.json")
_DUMP_SUFFIXES = (
    ".dump", ".pgdump", ".backup", ".bak", ".sql.gz", ".sql.bz2", ".sql.xz",
    ".sql.zst", ".sqlite", ".sqlite3", ".db",
)
_LOCAL_STATE_DIRS = frozenset(
    {".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
     ".tox", ".nox", "node_modules", ".ipynb_checkpoints", ".idea"}
)
_LOCAL_STATE_NAMES = frozenset({".ds_store", "thumbs.db", "desktop.ini", ".coverage"})
_LOCAL_STATE_SUFFIXES = (".pyc", ".pyo", ".log", ".pid", ".sock", ".swp", ".swo")
# This repository's own machine-local locations, as its .gitignore lists them.
_REPOSITORY_LOCAL_PREFIXES = ("DB/data/", "DB/backups/")
_REPOSITORY_LOCAL_FILES = frozenset({"DB/cron"})


def is_env_file(name: str) -> bool:
    lower = name.lower()
    if lower.endswith(_TEMPLATE_SUFFIXES):
        return False
    return (
        lower in (".env", ".envrc")
        or lower.startswith(".env.")
        or lower.endswith(".env")
        or ".env." in lower
    )


def path_findings(path: str) -> List[str]:
    """Rules decided by the path alone."""
    parts = PurePosixPath(path).parts
    name = parts[-1]
    lower = name.lower()
    found: List[str] = []
    if is_env_file(name):
        found.append("env-file")
    if lower in _KEY_FILE_NAMES or lower.endswith(_KEY_FILE_SUFFIXES):
        found.append("key-file")
    if (
        lower in _CREDENTIAL_NAMES
        or path.endswith(_CREDENTIAL_PATHS)
        or ".ssh" in parts[:-1]
    ):
        found.append("credential-file")
    local = (
        any(part in _LOCAL_STATE_DIRS for part in parts[:-1])
        or lower in _LOCAL_STATE_NAMES
        or lower.endswith(_LOCAL_STATE_SUFFIXES)
        or name.endswith("~")
        or path.startswith(_REPOSITORY_LOCAL_PREFIXES)
        or path in _REPOSITORY_LOCAL_FILES
    )
    if local:
        found.append("local-state")
    elif lower.endswith(_DUMP_SUFFIXES):
        found.append("database-dump")
    return found


# --------------------------------------------------------------------------
# Content rules
# --------------------------------------------------------------------------
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")
_PUTTY_KEY_RE = re.compile(r"^PuTTY-User-Key-File-\d+:")
_PEM_HEADER_RE = re.compile(r"^(?:Proc-Type|DEK-Info|Comment):")

_TOKEN_RULES: List[Tuple[str, "re.Pattern[str]"]] = [
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("slack-webhook", re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9/_-]{20,}")),
    ("stripe-secret-key", re.compile(r"\b[rs]k_live_[A-Za-z0-9]{16,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("pypi-token", re.compile(r"\bpypi-AgE[A-Za-z0-9_-]{50,}")),
    ("sendgrid-key", re.compile(r"\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}\b")),
    ("huggingface-token", re.compile(r"\bhf_[A-Za-z0-9]{34,}\b")),
    ("sk-secret-key", re.compile(r"\bsk-[A-Za-z0-9_-]{32,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
]

# scheme://user:password@host -- the user part may be empty.
_URL_USERINFO_RE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/?#@:'\"<>]*:(?P<secret>[^\s/?#@'\"]+)@"
)
_URL_QUERY_RE = re.compile(r"[?&;](?:password|passwd|pwd)=(?P<secret>[^&\s'\"#]+)", re.IGNORECASE)
# A run of two or more whitespace-separated libpq keyword=value pairs, as in
# "host=... dbname=... password=...". Keyword arguments in program source are
# comma-separated and never form such a run.
_KEYWORD_RUN_RE = re.compile(
    r"(?:^|(?<=[\s'\"`]))[a-z_]+=[^\s'\",()`]+(?:\s+[a-z_]+=[^\s'\",()`]+)+"
)
_LIBPQ_ADDRESS_KEYS = frozenset({"host", "hostaddr", "port", "dbname", "user"})

_HOME_PATH_RE = re.compile(r"(?<![\w.~-])/(?:home|Users)/(?P<user>[A-Za-z0-9._-]+)")
_WINDOWS_HOME_RE = re.compile(
    r"(?<!\w)[A-Za-z]:(?:\\\\|\\|/)Users(?:\\\\|\\|/)(?P<user>[A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
_PLACEHOLDER_USERS = frozenset(
    {"user", "username", "you", "yourname", "your-name", "your_name", "me",
     "name", "example", "someone"}
)
_PRIVATE_ADDRESS_RE = re.compile(
    r"(?<![\w.-])(?:10(?:\.\d{1,3}){3}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}"
    r"|192\.168(?:\.\d{1,3}){2})(?![\w-]|\.\d)"
)
_DUMP_HEADER_RE = re.compile(
    r"^--\s+(?:PostgreSQL database dump|Dumped from database version|MySQL dump)\b"
)
_BINARY_DUMP_MAGIC = (b"PGDMP", b"SQLite format 3\x00")

# Secret-named settings. Password- and secret-like names are reported for any
# literal value; token-, API-key- and credential-like names only for a value
# shaped like a credential (8+ characters mixing letters and digits), because
# such names also hold plain identifiers ("source_token: crossref").
_STRICT_KEY_RE = re.compile(
    r"(?:password|passwd|passphrase|pgpass)$"
    r"|(?:^|[_.-])(?:pass|pwd|secret|secret[_.-]?key|client[_.-]?secret|private[_.-]?key)$",
    re.IGNORECASE,
)
_TOKEN_KEY_RE = re.compile(
    r"(?:^|[_.-])(?:token|api[_.-]?key|apikey|access[_.-]?key|credentials?)$",
    re.IGNORECASE,
)
_NOT_SECRET_KEYS = frozenset({"pwd", "oldpwd"})
_NON_VALUES = frozenset({"true", "false", "yes", "no", "on", "off", "|", ">", "|-", ">-", "|+", ">+"})
# KEY= anywhere on a line of shell code, a Dockerfile or Markdown; the value is
# read as a shell word. An "&" does not start one, so a URL query
# ("?a=1&b=2") is left to the URL rules.
_SHELL_KEY_RE = re.compile(
    r"(?:^|(?<=[\s;|(\"'`]))(?:export\s+|--?)?(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)="
)
# KEY=value at the start of a line of an env file, also commented out; the
# value is the rest of the line.
_ENV_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:#+\s*)?(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)\s*=(?P<value>.*)$"
)
# key: value / key = value at the start of a line: YAML, TOML, INI and similar.
_MAPPING_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:-\s+)?[\"']?(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)[\"']?\s*[:=]\s*(?P<value>.*)$"
)
_JSON_ASSIGNMENT_RE = re.compile(r"\"(?P<key>[^\"\\]+)\"\s*:\s*\"(?P<value>(?:[^\"\\]|\\.)*)\"")

_SHELL_SUFFIXES = (".sh", ".bash", ".zsh", ".ksh", ".md", ".markdown")
_MAPPING_SUFFIXES = (
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".cnf", ".properties", ".tfvars",
)


def _assignment_styles(path: str) -> Set[str]:
    name = PurePosixPath(path).name.lower()
    template = name.endswith(_TEMPLATE_SUFFIXES)
    base = name.rsplit(".", 1)[0] if template else name
    styles: Set[str] = set()
    if template or is_env_file(base) or ".env" in base:
        styles.update(("env", "shell"))
    if base.endswith(_SHELL_SUFFIXES) or base.startswith(("dockerfile", "containerfile")):
        styles.add("shell")
    if base.endswith(_MAPPING_SUFFIXES):
        styles.add("mapping")
    if base.endswith((".yml", ".yaml")):
        styles.add("shell")  # Compose-style "- KEY=value" list items
    if base.endswith(".json"):
        styles.add("json")
    if base.endswith(".py"):
        styles.add("python")
    return styles


def _is_value(literal: str) -> bool:
    return bool(literal) and literal.lower() not in _NON_VALUES and not literal.isdigit()


def _credential_shaped(literal: str) -> bool:
    return (
        len(literal) >= 8
        and re.search(r"[A-Za-z]", literal) is not None
        and re.search(r"\d", literal) is not None
    )


def _secret_literal(key: str, value: str, *, shell: bool = False, composed: bool = False) -> bool:
    """True if a secret-named key holds a literal value, directly or as the
    fallback of a reference. composed: accept a value built from expansions
    plus a short affix, as only shell code does."""
    key = key.strip().lstrip("-")
    if key.lower() in _NOT_SECRET_KEYS:
        return False
    strict = bool(_STRICT_KEY_RE.search(key))
    if not strict and not _TOKEN_KEY_RE.search(key):
        return False
    literal, _quote = _split_quotes(value)
    if not _is_value(literal):
        return False
    exposed = [
        _split_quotes(item)[0]
        for item in _exposed_literals(value, shell=shell, composed=composed)
    ]
    exposed = [item for item in exposed if _is_value(item)]
    if strict:
        return bool(exposed)
    return any(_credential_shaped(item) for item in exposed)


def _line_value(raw: str, comment: str) -> str:
    """A value that runs to the end of the line: its quoted part, or the text
    before an inline comment introduced by whitespace and a `comment` character."""
    value = raw.strip()
    if value and value[0] in "\"'":
        end = value.find(value[0], 1)
        return value[: end + 1] if end > 0 else value
    value = re.split(r"\s+[" + re.escape(comment) + "]", value, maxsplit=1)[0]
    return value.rstrip(",").strip()


def _quote_end(line: str, start: int) -> int:
    """Index of the quote closing the one at line[start], or -1."""
    if line[start] == "'":
        return line.find("'", start + 1)
    index = start + 1
    while index < len(line):
        char = line[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            return index
        if char == "$" and line[index + 1 : index + 2] in ("(", "{"):
            end = _closing_index(line, index + 1)
            if end is None:
                return -1
            index = end
            continue
        index += 1
    return -1


def _shell_word(line: str, start: int) -> str:
    """The shell word starting at line[start]: quotes, $(...) and ${...}
    included, and a template such as `{{ name }}` or `<your password>` kept
    whole even when it holds spaces."""
    for opening, closing in (("{{", "}}"), ("<", ">")):
        if line.startswith(opening, start):
            end = line.find(closing, start + len(opening))
            if end >= 0:
                return line[start : end + len(closing)]
    index = start
    depth = 0
    while index < len(line):
        char = line[index]
        if char in " \t;&|`" or (char == ")" and depth == 0):
            break
        if char in "'\"":
            close = _quote_end(line, index)
            if close < 0:
                return line[start:]
            index = close + 1
            continue
        if char == "$" and line[index + 1 : index + 2] in ("(", "{"):
            end = _closing_index(line, index + 1)
            index = end if end is not None else len(line)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        index += 1
    return line[start:index]


_PYTHON_IGNORED = frozenset(
    {tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING}
)


def _string_assignment(tokens: List[tokenize.TokenInfo]) -> Optional[Tuple[int, str, str]]:
    """(line, name, value) if the statement is `target [: type] = "literal"`,
    where target is a name or a dotted attribute."""
    if len(tokens) < 3 or tokens[0].type != tokenize.NAME:
        return None
    if tokens[-1].type != tokenize.STRING or tokens[-2].string != "=":
        return None
    name = tokens[0].string
    index = 1
    while (
        index + 1 < len(tokens) - 2
        and tokens[index].string == "."
        and tokens[index + 1].type == tokenize.NAME
    ):
        name = tokens[index + 1].string
        index += 2
    annotation = tokens[index:-2]
    if annotation and (annotation[0].string != ":" or any(t.string == "=" for t in annotation)):
        return None
    literal = tokens[-1].string
    if "f" in re.match(r"[A-Za-z]*", literal).group(0).lower():  # type: ignore[union-attr]
        return None  # an f-string is built at run time
    try:
        value = ast.literal_eval(literal)
    except (ValueError, SyntaxError):
        return None
    if isinstance(value, bytes):
        value = value.decode("latin-1")
    return tokens[0].start[0], name, value


def _python_assignments_by_tokens(text: str) -> List[Tuple[int, str, str]]:
    """Statements that assign one string literal to a name or an attribute;
    the fallback for a file that does not parse as Python."""
    found: List[Tuple[int, str, str]] = []
    statement: List[tokenize.TokenInfo] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type in (tokenize.NEWLINE, tokenize.ENDMARKER):
                assignment = _string_assignment(statement)
                if assignment is not None:
                    found.append(assignment)
                statement = []
            elif token.type not in _PYTHON_IGNORED:
                statement.append(token)
    except (tokenize.TokenError, SyntaxError):
        pass  # not even tokenizable; the other rules still apply to the file
    return found


# Calls that look a value up by key and fall back to their second argument:
# os.environ.get, os.getenv, dict.get, setdefault.
_LOOKUP_CALLS = frozenset({"get", "getenv", "setdefault"})


def _string_parts(node: ast.AST) -> Tuple[str, bool]:
    """(literal text, composed) of an expression that builds a string; composed
    means part of it is computed at run time."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
        value = node.value
        return (value.decode("latin-1") if isinstance(value, bytes) else value), False
    if isinstance(node, ast.JoinedStr):
        text, composed = "", False
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                text += part.value
            else:
                composed = True
        return text, composed
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, left_composed = _string_parts(node.left)
        right, right_composed = _string_parts(node.right)
        return left + right, left_composed or right_composed
    return "", True


def _lookup(node: ast.AST) -> Optional[Tuple[Optional[str], Optional[ast.AST]]]:
    """(key, fallback) of a get/getenv/setdefault call, else None."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name not in _LOOKUP_CALLS:
        return None
    first = node.args[0] if node.args else None
    key = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None
    fallback = node.args[1] if len(node.args) > 1 else None
    for keyword in node.keywords:
        if keyword.arg == "default":
            fallback = keyword.value
    return key, fallback


def _python_literals(node: Optional[ast.AST]) -> List[Tuple[int, str]]:
    """(line, text) for each literal text an expression can evaluate to.

    String constants, f-strings and concatenations count as literals, and so
    do both operands of `or`, both branches of a conditional expression and
    the fallback of a lookup such as os.environ.get("NAME", "..."). Names,
    attributes, other calls and f-string fields obtain their value elsewhere.
    The literal part of a value composed at run time counts unless it is a
    short affix, as in shell code: f"smoke-{run_id}" passes.
    """
    if node is None:
        return []
    if isinstance(node, ast.BoolOp):
        return [found for value in node.values for found in _python_literals(value)]
    if isinstance(node, ast.IfExp):
        return _python_literals(node.body) + _python_literals(node.orelse)
    lookup = _lookup(node)
    if lookup is not None:
        return _python_literals(lookup[1])
    if not isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp)):
        return []
    text, composed = _string_parts(node)
    if composed and _COMPOSED_LITERAL_RE.fullmatch(text):
        return []
    return [(node.lineno, text)]


def _python_sinks(target: ast.AST, value: ast.AST) -> Iterable[Tuple[str, ast.AST]]:
    """(name, value) pairs an assignment to target stores."""
    if isinstance(target, ast.Name):
        yield target.id, value
    elif isinstance(target, ast.Attribute):
        yield target.attr, value
    elif isinstance(target, ast.Subscript):
        key = target.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            yield key.value, value
    elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
        if len(target.elts) == len(value.elts):
            for item, item_value in zip(target.elts, value.elts):
                yield from _python_sinks(item, item_value)


def _python_named_values(tree: ast.AST) -> Iterable[Tuple[str, ast.AST]]:
    """Every (name, value) pair the module binds to a name it states: targets
    of assignments, keyword arguments, dictionary entries, parameter defaults
    and the keys of lookups that have a fallback."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                yield from _python_sinks(target, node.value)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            yield from _python_sinks(node.target, node.value)
        elif isinstance(node, ast.keyword) and node.arg:
            yield node.arg, node.value
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    yield key.value, value
        elif isinstance(node, ast.arguments):
            positional = node.posonlyargs + node.args
            for arg, default in zip(positional[len(positional) - len(node.defaults):], node.defaults):
                yield arg.arg, default
            for arg, default in zip(node.kwonlyargs, node.kw_defaults):
                if default is not None:
                    yield arg.arg, default
        else:
            lookup = _lookup(node)
            if lookup is not None and lookup[0] and lookup[1] is not None:
                yield lookup[0], lookup[1]


def _python_findings(text: str) -> Set[int]:
    """Lines holding a literal value for a secret-named name in Python source."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return {
            number
            for number, name, value in _python_assignments_by_tokens(text)
            if _secret_literal(name, value)
        }
    lines: Set[int] = set()
    for name, value in _python_named_values(tree):
        # A Python string is a literal: nothing in it is expanded at run time.
        for number, literal in _python_literals(value):
            if _secret_literal(name, literal):
                lines.add(number)
    return lines


def _private_key_is_real(lines: List[str], index: int) -> bool:
    """A key header counts unless the block it opens holds only a placeholder."""
    for following in lines[index + 1 : index + 6]:
        stripped = following.strip()
        if not stripped or _PEM_HEADER_RE.match(stripped):
            continue
        if stripped.startswith("-----END "):
            return False
        return not is_placeholder(stripped, composed=False)
    rest = lines[index][_PRIVATE_KEY_RE.search(lines[index]).end():].strip()  # type: ignore[union-attr]
    return bool(rest) and not is_placeholder(rest, composed=False)


def _keyword_password_findings(line: str) -> bool:
    for run in _KEYWORD_RUN_RE.finditer(line):
        pairs = [item.split("=", 1) for item in run.group(0).split()]
        keys = {key for key, _value in pairs}
        if "password" in keys and keys & _LIBPQ_ADDRESS_KEYS:
            if any(key == "password" and not is_placeholder(value, composed=False) for key, value in pairs):
                return True
    return False


def content_findings(path: str, text: str) -> Set[Tuple[int, str]]:
    """(line, rule) pairs for one text file."""
    found: Set[Tuple[int, str]] = set()
    styles = _assignment_styles(path)
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        if _PRIVATE_KEY_RE.search(line) and _private_key_is_real(lines, number - 1):
            found.add((number, "private-key"))
        if _PUTTY_KEY_RE.match(line):
            found.add((number, "private-key"))
        for rule, pattern in _TOKEN_RULES:
            if any(not _placeholder_token(m.group(0)) for m in pattern.finditer(line)):
                found.add((number, rule))
        for pattern in (_URL_USERINFO_RE, _URL_QUERY_RE):
            if any(not is_placeholder(m.group("secret"), composed=False) for m in pattern.finditer(line)):
                found.add((number, "url-password"))
        if "password=" in line and _keyword_password_findings(line):
            found.add((number, "dsn-password"))
        for pattern in (_HOME_PATH_RE, _WINDOWS_HOME_RE):
            if any(m.group("user").lower() not in _PLACEHOLDER_USERS for m in pattern.finditer(line)):
                found.add((number, "home-path"))
        if _PRIVATE_ADDRESS_RE.search(line):
            found.add((number, "private-address"))
        if _DUMP_HEADER_RE.match(line):
            found.add((number, "database-dump"))
        if "env" in styles:
            m = _ENV_ASSIGNMENT_RE.match(line)
            if m and _secret_literal(m.group("key"), _line_value(m.group("value"), "#"), shell=True):
                found.add((number, "secret-assignment"))
        if "shell" in styles:
            for m in _SHELL_KEY_RE.finditer(line):
                word = _shell_word(line, m.end())
                if _secret_literal(m.group("key"), word, shell=True, composed=True):
                    found.add((number, "secret-assignment"))
        if "mapping" in styles and not line.lstrip().startswith(("#", ";", "//")):
            m = _MAPPING_ASSIGNMENT_RE.match(line)
            value = _line_value(m.group("value"), "#;") if m else ""
            # A YAML anchor, alias or tag is not an inline value.
            if m and not value.startswith(("&", "*", "!")) and _secret_literal(m.group("key"), value):
                found.add((number, "secret-assignment"))
        if "json" in styles:
            for m in _JSON_ASSIGNMENT_RE.finditer(line):
                if _secret_literal(m.group("key"), m.group("value")):
                    found.add((number, "secret-assignment"))
    if "python" in styles:
        found.update((number, "secret-assignment") for number in _python_findings(text))
    return found


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------
class CheckError(Exception):
    """The check could not be completed (exit status 2)."""


Finding = Tuple[str, int, str]  # (path, line or 0, rule); path is commit:path for a commit


def _link_leaves_repository(path: str, target: str) -> bool:
    inside = os.path.normpath(os.path.join(os.path.dirname(path), target))
    return os.path.isabs(target) or inside == ".." or inside.startswith(".." + os.sep)


def _data_findings(path: str, data: bytes) -> Set[Tuple[int, str]]:
    """(line, rule) pairs for the contents of a regular file."""
    found: Set[Tuple[int, str]] = set()
    if data.startswith(_BINARY_DUMP_MAGIC):
        found.add((0, "database-dump"))
    if b"\0" not in data[:8192]:
        found |= content_findings(path, data.decode("utf-8", errors="replace"))
    return found


def scan_file(root: Path, path: str) -> List[Finding]:
    """Findings for one candidate path, relative to root; [] if it is gone."""
    full = root / path
    found: Set[Tuple[int, str]] = {(0, rule) for rule in path_findings(path)}
    if full.is_symlink():
        if _link_leaves_repository(path, os.readlink(full)):
            found.add((0, "local-symlink"))
        # A link inside the repository is not followed; its target is a
        # candidate file of its own.
    elif full.is_file():
        try:
            data = full.read_bytes()
        except OSError as exc:
            raise CheckError(f"cannot read {path} ({type(exc).__name__})") from None
        found |= _data_findings(path, data)
    elif not full.exists():
        # Deleted from the working tree: nothing to read, nothing published.
        return []
    return [(path, line, rule) for line, rule in found]


def candidate_files(root: Path) -> List[str]:
    """The publishable tree: tracked paths plus untracked, nonignored ones."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
        )
    except (OSError, subprocess.CalledProcessError):
        raise CheckError(
            "cannot list the publishable files; run this in a Git checkout of the repository"
        ) from None
    return sorted({os.fsdecode(item) for item in result.stdout.split(b"\0") if item})


def scan(root: Path, paths: Iterable[str]) -> List[Finding]:
    findings: List[Finding] = []
    for path in paths:
        try:
            findings.extend(scan_file(root, path))
        except CheckError:
            raise
        except Exception as exc:  # fail closed, and never echo file contents
            raise CheckError(f"internal error while checking {path} ({type(exc).__name__})") from None
    return sorted(set(findings))


# --------------------------------------------------------------------------
# Commits
# --------------------------------------------------------------------------
# Findings in commits that are published already, and so cannot change any
# more, each reviewed and accepted on its own as (full commit id, path, line,
# rule). A later commit that brings the same text back is reported as usual.
_BASELINE_1A = "95a13daf3e37d5a60498e677f1736b793dc4e17e"  # the published task-1a root commit
ACCEPTED_IN_HISTORY: frozenset = frozenset(
    {
        # Synthetic stand-ins for the superuser and writer passwords in a test
        # of the role-specific password selection, not credentials; later
        # commits name them as example values.
        (_BASELINE_1A, "DB/tests/test_publication_policy.py", 152, "secret-assignment"),
        (_BASELINE_1A, "DB/tests/test_publication_policy.py", 153, "secret-assignment"),
    }
)

_SYMLINK_MODE = "120000"
_GITLINK_MODE = "160000"
_NO_OBJECT_RE = re.compile("0*")  # an empty or all-zero id: no object
_OBJECT_ID_RE = re.compile("[0-9a-f]{40}(?:[0-9a-f]{24})?")
_ABBREV = 12
_SHALLOW_HINT = (
    "this clone's history is shallow, so the commits cannot all be read; "
    "check them in a clone with the complete history (fetch-depth: 0 on GitHub, "
    "GIT_DEPTH: \"0\" on GitLab)"
)

Change = Tuple[str, str, str, str]  # (commit, path, mode, object id) of a file a commit adds or changes


def _git(root: Path, args: List[str], stdin: Optional[bytes] = None, *, check: bool = True) -> Optional[bytes]:
    """Standard output of a read-only git command in root; None when it fails
    and check is False. Objects missing from a partial clone are not fetched."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
    except OSError:
        raise CheckError("cannot run git; checking commits needs a Git checkout") from None
    if result.returncode == 0:
        return result.stdout
    if not check:
        return None
    detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
    raise CheckError(f"git {args[0]} failed" + (f" ({detail[0]})" if detail else ""))


def _commit_id(root: Path, revision: str) -> Optional[str]:
    """The full id of the commit that revision names in this clone, or None."""
    if not revision or revision.startswith("-"):
        return None
    out = _git(root, ["rev-parse", "--verify", "--quiet", revision + "^{commit}"], check=False)
    found = (out or b"").decode("ascii", "replace").strip()
    return found if _OBJECT_ID_RE.fullmatch(found) else None


def parse_changes(raw: bytes) -> List[Change]:
    """The files each commit adds or changes, from the output of
    `git diff-tree --stdin -r -c --root --no-renames -z`: a commit id, then one
    entry per file, `:<modes> <object ids> <status>` and the path -- with one
    colon, mode and id per parent, and for a merge only the files that differ
    from every parent. Deletions add nothing and are left out."""
    changes: List[Change] = []
    commit: Optional[str] = None
    tokens = raw.split(b"\0")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        if not token.startswith(b":"):
            commit = token.decode("ascii", "replace")
            if not _OBJECT_ID_RE.fullmatch(commit):
                raise CheckError("unexpected output from git diff-tree")
            continue
        if commit is None or index >= len(tokens) or not tokens[index]:
            raise CheckError("unexpected output from git diff-tree")
        path = os.fsdecode(tokens[index])
        index += 1
        meta = token.decode("ascii", "replace")
        parents = len(meta) - len(meta.lstrip(":"))
        fields = meta[parents:].split(" ")
        if len(fields) != 2 * parents + 3:
            raise CheckError("unexpected output from git diff-tree")
        mode, object_id = fields[parents], fields[2 * parents + 1]
        if _NO_OBJECT_RE.fullmatch(mode) or _NO_OBJECT_RE.fullmatch(object_id):
            continue
        changes.append((commit, path, mode, object_id))
    return changes


def parse_objects(raw: bytes, wanted: List[str]) -> Dict[str, bytes]:
    """The contents of the blobs asked for, from `git cat-file --batch` output:
    per object a `<id> blob <size>` line, the contents and a newline. A
    missing or unexpected object means the check cannot be completed."""
    objects: Dict[str, bytes] = {}
    position = 0
    for object_id in wanted:
        end = raw.find(b"\n", position)
        header = raw[position:end].decode("ascii", "replace").split(" ") if end >= 0 else []
        if len(header) != 3 or header[0] != object_id or header[1] != "blob" or not header[2].isdigit():
            state = " ".join(header[1:]) or "no answer"
            raise CheckError(
                f"cannot read object {object_id[:_ABBREV]} ({state}); checking commits "
                "needs every object they contain"
            )
        start = end + 1
        size = int(header[2])
        if raw[start + size : start + size + 1] != b"\n":
            raise CheckError(f"cannot read object {object_id[:_ABBREV]} (incomplete)")
        objects[object_id] = raw[start : start + size]
        position = start + size + 1
    return objects


def scan_commits(root: Path, base: str, head: str) -> Tuple[List[Finding], List[str], str]:
    """Findings in the files the commits in base..head add or change.

    Returns the findings, sorted by commit and path and written with the
    commit as `commit:path`, notes on how the range was read, and a summary.
    """
    if (_git(root, ["rev-parse", "--is-shallow-repository"]) or b"").strip() == b"true":
        raise CheckError(_SHALLOW_HINT)
    head_id = _commit_id(root, head)
    if head_id is None:
        raise CheckError(f"--head {head!r} is not a commit in this clone")
    notes: List[str] = []
    base_id: Optional[str] = None
    if not base or _NO_OBJECT_RE.fullmatch(base):
        notes.append(f"no base commit: checking every commit reachable from {head_id[:_ABBREV]}")
    else:
        base_id = _commit_id(root, base)
        if base_id is None:
            notes.append(
                f"base {base!r} is not a commit in this clone: checking every commit "
                f"reachable from {head_id[:_ABBREV]} instead"
            )
    listing = ["rev-list", "--reverse", "--topo-order", head_id] + (["^" + base_id] if base_id else [])
    commits = (_git(root, listing) or b"").decode("ascii", "replace").split()
    if not all(_OBJECT_ID_RE.fullmatch(commit) for commit in commits):
        raise CheckError("unexpected output from git rev-list")
    rank = {commit: index for index, commit in enumerate(commits)}

    # Each file version is read and checked once, however many commits add it.
    adding: Dict[Tuple[str, str, str], List[str]] = {}  # (path, mode, object) -> commits, in order
    if commits:
        raw = _git(
            root, ["diff-tree", "--stdin", "-r", "-c", "--root", "--no-renames", "-z"],
            stdin="".join(commit + "\n" for commit in commits).encode("ascii"),
        )
        for commit, path, mode, object_id in parse_changes(raw or b""):
            if commit not in rank:
                raise CheckError("unexpected output from git diff-tree")
            adding.setdefault((path, mode, object_id), []).append(commit)
    wanted = sorted({object_id for _path, mode, object_id in adding if mode != _GITLINK_MODE})
    objects: Dict[str, bytes] = {}
    if wanted:
        raw = _git(root, ["cat-file", "--batch"], stdin="".join(o + "\n" for o in wanted).encode("ascii"))
        objects = parse_objects(raw or b"", wanted)

    found: List[Tuple[int, str, int, str, str]] = []
    for (path, mode, object_id), added_by in adding.items():
        try:
            rules = {(0, rule) for rule in path_findings(path)}
            if mode == _SYMLINK_MODE:
                if _link_leaves_repository(path, os.fsdecode(objects[object_id])):
                    rules.add((0, "local-symlink"))
            elif mode != _GITLINK_MODE:
                rules |= _data_findings(path, objects[object_id])
        except Exception as exc:  # fail closed, and never echo file contents
            raise CheckError(
                f"internal error while checking {added_by[0][:_ABBREV]}:{path} ({type(exc).__name__})"
            ) from None
        for line, rule in rules:
            # Reported once, at the first commit adding it that is not accepted.
            commit = next((c for c in added_by if (c, path, line, rule) not in ACCEPTED_IN_HISTORY), None)
            if commit is not None:
                found.append((rank[commit], path, line, rule, commit))
    findings = [
        (f"{commit[:_ABBREV]}:{path}", line, rule)
        for _rank, path, line, rule, commit in sorted(set(found))
    ]
    count = f"{len(commits)} commit" + ("" if len(commits) == 1 else "s")
    if base_id:
        summary = f"{count} in {base_id[:_ABBREV]}..{head_id[:_ABBREV]}"
    else:
        summary = f"{count} reachable from {head_id[:_ABBREV]}"
    return findings, notes, summary


def _explicit_paths(root: Path, given: List[str]) -> List[str]:
    out = []
    for item in given:
        absolute = Path(item).resolve()
        try:
            out.append(absolute.relative_to(root.resolve()).as_posix())
        except ValueError:
            raise CheckError(f"{item} is outside {root}") from None
    return sorted(set(out))


def report(
    findings: List[Finding], checked: int, commits: Optional[Tuple[List[Finding], str]] = None,
    stream_ok=sys.stdout, stream_bad=sys.stderr,
) -> int:
    """Prints the result; commits are the findings in commits and a summary
    of the commits checked, when there were any to check."""
    in_commits = commits[0] if commits else []
    if not findings and not in_commits:
        checked_commits = f" and {commits[1]}" if commits else ""
        print(f"secrets and local-configuration check ok ({checked} files{checked_commits})", file=stream_ok)
        return EXIT_CLEAN
    every = findings + in_commits
    print(f"{len(every)} secrets or local-configuration finding(s):", file=stream_bad)
    for path, line, rule in every:
        location = f"{path}:{line}" if line else path
        print(f"  {location}: {rule}", file=stream_bad)
    print("What the rules mean (matched text is never shown):", file=stream_bad)
    for rule in sorted({rule for _path, _line, rule in every}):
        print(f"  {rule}: {RULES[rule]}", file=stream_bad)
    print(
        "Replace a literal value with a documented placeholder or a variable "
        "reference, or keep the file out of the repository; local settings "
        "belong in ignored files such as DB/.env.",
        file=stream_bad,
    )
    if in_commits:
        print(
            "A finding written as commit:path is in a commit of the checked range "
            "and stays in the history even after a later commit removes it: treat a "
            "real credential as disclosed and replace it, and rewrite the commits "
            "before they are published.",
            file=stream_bad,
        )
    return EXIT_FINDINGS


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check the publishable tree, and with --base the commits of a range, "
        "for committed secrets and local configuration."
    )
    parser.add_argument("paths", nargs="*", help="check only these files instead of the publishable tree")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root (default: this checkout)")
    parser.add_argument(
        "--base", metavar="REV",
        help="also check the commits in REV..HEAD, the files they add or change; an empty or "
        "all-zero REV, or one this clone does not contain, checks every commit reachable from HEAD",
    )
    parser.add_argument("--head", metavar="REV", help="the last commit of that range (default: HEAD)")
    parser.add_argument("--list-rules", action="store_true", help="print the rule ids and exit")
    args = parser.parse_args(argv)
    if args.list_rules:
        for rule, meaning in RULES.items():
            print(f"{rule}: {meaning}")
        return EXIT_CLEAN
    if args.head is not None and args.base is None:
        parser.error("--head requires --base")
    if args.base is not None and args.paths:
        parser.error("--base checks the publishable tree and its commits; it takes no paths")
    try:
        paths = _explicit_paths(args.root, args.paths) if args.paths else candidate_files(args.root)
        findings = scan(args.root, paths)
    except CheckError as exc:
        print(f"check_secrets_and_local_config.py: {exc}", file=sys.stderr)
        return EXIT_ERROR
    commits: Optional[Tuple[List[Finding], str]] = None
    if args.base is not None:
        try:
            in_commits, notes, summary = scan_commits(args.root, args.base, args.head or "HEAD")
        except CheckError as exc:
            # Whatever the tree holds is still reported, but an incomplete
            # check of the commits can never pass.
            if findings:
                report(findings, len(paths))
            print(f"check_secrets_and_local_config.py: commits not checked: {exc}", file=sys.stderr)
            return EXIT_ERROR
        for note in notes:
            print(note, flush=True)
        commits = (in_commits, summary)
    return report(findings, len(paths), commits)


if __name__ == "__main__":
    raise SystemExit(main())

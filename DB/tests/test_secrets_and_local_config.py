"""check_secrets_and_local_config.py: rules, placeholders and reporting, offline.

Every secret-shaped value below is assembled at run time from harmless pieces
and written to a temporary directory, so this file holds nothing the checker
itself, or any other scanner, would report; one test proves exactly that. The
candidate enumeration and the check of a commit range are exercised against a
stub `git` executable -- for commits, one that answers from a synthetic commit
graph in git's own output formats -- so no repository is created or changed.
Only the format test at the end reads this checkout's own HEAD, read-only.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "check_secrets_and_local_config.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_secrets_and_local_config", CHECKER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def j(*parts: str) -> str:
    return "".join(parts)


def lines(*items: str) -> str:
    return "\n".join(items) + "\n"


# Building blocks. Joined at run time, never present as one literal here.
MIXED = j("Q7wE9rT2", "yU4iO6pA", "8sD1fG3h", "J5kL0zX2", "cV4bN6mM", "1qW3eR5t")  # 48 chars
UPPER = j("Q7WE9RT2", "YU4IO6PA")  # 16 chars
SECRET = j("s3cr", "3t-v", "alue")
LOOPBACK = j("127", ".0.0.1")


def uri(password: str, user: str = "app", host: str = LOOPBACK) -> str:
    return j("postgresql", ":", "//", user, ":", password, "@", host, "/people_db")


def home(kind: str, user: str) -> str:
    return j("/", kind, "/", user, "/project/file.txt")


def address(*octets: str) -> str:
    return ".".join(octets)


def key_block(body: str, kind: str = "RSA ") -> str:
    begin = j("-----", "BEGIN ", kind, "PRIVATE ", "KEY", "-----")
    end = j("-----", "END ", kind, "PRIVATE ", "KEY", "-----")
    return f"{begin}\n{body}\n{end}\n"


def findings(tmp_path: Path, name: str, content, *, binary: bool = False) -> list:
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if binary:
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")
    return [(line, rule) for _path, line, rule in checker.scan(tmp_path, [name])]


# --------------------------------------------------------------------------
# Rules decided by the path
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name, rule",
    [
        (".env", "env-file"),
        ("DB/.env", "env-file"),
        (".env.local", "env-file"),
        ("deploy.env", "env-file"),
        (".envrc", "env-file"),
        ("id_ed25519", "key-file"),
        ("certs/server.key", "key-file"),
        ("store.p12", "key-file"),
        (".pgpass", "credential-file"),
        ("conf/.netrc", "credential-file"),
        (".ssh/config", "credential-file"),
        (".aws/credentials", "credential-file"),
        ("exports/people.dump", "database-dump"),
        ("people.sql.gz", "database-dump"),
        ("local.sqlite3", "database-dump"),
        ("state.bak", "database-dump"),
        (".venv/lib/site.py", "local-state"),
        ("DB/__pycache__/mod.cpython-311.pyc", "local-state"),
        ("ingest.log", "local-state"),
        ("DB/data/PG_VERSION", "local-state"),
        ("DB/backups/nightly.sql", "local-state"),
        ("DB/cron", "local-state"),
        (".DS_Store", "local-state"),
        ("Thumbs.db", "local-state"),
        ("notes.md~", "local-state"),
    ],
)
def test_path_rules(tmp_path: Path, name: str, rule: str) -> None:
    assert (0, rule) in findings(tmp_path, name, "harmless\n")


@pytest.mark.parametrize(
    "name",
    [".env.example", "config.env.example", ".env.sample", ".env.template", "DB/cron.example",
     "environment.yml", "notes.md", "DB/tests/fixtures/golden/demo_roster_minimal.csv"],
)
def test_ordinary_and_template_paths_are_not_reported(tmp_path: Path, name: str) -> None:
    assert findings(tmp_path, name, "harmless\n") == []


# --------------------------------------------------------------------------
# Content rules: true positives
# --------------------------------------------------------------------------
TOKENS = {
    "aws-access-key-id": j("AK", "IA", UPPER),
    "github-token": j("gh", "p_", MIXED[:36]),
    "gitlab-token": j("glp", "at-", MIXED[:20]),
    "slack-token": j("xo", "xb-", "1234567890-", MIXED[:12]),
    "slack-webhook": j("https://hooks.", "slack.com/services/", "T0A1/B0A1/", MIXED[:24]),
    "stripe-secret-key": j("sk", "_live_", MIXED[:24]),
    "google-api-key": j("AI", "za", MIXED[:35]),
    "npm-token": j("np", "m_", MIXED[:36]),
    "pypi-token": j("py", "pi-", "AgE", MIXED, MIXED[:10]),
    "sendgrid-key": j("S", "G.", MIXED[:22], ".", MIXED[:43]),
    "huggingface-token": j("h", "f_", MIXED[:34]),
    "sk-secret-key": j("s", "k-", MIXED[:40]),
    "jwt": j("ey", "J", MIXED[:12], ".", "ey", "J", MIXED[12:24], ".", MIXED[24:40]),
}


@pytest.mark.parametrize("rule", sorted(TOKENS))
def test_token_formats(tmp_path: Path, rule: str) -> None:
    content = f"# settings\nvalue = '{TOKENS[rule]}'\n"
    assert findings(tmp_path, "settings.py", content) == [(2, rule)]


def test_private_key_block(tmp_path: Path) -> None:
    content = "intro\n" + key_block(MIXED + MIXED)
    assert findings(tmp_path, "notes.txt", content) == [(2, "private-key")]


@pytest.mark.parametrize("kind", ["", "EC ", "OPENSSH ", "ENCRYPTED "])
def test_private_key_block_kinds(tmp_path: Path, kind: str) -> None:
    assert findings(tmp_path, "k.txt", key_block(MIXED, kind)) == [(1, "private-key")]


def test_putty_key_file(tmp_path: Path) -> None:
    content = j("PuTTY-User-", "Key-File-3: ssh-ed25519\n", "Encryption: none\n")
    assert findings(tmp_path, "key.txt", content) == [(1, "private-key")]


@pytest.mark.parametrize(
    "line",
    [
        uri(SECRET),
        uri(SECRET, user=""),
        j("https://example.org/api?user=app&pass", "word=", SECRET),
        j("redis", "://", "default", ":", SECRET, "@", "cache.example.org:6379/0"),
    ],
)
def test_passwords_in_urls(tmp_path: Path, line: str) -> None:
    assert findings(tmp_path, "notes.md", f"see {line} for details\n") == [(1, "url-password")]


def test_password_in_keyword_connection_string(tmp_path: Path) -> None:
    content = j('DSN = "host=', LOOPBACK, " dbname=people_db user=app pass", "word=", SECRET, '"\n')
    assert findings(tmp_path, "db.py", content) == [(1, "dsn-password")]


@pytest.mark.parametrize(
    "name, content, line",
    [
        (".env.example", j("PGPASS", "WORD=", SECRET, "\n"), 1),
        (".env.example", j("# PGPASS", "WORD=", SECRET, "\n"), 1),
        ("compose.yaml", lines("services:", "  db:", "    environment:", j("      POSTGRES_PASS", "WORD: ", SECRET)), 4),
        ("compose.yaml", lines("environment:", j("  - POSTGRES_PASS", "WORD=", SECRET)), 2),
        ("settings.json", j('{"api', '_key": "', "abc123", 'def456"}\n'), 1),
        ("settings.toml", j("[db]\nclient_sec", "ret = '", SECRET, "'\n"), 2),
        ("run.sh", j("#!/bin/sh\nexport PGPASS", "WORD=", SECRET, "\n"), 2),
        ("run.sh", j("docker run -e POSTGRES_PASS", "WORD=", SECRET, " image\n"), 1),
        ("run.sh", j("tool --pass", "word=", SECRET, "\n"), 1),
        ("README.md", j("```bash\nexport SERVICE_API_TO", "KEN=abc123def456\n```\n"), 2),
        ("Dockerfile", j("FROM base\nENV DB_PASS", "WORD=", SECRET, "\n"), 2),
    ],
)
def test_secret_assignments(tmp_path: Path, name: str, content: str, line: int) -> None:
    assert findings(tmp_path, name, content) == [(line, "secret-assignment")]


DOLLAR = "$"
# Literal values that merely contain "$" or start with a bracket.
LOOKALIKES = [
    j("S3cr", DOLLAR, "tP4ss!"),        # a "$" inside a literal is no reference
    j("pa", DOLLAR, DOLLAR, "word-literal"),
    j("Prefix", DOLLAR, "{PART}"),        # composed, but with a literal part that is no affix
    "[abc123]",
    "(xyz789)",
    "<Secr3t!>",
]


@pytest.mark.parametrize("value", LOOKALIKES)
def test_literals_that_only_look_like_references_or_templates(tmp_path: Path, value: str) -> None:
    assert findings(tmp_path, ".env.example", j("PGPASS", "WORD=", value, "\n")) == [(1, "secret-assignment")]
    assert findings(tmp_path, "config.yml", j("pass", "word: ", value, "\n")) == [(1, "secret-assignment")]


def test_single_quotes_keep_shell_text_literal(tmp_path: Path) -> None:
    quoted = j("'pa", DOLLAR, "word'")
    assert findings(tmp_path, "run.sh", j("export PGPASS", "WORD=", quoted, "\n")) == [(1, "secret-assignment")]
    assert findings(tmp_path, ".env.example", j("PGPASS", "WORD=", quoted, "\n")) == [(1, "secret-assignment")]
    # A variable's name spelled out in single quotes is still no secret.
    spelled = j("'", DOLLAR, "PGPASS'")
    assert findings(tmp_path, "run.sh", j("export PGPASS", "WORD=", spelled, "\n")) == []


def test_url_passwords_must_be_references_as_a_whole(tmp_path: Path) -> None:
    assert findings(tmp_path, "notes.md", f"see {uri(j('pa', DOLLAR, 'word'))}\n") == [(1, "url-password")]
    assert findings(tmp_path, "notes.md", f"see {uri(j(DOLLAR, '{PGPASS}'))}\n") == []


@pytest.mark.parametrize(
    "line",
    [
        j('-e POSTGRES_PASS', 'WORD="smoke-', DOLLAR, 'RUN_ID"'),
        j('PGPASS="it-', DOLLAR, "(od -An -N16 -tx1 /dev/urandom | tr -d ' \\n')", '"'),
        j('export PGPASS', 'WORD="', DOLLAR, '{PGPASSWORD:-', DOLLAR, '{POSTGRES_PASSWORD:-}}"'),
        j("export PGPASS", "WORD=", DOLLAR, "(cat /run/secrets/db)"),
        j('export PGPASS', 'WORD="smoke-"', DOLLAR, 'RUN_ID'),
        j("export PGPASS", "WORD=", DOLLAR, "{{ secrets.DB }}"),
    ],
    ids=["affix", "command-substitution", "nested-default", "unquoted-command", "partly-quoted", "ci-expression"],
)
def test_values_composed_at_run_time_are_references(tmp_path: Path, line: str) -> None:
    assert findings(tmp_path, "run.sh", line + "\n") == []


@pytest.mark.parametrize(
    "name, content",
    [
        ("settings.py", j("PASS", "WORD = ", repr(j("smoke-", DOLLAR, "RUN_ID")), "\n")),
        ("settings.py", j("PASS", "WORD = ", repr(j("pa", DOLLAR, "word")), "\n")),
        (".env.example", j("PGPASS", "WORD=smoke-", DOLLAR, "RUN_ID\n")),
        ("config.yml", j("pass", "word: smoke-", DOLLAR, "{RUN_ID}\n")),
        ("config.json", j('{"pass', 'word": "smoke-', DOLLAR, 'RUN_ID"}\n')),
    ],
    ids=["python-affix", "python-embedded", "env-file", "yaml", "json"],
)
def test_composition_is_reported_where_nothing_expands_it(tmp_path: Path, name: str, content: str) -> None:
    # Only shell code expands "$NAME" at run time; a Python string, an env file
    # read by people_pubs, YAML and JSON keep it as literal text.
    assert findings(tmp_path, name, content) == [(1, "secret-assignment")]


@pytest.mark.parametrize(
    "value",
    [j(DOLLAR, "PASSWORD"), j(DOLLAR, "{PASSWORD}"), "<password>", "{name}", "[redacted]", "change-me", ""],
)
def test_python_whole_value_placeholders_are_accepted(tmp_path: Path, value: str) -> None:
    assert findings(tmp_path, "settings.py", j("PASS", "WORD = ", repr(value), "\n")) == []


def test_python_constants_with_literal_secrets(tmp_path: Path) -> None:
    key = j("abc123", "def456", "ghi789")
    content = lines(
        "import os",
        j("API_", "KEY = ") + repr(key),
        "class Settings:",
        "    " + j("SECRET_", "KEY: str = ") + repr(SECRET),
        "    def __init__(self):",
        "        self." + j("pass", "word = ") + repr(SECRET),
        j("SERVICE_TO", "KEN = b") + repr(key),
    )
    assert findings(tmp_path, "settings.py", content) == [
        (2, "secret-assignment"), (4, "secret-assignment"), (6, "secret-assignment"), (7, "secret-assignment"),
    ]


KEY = j("abc123", "def456", "ghi789")  # credential-shaped, for token and API-key names


def python_source(*statements: str) -> str:
    """Python source with @KEY@ and @SECRET@ replaced by quoted values built here."""
    text = lines(*statements)
    return text.replace("@KEY@", repr(KEY)).replace("@SECRET@", repr(SECRET))


@pytest.mark.parametrize(
    "statements, line",
    [
        (["client = Client(api_key=@KEY@)"], 1),
        (["db = connect(host='db.example.org', password=@SECRET@)"], 1),
        (["client = Client(", "    to" "ken=@KEY@,", ")"], 2),
        (["CONFIG = {'api_key': @KEY@}"], 1),
        (["headers = {", "    'X-Api-Key': @KEY@,", "}"], 2),
        (["settings = dict(pass" "word=@SECRET@)"], 1),
        (["API_KEY = f@KEY@"], 1),
        (["pass" "word = f@SECRET@"], 1),
        (["if True: API_KEY = @KEY@"], 1),
        (["class Settings: pass" "word = @SECRET@"], 1),
        (["def configure(): to" "ken = @KEY@"], 1),
        (["for _ in range(1): secret = @SECRET@"], 1),
        (["ready = True; API_KEY = @KEY@"], 1),
        (["API_KEY = os.environ.get('API_KEY', @KEY@)"], 1),
        (["client = Client(os.getenv('SERVICE_TO" "KEN', @KEY@))"], 1),
        (["value = settings.get('pass" "word', @SECRET@)"], 1),
        (["os.environ.setdefault('PGPASS" "WORD', @SECRET@)"], 1),
        (["pass" "word = os.getenv('DB_LOGIN', default=@SECRET@)"], 1),
        (["pass" "word = args.password or @SECRET@"], 1),
        (["to" "ken = @KEY@ if offline else args.token"], 1),
        (["def connect(host, pass" "word=@SECRET@): ..."], 1),
        (["def connect(*, api_key=@KEY@): ..."], 1),
        (["handler = lambda to" "ken=@KEY@: to" "ken"], 1),
        (["os.environ['PGPASS" "WORD'] = @SECRET@"], 1),
        (["config['api_key'] = @KEY@"], 1),
        (["user, pass" "word = 'app', @SECRET@"], 1),
        (["(to" "ken := @KEY@)"], 1),
        (["pass" "word = 'Sup3r' + 'Secret'"], 1),
        (["pass" "word = f'Sup3rSecret{suffix}'"], 1),
        (["PASS" "WORD = '!Secr3t'"], 1),
    ],
    ids=[
        "keyword-argument", "keyword-argument-strict", "keyword-argument-multiline",
        "dict-entry", "dict-entry-multiline", "dict-call", "f-string-literal",
        "f-string-literal-strict", "one-line-if", "one-line-class", "one-line-def",
        "one-line-for", "semicolon", "environ-get-fallback", "getenv-fallback-argument",
        "mapping-get-fallback", "setdefault-fallback", "getenv-default-keyword",
        "or-fallback", "conditional", "parameter-default", "keyword-only-default",
        "lambda-default", "environ-item", "subscript-target", "tuple-unpacking", "walrus",
        "concatenation", "f-string-literal-part", "leading-exclamation-mark",
    ],
)
def test_python_literal_credentials_are_reported(tmp_path: Path, statements: list, line: int) -> None:
    content = python_source("import os", *statements)
    assert findings(tmp_path, "client.py", content) == [(line + 1, "secret-assignment")]


@pytest.mark.parametrize(
    "statements",
    [
        ["client = Client(to" "ken=args.token, api_key=api_key)"],
        ["db = connect(host=h, pass" "word=args.password)"],
        ["CONFIG = {'api_key': settings.api_key, 'pass" "word': None}"],
        ["CONFIG = {'to" "ken': 'crossref', 'pass" "word': ''}"],
        ["API_KEY = os.environ.get('API_KEY')", "API_KEY = os.environ.get('API_KEY', '')"],
        ["API_KEY = os.environ['API_KEY']", "PGPASS" "WORD = os.getenv('PGPASS" "WORD', 'change-me')"],
        ["pass" "word = args.password or os.environ['PGPASS" "WORD']"],
        ["API_KEY = f'{prefix}{suffix}'", "pass" "word = f'smoke-{run_id}'"],
        ["db = connect(pass" "word='<password>')", "db = connect(pass" "word='change-me')"],
        ["def connect(host, pass" "word=None): ...", "def search(source_to" "ken='crossref'): ..."],
        ["# API_KEY = @KEY@"],
        ['"""API_KEY = @KEY@"""'],
        ["source_to" "ken = 'crossref'", "API_KEY = ''", "PASS" "WORD = '<your password>'"],
        ["value = lookup('pass" "word', @SECRET@)"],
    ],
    ids=[
        "keyword-expressions", "keyword-expression-strict", "dict-expressions",
        "dict-identifier-and-empty", "environment-lookups", "lookups-with-placeholders",
        "or-without-literal", "composed-f-strings", "keyword-placeholders",
        "harmless-defaults", "comment", "docstring", "identifiers-and-placeholders",
        "positional-argument-of-other-call",
    ],
)
def test_python_values_obtained_elsewhere_are_not_reported(tmp_path: Path, statements: list) -> None:
    assert findings(tmp_path, "client.py", python_source("import os", *statements)) == []


def test_python_that_does_not_parse_still_has_its_constants_checked(tmp_path: Path) -> None:
    content = python_source("API_KEY = @KEY@", "def broken(:")
    assert findings(tmp_path, "broken.py", content) == [(1, "secret-assignment")]


@pytest.mark.parametrize(
    "name, content, rule",
    [
        ("run.sh", j('export API_KEY="', DOLLAR, '{API_KEY:-', KEY, '}"\n'), "secret-assignment"),
        ("run.sh", j('export PGPASS', 'WORD="', DOLLAR, '{PGPASS', 'WORD:-literal-credential}"\n'), "secret-assignment"),
        ("run.sh", j("PGPASS", "WORD=", DOLLAR, "{PGPASS", "WORD:=", SECRET, "}\n"), "secret-assignment"),
        ("run.sh", j("PGPASS", "WORD=", DOLLAR, "{PGPASS", "WORD-", SECRET, "}\n"), "secret-assignment"),
        ("run.sh", j("PGPASS", "WORD=", DOLLAR, "{CI:+", SECRET, "}\n"), "secret-assignment"),
        ("run.sh", j('PGPASS', 'WORD="', DOLLAR, '{PGPASS', 'WORD:-', DOLLAR, '{POSTGRES_PASS', 'WORD:-',
                     SECRET, '}}"\n'), "secret-assignment"),
        (".env.example", j("PGPASS", "WORD=", DOLLAR, "{PGPASS", "WORD:-", SECRET, "}\n"), "secret-assignment"),
        ("compose.yaml", j("    POSTGRES_PASS", "WORD: ", DOLLAR, "{POSTGRES_PASS", "WORD:-", SECRET, "}\n"),
         "secret-assignment"),
        ("notes.md", j("see ", uri(j(DOLLAR, "{PGPASS:-", SECRET, "}")), "\n"), "url-password"),
    ],
    ids=["api-key-default", "password-default", "assign-default", "unset-default", "alternate-value",
         "nested-default", "env-file", "compose", "url-password"],
)
def test_literal_fallbacks_inside_references_are_reported(tmp_path: Path, name: str, content: str, rule: str) -> None:
    assert findings(tmp_path, name, content) == [(1, rule)]


@pytest.mark.parametrize(
    "value",
    [
        j(DOLLAR, "{PGPASS", "WORD:-}"),
        j(DOLLAR, "{PGPASS", "WORD:-change-me}"),
        j(DOLLAR, "{PGPASS", "WORD:-<password>}"),
        j(DOLLAR, "{PGPASS", "WORD:-", DOLLAR, "(cat /run/secrets/db)}"),
        j(DOLLAR, "{PGPASS", "WORD:-", DOLLAR, "{POSTGRES_PASS", "WORD}}"),
        j(DOLLAR, "{PGPASS", "WORD:?set PGPASS", "WORD first}"),
        j(DOLLAR, "{PGPASS", "WORD#prefix}"),
    ],
    ids=["empty", "placeholder", "template", "command", "reference", "error-message", "pattern-removal"],
)
def test_references_with_harmless_fallbacks_are_accepted(tmp_path: Path, value: str) -> None:
    assert findings(tmp_path, "run.sh", j('export PGPASS', 'WORD="', value, '"\n')) == []
    assert findings(tmp_path, ".env.example", j("PGPASS", "WORD=", value, "\n")) == []


def test_identifier_fallbacks_under_token_names_are_accepted(tmp_path: Path) -> None:
    # Token-like names often hold identifiers, so a fallback is judged like a
    # direct value: only a credential-shaped one counts.
    assert findings(tmp_path, "run.sh", j("SOURCE_TO", "KEN=", DOLLAR, "{SOURCE_TO", "KEN:-crossref}\n")) == []


def test_yaml_anchors_are_not_values_but_quoted_text_is(tmp_path: Path) -> None:
    assert findings(tmp_path, "config.yml", j("pass", "word: *default_credentials\n")) == []
    assert findings(tmp_path, "config.yml", j("pass", 'word: "*', SECRET, '"\n')) == [(1, "secret-assignment")]


@pytest.mark.parametrize(
    "path_text",
    [
        home("home", "alice"),
        home("Users", "alice"),
        j("file://", home("home", "alice")),
        j("C", ":", "\\", "Users", "\\", "alice", "\\", "project"),
        j("D", ":", "/", "Users", "/", "alice"),
    ],
)
def test_home_paths(tmp_path: Path, path_text: str) -> None:
    assert findings(tmp_path, "notes.md", f"open {path_text} first\n") == [(1, "home-path")]


@pytest.mark.parametrize(
    "octets",
    [("10", "1", "2", "3"), ("172", "16", "0", "5"), ("172", "31", "255", "1"), ("192", "168", "1", "20")],
)
def test_private_addresses(tmp_path: Path, octets) -> None:
    content = f"bind = {address(*octets)}\nurl = http://{address(*octets)}:8080/\n"
    assert findings(tmp_path, "net.cfg", content) == [(1, "private-address"), (2, "private-address")]


def test_dump_contents(tmp_path: Path) -> None:
    header = j("-- ", "PostgreSQL ", "database dump\n")
    assert findings(tmp_path, "schema.sql", header + "SET x = 1;\n") == [(1, "database-dump")]
    custom = j("PG", "DMP").encode() + b"\x01\x0e\x00" * 40
    assert findings(tmp_path, "blob.bin", custom, binary=True) == [(0, "database-dump")]
    sqlite = j("SQLite ", "format 3").encode() + b"\x00" + b"\x10\x00" * 40
    assert findings(tmp_path, "blob2.bin", sqlite, binary=True) == [(0, "database-dump")]


def test_symlinks(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("readme\n", encoding="utf-8")
    os.symlink(os.path.abspath(os.sep), tmp_path / "absolute-link")
    os.symlink(os.path.join("..", "..", "elsewhere"), tmp_path / "docs" / "escaping-link")
    os.symlink(os.path.join("..", "README.md"), tmp_path / "docs" / "inside-link")
    found = checker.scan(tmp_path, ["absolute-link", "docs/escaping-link", "docs/inside-link"])
    assert found == [("absolute-link", 0, "local-symlink"), ("docs/escaping-link", 0, "local-symlink")]


# --------------------------------------------------------------------------
# Placeholders and false-positive boundaries
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    ["", "change-me-postgres", "changeme", "replace-me", "your-password-here", "example",
     "example-secret", "placeholder", "dummy", "redacted", "<password>",
     "<your password here>", "[redacted]", "${PGPASS}", "$PGPASS", "${PGPASS:-}",
     "$(cat secret-file)", "{password}", "{{ password }}", "%(password)s",
     "***", "...", "xxxxxxxx", "password", "secret"],
)
def test_documented_placeholders_are_not_reported(tmp_path: Path, value: str) -> None:
    content = j("PGPASS", "WORD=", value, "\nAPP_SEC", "RET=", value, "\n")
    assert findings(tmp_path, ".env.example", content) == []
    assert findings(tmp_path, "notes.md", f"use {uri(value)} here\n") == []


@pytest.mark.parametrize(
    "name, content",
    [
        # references and runtime-composed values in scripts
        ("run.sh", j('export PGPASS', 'WORD="${POSTGRES_PASS', 'WORD:-}"\n')),
        ("run.sh", j('-e POSTGRES_PASS', 'WORD="smoke-$RUN_ID"\n')),
        ("run.sh", j('POSTGRES_PASS', 'WORD="$PGPASS" docker run -e POSTGRES_PASS', 'WORD image\n')),
        # names that merely contain a secret word
        ("compose.yaml", j('command: ["-c", "pass', 'word_encryption=scram-sha-256"]\n')),
        ("compose.yaml", j("pass", "word_file: /run/config/db\n")),
        ("jobs.yaml", "source_token: crossref\n"),
        ("jobs.json", '{"args": ["--source-token", "crossref"], "scope_key": "crossref_backfill_global"}\n'),
        ("ci.yml", j("  to", "ken: ${{ secrets.DEPLOY_TOKEN }}\n")),
        ("ci.yml", j("  pass", "word: |\n    ${{ secrets.X }}\n")),
        # program source: keyword arguments are not stored values
        ("ingest.py", j("client = Client(to", "ken=args.token, api_key=api_key)\n")),
        ("db.py", j("connect(host=h, pass", "word=args.password)\n")),
        ("db.py", j('dsn = f"postgresql://{user}:{pass', 'word}@{host}/{name}"\n')),
        # prose
        ("notes.md", "Put the password and the API token into DB/.env.\n"),
        # URLs without a password
        ("notes.md", j("clone ", "ssh", "://", "git", "@", "code.example.org/team/repo\n")),
        ("notes.md", j("see https://example.org:8443/path", "@", "anchor\n")),
        ("notes.md", j("connect to postgresql", "://", "app", "@", LOOPBACK, "/people_db\n")),
        # placeholder home paths, relative paths and URLs
        ("notes.md", j("/", "home", "/", "<user>", "/project\n")),
        ("notes.md", j("/", "home", "/", "user", "/project and ", "/", "Users", "/", "you", "/x\n")),
        ("notes.md", j("$HOME/project and ~/project and src/", "home/alice\n")),
        ("notes.md", j("https://example.org/", "home", "/alice\n")),
        # numbers that are not private addresses
        ("notes.md", j("doi 10.5555/fixture-001, version v", address("10", "1", "2", "3"), "\n")),
        ("notes.md", j(LOOPBACK, " ", address("172", "32", "0", "1"), " ", address("192", "169", "1", "1"), "\n")),
        # token placeholders
        ("notes.md", j("AK", "IA", "IOSFODNN7", "EXAMPLE\n")),
        ("notes.md", j("gh", "p_", "x" * 36, "\n")),
        # a private-key header around a placeholder body
        ("notes.md", key_block("...")),
        ("notes.md", key_block("<your key here>", "OPENSSH ")),
    ],
)
def test_false_positive_boundaries(tmp_path: Path, name: str, content: str) -> None:
    assert findings(tmp_path, name, content) == []


def test_this_test_corpus_and_the_checker_hold_no_findings() -> None:
    names = ["check_secrets_and_local_config.py", "DB/tests/test_secrets_and_local_config.py"]
    assert checker.scan(ROOT, names) == []


# --------------------------------------------------------------------------
# The command line: output, exit status, candidate enumeration
# --------------------------------------------------------------------------
def _run(args: list, *, cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECKER), *args],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
    )


def test_report_names_rule_file_and_line_but_never_the_value(tmp_path: Path) -> None:
    (tmp_path / "b.md").write_text(f"first\nsee {uri(SECRET)}\n", encoding="utf-8")
    (tmp_path / "a.env").write_text(j("API_TO", "KEN=abc123def456\n"), encoding="utf-8")
    (tmp_path / "c.py").write_text(python_source("client = Client(", "    api_key=@KEY@)"), encoding="utf-8")
    (tmp_path / "d.sh").write_text(j('PGPASS', 'WORD="', DOLLAR, '{PGPASS', 'WORD:-', SECRET, '}"\n'), encoding="utf-8")
    files = ["b.md", "a.env", "c.py", "d.sh"]
    first = _run(["--root", str(tmp_path), *files], cwd=tmp_path)
    second = _run(["--root", str(tmp_path), *reversed(files)], cwd=tmp_path)
    assert first.returncode == 1
    assert first.stderr == second.stderr, "output is deterministic, whatever the argument order"
    reported = [line.strip() for line in first.stderr.splitlines()]
    assert reported[1:6] == [
        "a.env: env-file", "a.env:1: secret-assignment", "b.md:2: url-password",
        "c.py:2: secret-assignment", "d.sh:1: secret-assignment",
    ]
    assert "url-password: " in first.stderr, "each reported rule is explained"
    for value in (SECRET, "abc123def456", KEY):
        assert value not in first.stdout + first.stderr


def test_clean_files_exit_zero(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("PubDex\n", encoding="utf-8")
    proc = _run(["--root", str(tmp_path), "README.md"], cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "check ok (1 files)" in proc.stdout


def test_list_rules_names_every_rule(tmp_path: Path) -> None:
    proc = _run(["--list-rules"], cwd=tmp_path)
    assert proc.returncode == 0
    listed = {line.split(":", 1)[0] for line in proc.stdout.splitlines()}
    assert listed == set(checker.RULES)


def test_path_outside_the_root_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()
    (tmp_path / "outside.txt").write_text("x\n", encoding="utf-8")
    proc = _run(["--root", str(tmp_path / "repo"), str(tmp_path / "outside.txt")], cwd=tmp_path)
    assert proc.returncode == 2


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
def test_unreadable_file_is_an_error_not_a_pass(tmp_path: Path) -> None:
    target = tmp_path / "locked.txt"
    target.write_text("x\n", encoding="utf-8")
    target.chmod(0)
    try:
        proc = _run(["--root", str(tmp_path), "locked.txt"], cwd=tmp_path)
    finally:
        target.chmod(0o600)
    assert proc.returncode == 2
    assert "cannot read locked.txt" in proc.stderr


STUB_GIT = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["STUB_GIT_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
if os.environ.get("STUB_GIT_FAIL"):
    sys.exit(128)
sys.stdout.write("".join(p + "\0" for p in os.environ["STUB_GIT_FILES"].split(",")))
'''


def _stub_git(tmp_path: Path, files: list, fail: bool = False) -> dict:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "git"
    stub.write_text(STUB_GIT, encoding="utf-8")
    stub.chmod(0o755)
    env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
               STUB_GIT_LOG=str(tmp_path / "git.log"), STUB_GIT_FILES=",".join(files))
    if fail:
        env["STUB_GIT_FAIL"] = "1"
    return env


def test_default_candidates_come_from_the_publishable_file_list(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "DB").mkdir(parents=True)
    (repo / "README.md").write_text("PubDex\n", encoding="utf-8")
    (repo / "config.yml").write_text(j("pass", "word: ", SECRET, "\n"), encoding="utf-8")
    # Present on disk but not listed, as an ignored local file would be.
    (repo / "DB" / ".env").write_text(j("PGPASS", "WORD=", SECRET, "\n"), encoding="utf-8")
    env = _stub_git(tmp_path, ["README.md", "config.yml", "deleted-in-working-tree.md"])
    proc = _run(["--root", str(repo)], cwd=tmp_path, env=env)
    calls = (tmp_path / "git.log").read_text(encoding="utf-8").splitlines()
    assert calls == ['["-C", "%s", "ls-files", "-z", "--cached", "--others", "--exclude-standard"]' % repo]
    assert proc.returncode == 1
    reported = [line.strip() for line in proc.stderr.splitlines() if line.startswith("  ")]
    assert "config.yml:1: secret-assignment" in reported
    assert not any(line.startswith("DB/.env") for line in reported), "ignored files are never opened"
    assert not any("deleted-in-working-tree" in line for line in reported)


def test_listing_failure_is_an_error(tmp_path: Path) -> None:
    env = _stub_git(tmp_path, [], fail=True)
    proc = _run(["--root", str(tmp_path)], cwd=tmp_path, env=env)
    assert proc.returncode == 2
    assert "Git checkout" in proc.stderr


# --------------------------------------------------------------------------
# Commits: --base/--head
# --------------------------------------------------------------------------
ZERO_ID = "0" * 40

# Answers the read-only commands of the commit check from a synthetic history,
# in git's formats, and records each call with what it read on stdin. Any
# other invocation is refused, so a changed command line fails these tests.
STUB_HISTORY_GIT = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args[:1] == ["-C"]:
    args = args[2:]
given = sys.stdin.read() if args[:1] in (["diff-tree"], ["cat-file"]) else ""
with open(os.environ["STUB_GIT_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps([args, given]) + "\n")
with open(os.environ["STUB_GIT_HISTORY"], encoding="utf-8") as fh:
    history = json.load(fh)
out = sys.stdout.buffer
if args and args[0] in history["fail"]:
    sys.stderr.write(history["fail"][args[0]] + "\n")
    sys.exit(128)
if args == ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]:
    out.write(b"".join(path.encode() + b"\0" for path in history["files"]))
elif args == ["rev-parse", "--is-shallow-repository"]:
    out.write(b"true\n" if history["shallow"] else b"false\n")
elif args[:3] == ["rev-parse", "--verify", "--quiet"] and len(args) == 4 and args[3].endswith("^{commit}"):
    name = args[3][: -len("^{commit}")]
    commit = history["refs"].get(name, name if name in history["commits"] else None)
    if commit is None:
        sys.exit(1)
    out.write(commit.encode() + b"\n")
elif args[:3] == ["rev-list", "--reverse", "--topo-order"] and len(args) in (4, 5):
    graph, excluded = history["commits"], set()
    stack = [args[4][1:]] if len(args) == 5 else []
    while stack:
        commit = stack.pop()
        if commit not in excluded:
            excluded.add(commit)
            stack.extend(graph[commit])
    order, seen = [], set()
    def visit(commit):
        if commit in seen or commit in excluded:
            return
        seen.add(commit)
        for parent in graph[commit]:
            visit(parent)
        order.append(commit)
    visit(args[3])
    out.write("".join(commit + "\n" for commit in order).encode())
elif args == ["diff-tree", "--stdin", "-r", "-c", "--root", "--no-renames", "-z"]:
    for commit in given.split():
        out.write(bytes.fromhex(history["diffs"][commit]))
elif args == ["cat-file", "--batch"]:
    for name in given.split():
        data = history["blobs"].get(name)
        if data is None:
            out.write(name.encode() + b" missing\n")
        else:
            data = bytes.fromhex(data)
            out.write(b"%s blob %d\n" % (name.encode(), len(data)) + data + b"\n")
else:
    sys.stderr.write("stub git: unexpected " + json.dumps(args) + "\n")
    sys.exit(2)
'''


class History:
    """A synthetic commit graph, and what git prints about it for the checker."""

    def __init__(self) -> None:
        self.parents: dict = {}
        self.trees: dict = {}
        self.blobs: dict = {}
        self.refs: dict = {}

    def commit(self, name: str, files: dict, *parents: str) -> str:
        """A commit holding its first parent's files with these changes: new
        contents, a (mode, contents) pair, or None for a deletion."""
        tree = dict(self.trees[parents[0]]) if parents else {}
        for path, content in files.items():
            if content is None:
                tree.pop(path, None)
                continue
            mode, data = content if isinstance(content, tuple) else ("100644", content)
            data = data.encode() if isinstance(data, str) else data
            blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
            self.blobs[blob] = data
            tree[path] = (mode, blob)
        commit = hashlib.sha1(f"commit {name}".encode()).hexdigest()
        self.parents[commit], self.trees[commit], self.refs[name] = list(parents), tree, commit
        return commit

    def diff(self, commit: str) -> bytes:
        """`git diff-tree -r -c --root --no-renames -z` for one commit: the
        commit id, then per file one colon, mode and id per parent and the
        result's mode and id; a merge lists only files differing from all
        of its parents."""
        tree = self.trees[commit]
        parents = [self.trees[parent] for parent in self.parents[commit]] or [{}]
        absent = ("000000", ZERO_ID)
        entries = b""
        for path in sorted(set(tree).union(*parents)):
            result = tree.get(path)
            if any(parent.get(path) == result for parent in parents):
                continue
            sides = [parent.get(path, absent) for parent in parents] + [result or absent]
            status = "".join("A" if path not in p else "D" if result is None else "M" for p in parents)
            meta = ":" * len(parents) + " ".join([m for m, _ in sides] + [o for _, o in sides] + [status])
            entries += meta.encode() + b"\0" + path.encode() + b"\0"
        return commit.encode() + b"\0" + entries if entries else b""

    def describe(self, path: Path, *, files, shallow=False, missing=(), fail=None, garble=None) -> None:
        diffs = {commit: self.diff(commit).hex() for commit in self.parents}
        if garble:
            diffs[garble] = (garble.encode() + b"\0:100644 broken\0x\0").hex()
        path.write_text(json.dumps({
            "commits": self.parents, "refs": self.refs, "diffs": diffs,
            "blobs": {blob: data.hex() for blob, data in self.blobs.items() if blob not in missing},
            "files": sorted(files), "shallow": shallow, "fail": fail or {},
        }), encoding="utf-8")


def _history_setup(tmp_path: Path, history: History, head: str, **describe) -> tuple:
    """A checkout of head's regular files, and the environment that puts the
    stub git first on PATH."""
    repo = tmp_path / "repo"
    files = {path: blob for path, (mode, blob) in history.trees[head].items() if mode == "100644"}
    for path, blob in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_bytes(history.blobs[blob])
    repo.mkdir(exist_ok=True)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "git").write_text(STUB_HISTORY_GIT, encoding="utf-8")
    (bindir / "git").chmod(0o755)
    history.describe(tmp_path / "history.json", files=files, **describe)
    env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
               STUB_GIT_LOG=str(tmp_path / "git.log"), STUB_GIT_HISTORY=str(tmp_path / "history.json"))
    return repo, env


def _history_run(tmp_path: Path, history: History, *args: str, head: str, **describe) -> subprocess.CompletedProcess:
    repo, env = _history_setup(tmp_path, history, head, **describe)
    return _run(["--root", str(repo), *args], cwd=tmp_path, env=env)


def _reported(proc: subprocess.CompletedProcess) -> list:
    lines = proc.stderr.splitlines()
    end = lines.index("What the rules mean (matched text is never shown):")
    return [line.strip() for line in lines[1:end]]


def _git_calls(tmp_path: Path) -> list:
    return [json.loads(line) for line in (tmp_path / "git.log").read_text(encoding="utf-8").splitlines()]


def test_a_credential_committed_and_removed_again_is_found_in_its_commit(tmp_path: Path) -> None:
    h = History()
    base = h.commit("base", {"README.md": "PubDex\n"})
    added = h.commit("added", {"config.yml": j("pass", "word: ", SECRET, "\n"), "notes.md": "notes\n"}, base)
    removed = h.commit("removed", {"config.yml": None}, added)
    tree_only = _history_run(tmp_path, h, head=removed)
    assert tree_only.returncode == 0, "the tree alone no longer holds it"
    proc = _history_run(tmp_path, h, "--base", base, "--head", removed, head=removed)
    assert proc.returncode == 1
    assert _reported(proc) == [f"{added[:12]}:config.yml:1: secret-assignment"]
    assert "stays in the history even after a later commit removes it" in proc.stderr
    assert SECRET not in proc.stdout + proc.stderr


def test_a_credential_replaced_by_a_placeholder_is_still_found_in_its_commit(tmp_path: Path) -> None:
    h = History()
    base = h.commit("base", {".env.example": "PGPASSWORD=change-me\n"})
    leaked = h.commit("leaked", {".env.example": j("PGPASS", "WORD=", SECRET, "\n")}, base)
    fixed = h.commit("fixed", {".env.example": "PGPASSWORD=change-me\n"}, leaked)
    proc = _history_run(tmp_path, h, "--base", base, "--head", fixed, head=fixed)
    assert proc.returncode == 1
    assert _reported(proc) == [f"{leaked[:12]}:.env.example:1: secret-assignment"]


def test_clean_commits_pass_and_are_counted(tmp_path: Path) -> None:
    h = History()
    base = h.commit("base", {"README.md": "PubDex\n"})
    one = h.commit("one", {"notes.md": "first\n"}, base)
    two = h.commit("two", {"notes.md": "second\n", "README.md": "PubDex, updated\n"}, one)
    proc = _history_run(tmp_path, h, "--base", base, "--head", two, head=two)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == (
        f"secrets and local-configuration check ok (2 files and 2 commits in {base[:12]}..{two[:12]})"
    )


def test_commits_the_base_holds_already_are_not_read_again(tmp_path: Path) -> None:
    h = History()
    leaked = h.commit("leaked", {"config.yml": j("pass", "word: ", SECRET, "\n")})
    base = h.commit("base", {"config.yml": None, "README.md": "PubDex\n"}, leaked)
    head = h.commit("head", {"notes.md": "notes\n"}, base)
    proc = _history_run(tmp_path, h, "--base", base, "--head", head, head=head)
    assert proc.returncode == 0, proc.stderr
    assert "1 commit in" in proc.stdout
    calls = _git_calls(tmp_path)
    assert [args for args, _stdin in calls if args[0] == "rev-list"] == [
        ["rev-list", "--reverse", "--topo-order", head, "^" + base]
    ]
    read = "".join(stdin for args, stdin in calls if args[0] in ("diff-tree", "cat-file"))
    assert leaked not in read and h.trees[leaked]["config.yml"][1] not in read


def test_paths_and_links_a_commit_adds_are_checked_too(tmp_path: Path) -> None:
    h = History()
    base = h.commit("base", {".env.example": "PGPASSWORD=change-me\n"})
    added = h.commit("added", {"DB/.env": "PGHOST=localhost\n", "docs/link": ("120000", "../../outside")}, base)
    # The template's contents under a name that is no template any more.
    renamed = h.commit("renamed", {".env": "PGPASSWORD=change-me\n", "DB/.env": None, "docs/link": None}, added)
    proc = _history_run(tmp_path, h, "--base", base, "--head", renamed, head=renamed)
    assert proc.returncode == 1
    assert _reported(proc) == [
        ".env: env-file",
        f"{added[:12]}:DB/.env: env-file",
        f"{added[:12]}:docs/link: local-symlink",
        f"{renamed[:12]}:.env: env-file",
    ]


def test_merges_are_checked_for_what_they_change_themselves(tmp_path: Path) -> None:
    h = History()
    base = h.commit("base", {"README.md": "PubDex\n", "merged.md": "a\n"})
    feature = h.commit("feature", {"feature.yml": j("pass", "word: ", SECRET, "\n")}, base)
    feature_fixed = h.commit("feature-fixed", {"feature.yml": "enabled: true\n", "merged.md": "b\n"}, feature)
    main = h.commit("main", {"merged.md": "c\n"}, base)
    token = j("gh", "p_", MIXED[:36])
    merge = h.commit("merge", {"feature.yml": "enabled: true\n", "merged.md": f"token {token}\n"}, main, feature_fixed)
    head = h.commit("head", {"merged.md": "d\n"}, merge)
    proc = _history_run(tmp_path, h, "--base", base, "--head", head, head=head)
    assert proc.returncode == 1
    # The feature commit's own change, and the merge's resolution, which
    # differs from both of its parents; nothing reported twice.
    assert sorted(_reported(proc)) == sorted([
        f"{feature[:12]}:feature.yml:1: secret-assignment",
        f"{merge[:12]}:merged.md:1: github-token",
    ])
    assert token not in proc.stdout + proc.stderr


@pytest.mark.parametrize("base", ["", ZERO_ID], ids=["empty", "all-zero"])
def test_without_a_base_every_reachable_commit_is_checked(tmp_path: Path, base: str) -> None:
    h = History()
    first = h.commit("first", {"config.yml": j("pass", "word: ", SECRET, "\n")})
    second = h.commit("second", {"config.yml": None, "README.md": "PubDex\n"}, first)
    proc = _history_run(tmp_path, h, "--base", base, "--head", second, head=second)
    assert proc.returncode == 1
    assert f"no base commit: checking every commit reachable from {second[:12]}" in proc.stdout
    assert _reported(proc) == [f"{first[:12]}:config.yml:1: secret-assignment"]
    rev_lists = [args for args, _stdin in _git_calls(tmp_path) if args[0] == "rev-list"]
    assert rev_lists == [["rev-list", "--reverse", "--topo-order", second]]


def test_a_base_missing_from_the_clone_checks_every_reachable_commit(tmp_path: Path) -> None:
    # A rewritten history: the old tip that a push event names is gone.
    h = History()
    first = h.commit("first", {"config.yml": j("pass", "word: ", SECRET, "\n")})
    second = h.commit("second", {"config.yml": None, "README.md": "PubDex\n"}, first)
    proc = _history_run(tmp_path, h, "--base", "f" * 40, "--head", second, head=second)
    assert proc.returncode == 1
    assert "is not a commit in this clone: checking every commit reachable from" in proc.stdout
    assert _reported(proc) == [f"{first[:12]}:config.yml:1: secret-assignment"]


@pytest.mark.parametrize(
    "problem, message",
    [
        ("shallow", "history is shallow"),
        ("missing-object", "(missing)"),
        ("unknown-head", "is not a commit in this clone"),
        ("failing-git", "git rev-list failed"),
        ("unreadable-output", "unexpected output from git diff-tree"),
    ],
)
def test_an_incomplete_check_of_the_commits_never_passes(tmp_path: Path, problem: str, message: str) -> None:
    h = History()
    base = h.commit("base", {"README.md": "PubDex\n"})
    head = h.commit("head", {"notes.md": "notes\n"}, base)
    args, describe = ["--base", base, "--head", head], {}
    if problem == "shallow":
        describe["shallow"] = True
    elif problem == "missing-object":
        describe["missing"] = [h.trees[head]["notes.md"][1]]  # a partial clone
    elif problem == "unknown-head":
        args[-1] = "no-such-branch"
    elif problem == "failing-git":
        describe["fail"] = {"rev-list": "fatal: synthetic failure"}
    else:
        describe["garble"] = head
    proc = _history_run(tmp_path, h, *args, head=head, **describe)
    assert proc.returncode == 2
    assert "check ok" not in proc.stdout
    assert "commits not checked" in proc.stderr and message in proc.stderr


def test_tree_findings_are_reported_when_the_commits_cannot_be_checked(tmp_path: Path) -> None:
    h = History()
    head = h.commit("head", {"config.yml": j("pass", "word: ", SECRET, "\n")})
    proc = _history_run(tmp_path, h, "--base", "", "--head", head, head=head, shallow=True)
    assert proc.returncode == 2
    assert "config.yml:1: secret-assignment" in proc.stderr
    assert "commits not checked" in proc.stderr


def test_an_accepted_finding_covers_its_published_commit_alone(tmp_path: Path, monkeypatch) -> None:
    h = History()
    leaked = python_source("PASS" "WORD = @SECRET@")
    published = h.commit("published", {"settings.py": leaked})
    later = h.commit("later", {"settings.py": "PASSWORD = None\n"}, published)
    back = h.commit("back", {"settings.py": leaked}, later)
    repo, env = _history_setup(tmp_path, h, back)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("STUB_GIT_LOG", env["STUB_GIT_LOG"])
    monkeypatch.setenv("STUB_GIT_HISTORY", env["STUB_GIT_HISTORY"])
    finding = ("settings.py", 1, "secret-assignment")
    assert checker.scan_commits(repo, "", back)[0] == [(f"{published[:12]}:settings.py", 1, "secret-assignment")]
    monkeypatch.setattr(checker, "ACCEPTED_IN_HISTORY", frozenset({(published, *finding)}))
    # The same text brought back by a later commit is reported there.
    assert checker.scan_commits(repo, "", back)[0] == [(f"{back[:12]}:settings.py", 1, "secret-assignment")]
    assert checker.scan_commits(repo, "", later)[0] == []


def test_accepted_findings_name_published_commits_precisely() -> None:
    assert checker.ACCEPTED_IN_HISTORY, "the published task-1a commit holds two accepted test values"
    for commit, path, line, rule in checker.ACCEPTED_IN_HISTORY:
        assert len(commit) == 40 and all(c in "0123456789abcdef" for c in commit)
        assert path and not path.startswith("/") and line > 0 and rule in checker.RULES


def test_range_options_are_checked(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("PubDex\n", encoding="utf-8")
    assert _run(["--root", str(tmp_path), "--head", "HEAD"], cwd=tmp_path).returncode == 2
    proc = _run(["--root", str(tmp_path), "--base", "HEAD", "README.md"], cwd=tmp_path)
    assert proc.returncode == 2 and "takes no paths" in proc.stderr


def _real_git(*args: str, stdin: str | None = None) -> bytes:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], input=stdin.encode() if stdin else None,
        capture_output=True, check=True, timeout=60,
    ).stdout


def test_the_commit_parsers_read_what_git_itself_prints() -> None:
    # This checkout's HEAD, read-only: needs no history beyond that commit.
    head = _real_git("rev-parse", "HEAD").decode().strip()
    changes = checker.parse_changes(
        _real_git("diff-tree", "--stdin", "-r", "-c", "--root", "--no-renames", "-z", stdin=head + "\n")
    )
    tree = {}
    for entry in _real_git("ls-tree", "-r", "-z", head).split(b"\0"):
        if entry:
            meta, path = entry.split(b"\t", 1)
            mode, _kind, object_id = meta.decode().split(" ")
            tree[os.fsdecode(path)] = (mode, object_id)
    for commit, path, mode, object_id in changes:
        assert commit == head and tree[path] == (mode, object_id)
    sample = [object_id for _c, _p, mode, object_id in changes if mode == "100644"][:5]
    if sample:
        objects = checker.parse_objects(_real_git("cat-file", "--batch", stdin="".join(o + "\n" for o in sample)), sample)
        assert [len(objects[o]) for o in sample] == [int(_real_git("cat-file", "-s", o)) for o in sample]

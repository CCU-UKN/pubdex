#!/usr/bin/env python3
# Reusable utilities for ORCID person search, works, and co-author extraction.
# orcidkit.py

from __future__ import annotations
import re, time, json, unicodedata, logging
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from typing import Dict, Any, List, Optional, Tuple, Iterable, Set
from urllib.parse import urlparse

import httpx

from .config import USER_AGENT  # single source for the polite contact UA

# ----------------------------
# Tunables & polite defaults
# ----------------------------
ORCID_BASE = "https://pub.orcid.org/v3.0"
REQUEST_TIMEOUT = 8.0
SLEEP_BETWEEN = 0.15
MAX_TRIES = 5
RETRY_STATUS = {429, 500, 502, 503, 504}

STOP_EARLY_AT_DEFAULT = 0.90     # stop trying more variants once a match is this confident
MAX_VARIANT_QUERIES = 8          # hard cap per person to avoid request explosions
WEAK_STRUCTURED_THRESHOLD = 0.55  # if best structured score is below this, run context text probes
CTX_MINLEN = 4                    # min length for context tokens used in text probes
MIN_ACCEPT_SCORE = 0.25           # below this, do not return a candidate (treat as "no match")
REQUIRE_FAMILY_OVERLAP_DEFAULT = True  # strict by default; can be relaxed by caller


TITLE_PREFIXES = [
    "prof.", "prof", "dr.", "dr", "assoc. prof.", "assoc prof", "asst. prof.", "asst prof",
    "mr.", "mr", "ms.", "ms", "mrs.", "mrs", "doc.", "doc", "dott.", "dott", "professor"
]
TITLE_SUFFIXES = [
    "phd", "md", "msc", "m.sc.", "m.sc", "bsc", "b.sc.", "b.sc", "dphil", "frs", "frcp"
]

_NAME_PARTICLES = {
    "da","de","di","do","dos","das",
    "del","della","der","den",
    "van","von","la","le","du"
}

# Very small stoplist for context tokens to avoid noisy text probes
_CTX_STOPWORDS = {
    # Keep this tiny and generic. We want to allow location/org clues such as
    # a city name or an organisation acronym to survive as probe tokens.
    "www", "the", "for", "and", "about", "people", "study", "studies",
    "exc", "uni", "university", "department", "institute",
    "lab", "labs", "researcher", "centre", "center",
}

# ----------------------------
# Name normalization utilities
# ----------------------------
def norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def ascii_fold(s: str) -> str:
    """
    ASCII-friendly rewrite for names. Map German umlauts BEFORE stripping diacritics,
    otherwise 'ü' -> 'u' and we lose the extra 'e' (Goldlücke -> Goldlucke).
    """
    if not s:
        return ""
    s = (s
         .replace("ß", "ss")
         .replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue")
         .replace("ä", "ae").replace("ö", "oe").replace("ü", "ue"))
    s_norm = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s_norm if not unicodedata.combining(ch))

def strip_titles(name: str) -> str:
    n = norm_ws(name)
    parts = [p.strip() for p in re.split(r"[,\u2013\u2014;]| - ", n) if p.strip()]
    n = " ".join(parts) if parts else n
    toks = n.split()
    out = []
    for t in toks:
        low = t.lower().strip(".")
        if low in [p.strip(".") for p in TITLE_PREFIXES]:
            continue
        out.append(t)
    n = " ".join(out)
    toks = n.split()
    while toks and toks[-1].lower().strip(".") in [sfx.strip(".") for sfx in TITLE_SUFFIXES]:
        toks = toks[:-1]
    return " ".join(toks).strip()

def split_name(name: str) -> Tuple[str, str]:
    n = norm_ws(name)
    if "," in n:  # "Family, Given"
        fam, giv = [p.strip() for p in n.split(",", 1)]
        return giv, fam
    toks = n.split()
    if len(toks) <= 1:
        return n, ""
    return " ".join(toks[:-1]), toks[-1]

def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9\s\-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _parts_no_particles(parts: List[str]) -> List[str]:
    return [p for p in parts if p.lower() not in _NAME_PARTICLES]

def family_variants(family: str) -> List[str]:
    """Prioritized small set of family-name candidates (de-duplicated, bounded)."""
    fam = norm_ws(family)
    if not fam:
        return [fam]
    parts = [p for p in re.split(r"[ \-]+", fam) if p]
    variants: List[str] = []
    seen = set()
    def add(v: str):
        v = v.strip()
        if v and v not in seen:
            seen.add(v); variants.append(v)
    add(fam)                       # as-is
    if len(parts) >= 1:
        add(parts[-1])             # last token (e.g., "Carberry")
        add(parts[0])              # first token (e.g., "Schardosim")
    if len(parts) >= 2:
        add(" ".join(parts[:2]))
        add(" ".join(parts[-2:]))
        add(" ".join(parts[1:]))   # family_without_first
        add(" ".join(parts[:-1]))  # family_without_last
    nparts = _parts_no_particles(parts)
    if nparts:
        add(" ".join(nparts))
        if len(nparts) >= 1:
            add(nparts[0]); add(nparts[-1])
        if len(nparts) >= 2:
            add(" ".join(nparts[:2])); add(" ".join(nparts[-2:]))
    return variants[:6]

def given_variants(given: str, family: str) -> List[str]:
    """Given-name variants that pair well with multiword family names."""
    g = norm_ws(given)
    out: List[str] = [g] if g else [""]
    fam = norm_ws(family)
    parts = [p for p in re.split(r"[ \-]+", fam) if p]
    if g and parts:
        pref = _parts_no_particles(parts[:-1])  # everything but last (prefix)
        if pref:
            out.append(f"{g} " + " ".join(pref))
        suff = _parts_no_particles(parts[1:])   # everything but first (suffix)
        if suff:
            out.append(f"{g} " + " ".join(suff))
    dedup = []
    seen = set()
    for v in out:
        v = v.strip()
        if v not in seen:
            seen.add(v); dedup.append(v)
    return dedup[:3]

def name_match_score(query_name: str, cand_given: str, cand_family: str) -> float:
    """
    Heuristic score in [0,1]. Rewards exact family match and initial match.
    Penalizes mismatched initials if both are present.
    """
    qgiv, qfam = split_name(query_name)
    # Fold diacritics before ascii normalization (e.g., Müller -> Mueller)
    qg, qf = _norm(ascii_fold(qgiv)), _norm(ascii_fold(qfam))
    cg, cf = _norm(ascii_fold(cand_given)), _norm(ascii_fold(cand_family))
    score = 0.0
    fam_eq = (qf and cf and (qf == cf or qf.replace(" ", "") == cf.replace(" ", "")))
    # Last-token family equality (e.g., "Font" vs "Font Massot")
    qf_last = qf.split()[-1] if qf else ""
    cf_last = cf.split()[-1] if cf else ""
    if fam_eq:
        score += 0.7
    elif qf and cf and qf_last and cf_last and qf_last == cf_last:
        score += 0.6  # strong hint for multi-token surnames
    if qg and cg and qg[:1] == cg[:1]:
        score += 0.2
    if qg and cg and (qg.split(" ")[0:1] == cg.split(" ")[0:1]):
        score += 0.1
    if qg and cg and qg[:1] and cg[:1] and qg[:1] != cg[:1]:
        score -= 0.1
    return max(0.0, min(1.0, score))


# ----------------------------
# Context & scoring helpers
# ----------------------------
def extract_tokens_from_context(row_like: Dict[str, Any], who: str = "", trace: bool = False) -> List[str]:
    raw: List[str] = []

    def add_tokens_from_host(host: str):
        # split by dots, then also split hyphenated labels
        # (example-university -> example, university)
        for label in host.split("."):
            if not label:
                continue
            subs = [label] + label.split("-")
            for s in subs:
                s = s.strip().lower()
                if s:
                    raw.append(s)

    # 1) URLs first (most discriminative)
    for col in ["contact.websites", "section_url"]:
        v = row_like.get(col)
        if isinstance(v, str):
            for url in re.split(r"[ ,;]+", v):
                try:
                    host = urlparse(url).netloc.lower()
                    if host:
                        add_tokens_from_host(host)
                except Exception:
                    pass
    # 2) Email domains next
    v = row_like.get("contact.email")
    if isinstance(v, str):
        m = re.search(r"@([A-Za-z0-9\.\-]+)", v)
        if m:
            add_tokens_from_host(m.group(1).lower())
    # 3) Free text last (less discriminative)
    for col in ["description", "section"]:
        v = row_like.get(col)
        if isinstance(v, str):
            raw.extend([t for t in re.split(r"[^a-zA-Z0-9]+", v.lower()) if t])

    # Deduplicate while preserving order; keep tokens of length >=3 and not stoplisted
    seen = set()
    tokens: List[str] = []
    for t in raw:
        if len(t) < 3:
            continue
        if t in _CTX_STOPWORDS:
            continue
        if t not in seen:
            seen.add(t)
            tokens.append(t)
    if trace:
        logging.debug(f'[TRACE] {who}: context tokens -> {tokens}')
    return tokens

def jaccard_boost(inst: Optional[str], ctx_tokens: List[str], who: str = "", trace: bool = False) -> float:
    if not inst or not ctx_tokens:
        return 0.0
    A = set(_norm(inst).split())
    B = set(_norm(" ".join(ctx_tokens)).split())
    if not A or not B:
        return 0.0
    inter = len(A & B)
    if inter == 0:
        return 0.0
    j = inter / len(A | B)
    boost = min(0.2, j * 0.2)
    if trace and boost > 0:
        logging.debug(f'[TRACE] {who}: affil overlap A∩B={inter}, |A|={len(A)}, |B|={len(B)}, J={j:.4f}, boost=+{boost:.3f} (inst="{inst}")')
    return boost


# ----------------------------
# HTTP client (retries + http2)
# ----------------------------
def http_get_json(client: httpx.Client, path: str, params: Dict[str, Any],
                  trace: bool = False, who: str = "") -> Dict[str, Any]:
    delay = SLEEP_BETWEEN
    for attempt in range(1, MAX_TRIES + 1):
        if trace:
            logging.debug(f'[TRACE] {who}: GET {path} params={params} attempt={attempt}')
        r = client.get(path, params=params)
        if r.status_code in RETRY_STATUS:
            ra = r.headers.get("Retry-After")
            try:
                ra_s = float(ra) if ra else None
            except Exception:
                ra_s = None
            time.sleep(ra_s or delay)
            delay = min(delay * 2, 8.0)
            if attempt == MAX_TRIES:
                if trace and r.status_code >= 500:
                    logging.debug(f'[TRACE] {who}: server {r.status_code} for {path} params={params}')
                r.raise_for_status()
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Unreachable")

@dataclass
class OrcidClient:
    base_url: str = ORCID_BASE
    user_agent: str = USER_AGENT
    timeout: float = REQUEST_TIMEOUT

    def __post_init__(self):
        self._client: Optional[httpx.Client] = None

    def __enter__(self):
        headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        self._client = httpx.Client(base_url=self.base_url, headers=headers, timeout=self.timeout,
                                    http2=True, follow_redirects=True)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._client:
            self._client.close()
        self._client = None

    @property
    def client(self) -> httpx.Client:
        assert self._client is not None, "Use OrcidClient as a context manager"
        return self._client

    # -------- person search ----------
    def expanded_search(self, q: str, rows: int, *, trace=False, who="") -> List[Dict[str, Any]]:
        data = http_get_json(self.client, "/expanded-search", {"q": q, "rows": rows}, trace=trace, who=who)
        return data.get("expanded-result", []) or []

    def expanded_search_given_family(self, given: str, family: str, rows: int,
                                     *, trace=False, who="") -> List[Dict[str, Any]]:
        q_parts = []
        if given:  q_parts.append(f'given-names:"{given}"')
        if family: q_parts.append(f'family-name:"{family}"')
        q = " AND ".join(q_parts) if q_parts else f'person-name:"{(given + " " + family).strip()}"'
        try:
            return self.expanded_search(q, rows, trace=trace, who=who)
        except httpx.HTTPStatusError as e:
            if trace and e.response is not None and e.response.status_code >= 500:
                logging.debug(f'[TRACE] {who}: server 5xx for q={q!r}; skipping this variant')
            return []

    # -------- works & co-authors ----------
    def list_works(self, orcid: str) -> Dict[str, Any]:
        # summary of works (titles, put-codes, external-ids)
        return http_get_json(self.client, f"/{orcid}/works", {}, trace=False, who=orcid)

    def get_work(self, orcid: str, put_code: int) -> Dict[str, Any]:
        # full work (includes contributors)
        return http_get_json(self.client, f"/{orcid}/work/{put_code}", {}, trace=False, who=f"{orcid}:{put_code}")


    # -------- person & employments ----------
    def get_person(self, orcid: str) -> Dict[str, Any]:
        return http_get_json(self.client, f"/{orcid}/person", {}, trace=False, who=f"{orcid}:person")

    def get_record(self, orcid: str) -> Dict[str, Any]:
        # includes history.last-modified-date and more
        return http_get_json(self.client, f"/{orcid}/record", {}, trace=False, who=f"{orcid}:record")

    def list_employments(self, orcid: str) -> Dict[str, Any]:
        return http_get_json(self.client, f"/{orcid}/employments", {}, trace=False, who=f"{orcid}:employments")


# ----------------------------
# ORCID search glue & scoring
# ----------------------------
def _slug(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9\-_.]+", "_", (s or "").strip())
    return re.sub(r"_+", "_", s).strip("_").lower() or "hits"

def _other_names_from_hit(h: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    raw = h.get("other-names") or h.get("other-names.name") or h.get("other-name")
    if isinstance(raw, list):
        for x in raw:
            if isinstance(x, dict):
                c = x.get("content") or x.get("value") or x.get("name")
                if c: out.append(str(c))
            elif isinstance(x, str):
                out.append(x)
    elif isinstance(raw, dict):
        inner = raw.get("other-name") or raw.get("name") or raw.get("value")
        if isinstance(inner, list):
            for x in inner:
                if isinstance(x, dict):
                    c = x.get("content") or x.get("value") or x.get("name")
                    if c: out.append(str(c))
                elif isinstance(x, str):
                    out.append(x)
    return out

def _full_name_match_score(query_name: str, full: str) -> float:
    g, f = split_name(strip_titles(full))
    return name_match_score(query_name, g, f)


def _family_tokens(s: str) -> Set[str]:
    s = ascii_fold(strip_titles(s))
    toks = re.split(r"[ \-]+", s.strip()) if s else []
    return set(t.lower() for t in toks if t)

def _family_overlap_ok(query_display_name: str, cand_family: str, aka_names: List[str]) -> bool:
    """Require some overlap in family tokens, or a strong AKA full-name match."""
    qg, qf = split_name(strip_titles(query_display_name))
    qfam = _family_tokens(qf)
    cfam = _family_tokens(cand_family)
    if qfam and cfam and (qfam & cfam):
        return True
    # Allow through if any AKA looks like a strong full-name match (hyphenated/compound families)
    if aka_names:
        best_aka = max((_full_name_match_score(query_display_name, a) for a in aka_names), default=0.0)
        if best_aka >= 0.80:
            return True
    return False


def pick_best_candidate(
    display_name: str,
    hits: List[Dict[str, Any]],
    ctx_tokens: List[str],
    *,
    trace: bool = False,
    min_accept_score: float = MIN_ACCEPT_SCORE,
    require_family_overlap: bool = REQUIRE_FAMILY_OVERLAP_DEFAULT,
) -> Tuple[Optional[str], float, str]:
    # Track two bests: with family-overlap preferred; and any fallback.
    best_fam = (None, 0.0, "no candidates")
    best_any = (None, 0.0, "no candidates")
    for h in hits:
        ocid = h.get("orcid-id")
        g = h.get("given-names") or ""
        f = h.get("family-names") or h.get("family-name") or ""
        inst = h.get("institution-name")
        if isinstance(inst, list):
            inst = next((i for i in inst if i), None)
        base = name_match_score(display_name, g, f)
        boost = jaccard_boost(inst, ctx_tokens, who=display_name, trace=trace)
        aka = _other_names_from_hit(h)
        aka_boost = 0.0
        if aka:
            best_aka = max((_full_name_match_score(display_name, a) for a in aka), default=0.0)
            if best_aka >= 0.80:
                aka_boost = 0.2
            elif best_aka >= 0.60:
                aka_boost = 0.1
        score = round(min(1.0, base + boost + aka_boost), 3)
        evidence = []
        if base >= 0.7: evidence.append("family+initial")
        elif base >= 0.5: evidence.append("partial-name")
        if boost > 0: evidence.append(f"affil-boost:{boost:.2f}")
        if aka_boost > 0: evidence.append(f"aka:{aka_boost:.2f}")
        ev = ",".join(evidence) if evidence else "name-heuristic"
        fam_ok = _family_overlap_ok(display_name, f, aka)
        if trace:
            logging.debug(f'[TRACE] {display_name}: cand orcid={ocid} g="{g}" f="{f}" inst="{inst}" '
                          f'base={base:.3f} +boost={boost:.3f} +aka={aka_boost:.3f} -> {score:.3f} ev={ev}')
        if fam_ok:
            if score > best_fam[1]:
                best_fam = (ocid, score, ev + ",fam-overlap")
        else:
            if score > best_any[1]:
                best_any = (ocid, score, ev + ",no-fam-overlap")
    # Prefer family-overlap; optionally forbid non-overlap entirely
    if best_fam[0]:
        chosen = best_fam
    else:
        if require_family_overlap:
            return (None, 0.0, "filtered:no-family-overlap")
        chosen = best_any
    # Final acceptance check
    if (not chosen[0]) or (chosen[1] < min_accept_score):
        reason = "low-score"
        return (None, 0.0, f"filtered:{reason}")
    return chosen

def orcid_expanded_search(client: OrcidClient, given: str, family: str, *, rows: int,
                          trace: bool = False, who: str = "", save_hits_json: bool = False,
                          allow_person_fallback: bool = True,
                          ctx_tokens: Optional[List[str]] = None,
                          allow_text_probes: bool = True) -> List[Dict[str, Any]]:
    def _run(q: str) -> List[Dict[str, Any]]:
        try:
            return client.expanded_search(q, rows, trace=trace, who=who)
        except httpx.HTTPStatusError as e:
            if trace and e.response is not None and e.response.status_code >= 500:
                logging.debug(f'[TRACE] {who}: server 5xx for q={q!r}; skipping this variant')
            return []
    # structured
    q1_parts = []
    if given: q1_parts.append(f'given-names:"{given}"')
    if family: q1_parts.append(f'family-name:"{family}"')
    q1 = " AND ".join(q1_parts) if q1_parts else f'person-name:"{(given + " " + family).strip()}"'
    hits = _run(q1)
    # folded
    if not hits:
        g2, f2 = ascii_fold(given), ascii_fold(family)
        if g2 != given or f2 != family:
            q2_parts = []
            if g2: q2_parts.append(f'given-names:"{g2}"')
            if f2: q2_parts.append(f'family-name:"{f2}"')
            q2 = " AND ".join(q2_parts) if q2_parts else f'person-name:"{(g2 + " " + f2).strip()}"'
            hits = _run(q2)
    # text-probe fallback (still under /expanded-search; avoid often-flaky person-name)
    if not hits and allow_text_probes:
        gf = ascii_fold((given + " " + family).strip())
        # For broad text probes, allow a higher rows cap to avoid clipping the right hit.
        def run_text(q: str) -> List[Dict[str, Any]]:
            nonlocal rows
            old_rows = rows
            rows = max(rows, 120)
            try:
                return _run(q)
            finally:
                rows = old_rows
        if gf:
            # 0) strict phrase on "given family"
            hits = run_text(f'text:"{gf}"')
            # 0.1) tokenized AND (less strict than phrase)
            if not hits:
                parts = [p for p in gf.split() if p]
                if len(parts) >= 2:
                    hits = run_text(" AND ".join([f'text:"{p}"' for p in parts[:2]]))

        if not hits and ctx_tokens:
            # pick up to 3 informative tokens in the order they appeared
            toks: List[str] = []
            seen_tok = set()
            for t in ctx_tokens:
                tl = ascii_fold(t.lower())
                if len(tl) >= CTX_MINLEN and tl not in _CTX_STOPWORDS and tl not in seen_tok:
                    seen_tok.add(tl); toks.append(tl)
                if len(toks) >= 3:
                    break
            # (a) phrase + context, then (a.1) tokenized name + context, then (b) given-name + context
            for tk in toks:
                hits = run_text(f'text:"{gf}" AND text:"{tk}"')
                if hits:
                    break
            if not hits and given and family:
                pair = f'text:"{ascii_fold(given)}" AND text:"{ascii_fold(family)}"'
                for tk in toks:
                    hits = run_text(pair + f' AND text:"{tk}"')
                    if hits:
                        break
            if not hits and given:
                for tk in toks:
                    hits = run_text(f'given-names:"{ascii_fold(given)}" AND text:"{tk}"')
                    if hits:
                        break

    # person-name fallback (last resort; some deployments return 5xx)
    if not hits and allow_person_fallback:
        q3 = f'person-name:"{ascii_fold((given + " " + family).strip())}"'
        try:
            old_rows = rows
            rows = min(rows, 15)
            hits = _run(q3)
        finally:
            rows = old_rows
    if trace and save_hits_json:
        fn = f'.orcid_hits_{_slug(who)}.json'
        try:
            with open(fn, "w", encoding="utf-8") as f:
                json.dump(hits, f, ensure_ascii=False, indent=2)
            logging.debug(f'[TRACE] {who}: saved raw hits -> {fn}')
        except Exception:
            pass
    return hits

def resolve_orcid_for_person(
    client: OrcidClient,
    given: str,
    family: str,
    display_name: str,
    row_like: Dict[str, Any],
    *,
    rows: int = 5,
    stop_early_at: float = STOP_EARLY_AT_DEFAULT,
    allow_person_fallback: bool = True,
    trace: bool = False,
    save_hits_json: bool = False,
    allow_structured_prefix: bool = True,
    min_accept_score: float = MIN_ACCEPT_SCORE,
    require_family_overlap: bool = REQUIRE_FAMILY_OVERLAP_DEFAULT,
) -> Dict[str, Any]:

    if trace:
        logging.debug(f'[TRACE] {display_name}: query given="{given}" family="{family}"')
    ctx = extract_tokens_from_context(row_like, who=display_name, trace=trace)
    best = (None, 0.0, "no candidates")
    best_q = (given, family)
    tried = 0
    g_opts = given_variants(given, family)
    f_opts = family_variants(family)
    for g_opt in g_opts:
        for f_opt in f_opts:
            if tried >= MAX_VARIANT_QUERIES:
                break
            tried += 1
            hits = orcid_expanded_search(client, g_opt, f_opt, rows=rows,
                                         trace=trace and (g_opt == given and f_opt == family),
                                         who=display_name, save_hits_json=save_hits_json,
                                         allow_person_fallback=False,
                                         ctx_tokens=ctx, allow_text_probes=True)
            ocid, score, ev = pick_best_candidate(
                display_name, hits, ctx, trace=trace,
                min_accept_score=min_accept_score,
                require_family_overlap=require_family_overlap
            )
            if score > best[1]:
                best = (ocid, score, ev + ("" if (g_opt == given and f_opt == family) else ",fam-variant"))
                best_q = (g_opt, f_opt)

            # If structured result is weak, force context-driven text probes anyway.
            if score < WEAK_STRUCTURED_THRESHOLD and ctx:
                def _run_text(q: str) -> List[Dict[str, Any]]:
                    nonlocal rows
                    old_rows = rows
                    rows = max(rows, 120)
                    try:
                        return client.expanded_search(q, rows=rows, trace=trace, who=display_name)
                    finally:
                        rows = old_rows
                gf = ascii_fold((g_opt + " " + f_opt).strip())
                # up to 3 informative tokens, in order
                toks: List[str] = []
                seen_tok = set()
                for t in ctx:
                    tl = ascii_fold(t.lower())
                    if len(tl) >= CTX_MINLEN and tl not in _CTX_STOPWORDS and tl not in seen_tok:
                        seen_tok.add(tl); toks.append(tl)
                    if len(toks) >= 3:
                        break
                # (a) phrase + context
                hits2: List[Dict[str, Any]] = []
                if gf:
                    for tk in toks:
                        hits2 = _run_text(f'text:"{gf}" AND text:"{tk}"')
                        if hits2: break
                # (a.1) tokenized name + context
                if not hits2 and g_opt and f_opt:
                    pair = f'text:"{ascii_fold(g_opt)}" AND text:"{ascii_fold(f_opt)}"'
                    for tk in toks:
                        hits2 = _run_text(pair + f' AND text:"{tk}"')
                        if hits2: break
                # (b) given-name + context (fallback)
                if not hits2 and g_opt:
                    for tk in toks:
                        hits2 = _run_text(f'given-names:"{ascii_fold(g_opt)}" AND text:"{tk}"')
                        if hits2: break
                if hits2:
                    ocid2, score2, ev2 = pick_best_candidate(
                        display_name, hits2, ctx, trace=trace,
                        min_accept_score=min_accept_score,
                        require_family_overlap=require_family_overlap
                    )
                    if score2 > best[1]:
                        best = (ocid2, score2, (ev2 + ",text-probe"))
                        best_q = (g_opt, f_opt)
            if score >= stop_early_at:
                break
        if best[1] >= stop_early_at:
            break

    # 1.5) If still nothing and family is a single token, try a structured prefix probe like family-name:font*
    # (Some ORCID indices accept a bare wildcard; if rejected, we just get [] and move on.)
    if best[1] < stop_early_at and allow_structured_prefix and family and (" " not in family):
        try:
            q = f'given-names:"{given}" AND family-name:{family}*'
            if trace:
                logging.debug(f'[TRACE] {display_name}: structured prefix probe q={q!r}')
            hits = client.expanded_search(q, rows, trace=trace, who=display_name)
            ocid, score, ev = pick_best_candidate(
                display_name, hits, ctx, trace=trace,
                min_accept_score=min_accept_score,
                require_family_overlap=require_family_overlap
            )
            if score > best[1]:
                best = (ocid, score, ev + ",fam-prefix")
                best_q = (given, family)
        except Exception:
            pass
    if best[1] < stop_early_at and allow_person_fallback:
        hits = orcid_expanded_search(client, given, family, rows=rows, trace=trace, who=display_name,
                                     save_hits_json=save_hits_json, allow_person_fallback=True,
                                     ctx_tokens=ctx, allow_text_probes=True)
        ocid, score, ev = pick_best_candidate(
            display_name, hits, ctx, trace=trace,
            min_accept_score=min_accept_score,
            require_family_overlap=require_family_overlap)
        if score > best[1]:
            best = (ocid, score, ev + ",person-fallback")
            best_q = (given, family)
    return {
        "query_given": best_q[0],
        "query_family": best_q[1],
        "display_name": display_name,
        "orcid": best[0],
        "confidence": float(best[1]),
        "confidence_label": "high" if best[1] >= 0.90 else "medium" if best[1] >= 0.75 else "low",
        "evidence": best[2],
    }


# ============================
# Profile enrichment helpers
# ============================
def _orcid_date_to_iso(date_obj: Optional[Dict[str, Any]]) -> Optional[str]:
    """ORCID dates are {year:{value}, month:{value}, day:{value}} with pieces optional."""
    if not isinstance(date_obj, dict):
        return None
    y = (date_obj.get("year") or {}).get("value")
    m = (date_obj.get("month") or {}).get("value")
    d = (date_obj.get("day") or {}).get("value")
    try:
        y = int(y) if y is not None else None
        m = int(m) if m is not None else None
        d = int(d) if d is not None else None
    except Exception:
        return None
    if not y:
        return None
    if not m:
        return f"{y:04d}"
    if not d:
        return f"{y:04d}-{m:02d}"
    return f"{y:04d}-{m:02d}-{d:02d}"

def _last_modified_iso(obj: Dict[str, Any]) -> Optional[str]:
    # last-modified-date.value is epoch millis
    lm = (obj.get("last-modified-date") or {}).get("value")
    if lm is None:
        return None
    try:
        ts = int(lm) / 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
    except Exception:
        return None

_ORG_STOP = {"of","for","the","and","der","die","das","de","da","do","du","den"}

def _norm_org_tokens(s: Optional[str]) -> Set[str]:
    """
    Normalize organization strings:
    - fold accents, lowercase
    - replace common variants/synonyms:
        universität|universitaet|universitat|university|uni -> uni
        centre|center|ctr -> center
        behaviour|behavior -> behavior
        institut|institute -> inst
    - drop punctuation and stopwords, split to tokens

    No organisation's names or acronyms are folded into each other: an
    install that wants several forms to match lists each one as a pattern.
    """
    if not s:
        return set()
    t = ascii_fold(s).lower()
    # unify well-known variants
    t = re.sub(r"\b(universitaet|universit[aä]t|universitat|university|uni)\b", "uni", t)
    t = re.sub(r"\b(centre|center|ctr)\b", "center", t)
    t = re.sub(r"\b(behaviour|behavior)\b", "behavior", t)
    t = re.sub(r"\b(institut|institute)\b", "inst", t)
    # strip punctuation to spaces
    t = re.sub(r"[^a-z0-9]+", " ", t)
    toks = [tok for tok in t.split() if tok and tok not in _ORG_STOP]
    return set(toks)

def _org_match(name: str, patterns: List[str]) -> bool:
    """
    Match if ALL tokens of any given pattern are contained in the org tokens,
    after normalization and synonym folding.
    Example: 'Example University' -> tokens ['example','university'] matches
             'Example University/Example Institute'
    """
    org_toks = _norm_org_tokens(name)
    if not org_toks:
        return False
    for p in patterns:
        ptoks = _norm_org_tokens(p)
        if ptoks and ptoks.issubset(org_toks):
            return True
    return False

def _iter_employment_summaries(employments_obj: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """Traverse employments summaries regardless of minor schema shape differences."""
    if not isinstance(employments_obj, dict):
        return []
    # Common shape: {"affiliation-group":[{"summaries":[{"employment-summary":{...}}, ...]}]}
    groups = employments_obj.get("affiliation-group") or []
    for g in groups:
        for s in (g.get("summaries") or []):
            es = s.get("employment-summary")
            if isinstance(es, dict):
                yield es
    # Fallbacks (handle direct lists just in case)
    for key in ("employment-summary", "employment-summaries"):
        es = employments_obj.get(key)
        if isinstance(es, list):
            for item in es:
                if isinstance(item, dict):
                    yield item
        elif isinstance(es, dict):
            yield es

def fetch_orcid_profile_enrichment(
    client: OrcidClient,
    orcid: str,
    *,
    display_name_for_scoring: str = "",
    org_patterns: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Returns {
      'first_name','last_name','name_match_score','verified_emails': [..],'verified_email_count',
      'last_updated','matched_employments': [ {org_name,department,role_title,start_date,end_date,is_current} ... ]
    }
    """
    org_patterns = org_patterns or []
    out: Dict[str, Any] = {
        "first_name": None,
        "last_name": None,
        "name_match_score": None,
        "verified_emails": [],
        "verified_email_count": 0,
        "last_updated": None,
        "matched_employments": [],
    }
    try:
        person = client.get_person(orcid)
    except Exception:
        person = {}
    try:
        record = client.get_record(orcid)
    except Exception:
        record = {}
    # names
    name_block = person.get("name") or {}
    gn = (name_block.get("given-names") or {}).get("value")
    fn = (name_block.get("family-name") or {}).get("value")
    out["first_name"] = gn or None
    out["last_name"] = fn or None
    if display_name_for_scoring and (gn or fn):
        out["name_match_score"] = round(name_match_score(display_name_for_scoring, gn or "", fn or ""), 3)
    # emails (public only; include only verified ones)
    emails = ((person.get("emails") or {}).get("email") or []) if isinstance(person, dict) else []
    verified = []
    for e in emails:
        try:
            if e.get("verified") and (e.get("visibility") or "").lower() == "public":
                addr = e.get("email")
                if addr:
                    verified.append(addr)
        except Exception:
            continue
    out["verified_emails"] = verified
    out["verified_email_count"] = len(verified)
    # last updated (prefer record; fall back to person)
    out["last_updated"] = _last_modified_iso(record) or _last_modified_iso(person)
    # employments
    try:
        emps = client.list_employments(orcid)
    except Exception:
        emps = {}
    matches = []
    def _harvest(emps_obj: Dict[str, Any]) -> None:
        for es in _iter_employment_summaries(emps_obj):
            org = (es.get("organization") or {})
            org_name = org.get("name")
            if not _org_match(org_name or "", org_patterns):
                continue
            start_iso = _orcid_date_to_iso(es.get("start-date"))
            end_iso = _orcid_date_to_iso(es.get("end-date"))
            matches.append({
                "org_name": org_name,
                "department": es.get("department-name"),
                "role_title": es.get("role-title"),
                "start_date": start_iso,
                "end_date": end_iso,
                "is_current": bool(start_iso and not end_iso),
            })
    _harvest(emps)
    # Fallback: some records expose employments under record.activities-summary.employments
    if not matches:
        rec_emps = ((record.get("activities-summary") or {}).get("employments") or {})
        _harvest(rec_emps)
    out["matched_employments"] = matches
    return out

# ----------------------------
# Works → co-authors helpers
# ----------------------------
def works_summary_records(works_obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Flatten /{orcid}/works summary into a list of {put_code, title, year, doi, type}.
    """
    out: List[Dict[str, Any]] = []

    # ORCID can return odd shapes or nulls here; be defensive.
    if not isinstance(works_obj, dict):
        return out

    groups = works_obj.get("group") or []
    for g in groups:
        if not isinstance(g, dict):
            continue
        summaries = g.get("work-summary") or []
        for s in summaries:
            # Some records contain null / non-dict entries in "work-summary".
            if not isinstance(s, dict):
                continue
            put = s.get("put-code")
            title = None
            tt = (s.get("title") or {}).get("title")
            if isinstance(tt, dict):
                title = tt.get("value")
            elif isinstance(tt, str):
                title = tt
            year = None
            if "publication-date" in s and isinstance(s["publication-date"], dict):
                y = (s["publication-date"].get("year") or {}).get("value")
                year = y
            # DOI: be robust if "external-ids" is null or oddly shaped
            doi = None
            ext_block = s.get("external-ids") or {}
            if not isinstance(ext_block, dict):
                # some records literally have "external-ids": null
                ext_block = {}
            ext = ext_block.get("external-id") or []
            # Normalize to a list of dicts
            if isinstance(ext, dict):
                ext = [ext]
            elif not isinstance(ext, list):
                ext = []
            for e in ext:
                if not isinstance(e, dict):
                    continue
                if (e.get("external-id-type") or "").lower() == "doi":
                    val = e.get("external-id-value")
                    if isinstance(val, str):
                        doi = val.lower()
                    break
            out.append({
                "put_code": put,
                "title": title,
                "year": year,
                "doi": doi,
                "type": s.get("type"),
            })
    return out

def extract_contributors(work_obj: Dict[str, Any]) -> List[Dict[str, Optional[str]]]:
    """
    From full work (/work/{put-code}), pull contributors -> [{name, orcid}...]
    """
    out: List[Dict[str, Optional[str]]] = []
    contribs = (work_obj.get("contributors") or {}).get("contributor") or []
    for c in contribs:
        name = None
        cn = c.get("credit-name")
        if isinstance(cn, dict):
            name = cn.get("value")
        elif isinstance(cn, str):
            name = cn
        ocid = None
        co = c.get("contributor-orcid")
        if isinstance(co, dict):
            path = (co.get("path") or "").strip()
            ocid = path or None
        out.append({"name": name, "orcid": ocid})
    return out

def coauthors_from_orcid(client: OrcidClient, author_orcid: str, *, fetch_full_works: bool = True
                         ) -> List[Dict[str, Any]]:
    """
    Return list of {author_orcid, put_code, title, year, doi, contributor_name, contributor_orcid}
    """
    works = client.list_works(author_orcid)
    summ = works_summary_records(works)
    rows: List[Dict[str, Any]] = []
    for s in summ:
        put = s.get("put_code")
        if fetch_full_works and put is not None:
            full = client.get_work(author_orcid, put)
            contribs = extract_contributors(full)
        else:
            contribs = []
        if contribs:
            for c in contribs:
                rows.append({
                    "author_orcid": author_orcid,
                    "put_code": put,
                    "title": s.get("title"),
                    "year": s.get("year"),
                    "doi": s.get("doi"),
                    "contributor_name": c.get("name"),
                    "contributor_orcid": c.get("orcid"),
                })
        else:
            rows.append({
                "author_orcid": author_orcid,
                "put_code": put,
                "title": s.get("title"),
                "year": s.get("year"),
                "doi": s.get("doi"),
                "contributor_name": None,
                "contributor_orcid": None,
            })
    return rows

#!/usr/bin/env python3
"""Two-sided consent handshake for mentor introductions (Initiative 07, epic 3).

The proof of value for this initiative: **a match cannot become an
introduction without explicit consent from both people, and either person
can withdraw at any non-terminal stage.**

Handshake lifecycle
-------------------
``awaiting_mentor`` (mentee requested = mentee's consent recorded)
  -> ``mutual``      (mentor approved; contact details unsealed)
  -> ``declined``    (mentor declined; cooldown before re-request)
  -> ``withdrawn``   (either party withdrew, while the handshake was live)
  -> ``expired``     (no mentor answer within TTL; re-request allowed)

Revelation rule
---------------
Neither the mentor's contact detail nor the mentee's chosen contact path
is revealed to the other side until the handshake is ``mutual``.
Redacted previews (name/initials, industry, role, seniority, topics,
ratings, match reasons) are visible before consent; everything that can
identify how to reach someone is sealed. All text-rendered preview
fields — including name, industry, role, and seniority — are scrubbed
for contact patterns (email, phone, URL, including bare domains like
``linkedin.com/in/x``) and show ``[redacted]`` in their place, so the
revelation rule holds even when a mentor's bio, availability text, or
any other card field contains contact details.

Local-first honesty contract
----------------------------
Veto is local-first: it never transmits on anyone's behalf. The handshake
record is a *receipt book*, not a messaging channel. ``request_introduction``
records YOUR consent (the mentee act). The mentor's side is recorded via
``mentor_respond`` when you log their out-of-band reply — the function
requires an explicit ``channel`` (how the mentor communicated the
decision) and refuses to fabricate consent. There is deliberately no API
that "approves on behalf of" the other party without that record.

Audit
-----
Every state transition appends one line to ``consent_audit.jsonl``. Each
line carries ``prev_hash`` (the previous line's ``entry_hash``, or
``GENESIS`` for the first line) and ``entry_hash`` (SHA-256 over the
canonical JSON of the record), forming a tamper-evident hash chain:
:func:`verify_audit` recomputes every link and fails closed on any
forged, edited, reordered, deleted-prior-entry, or hash-less line.
Honest limits: the chain protects the integrity of *prior* entries. It
is silent against tail truncation (deleting the newest line(s) leaves a
still-valid chain) and against appended forgery by anyone who can
compute a valid hash chain themselves — there is no API to edit or
delete audit entries, but the log has no notarization against a
filesystem-level attacker. Deletion requests (see
:mod:`initiatives.i07.safety`) redact handshake payloads but keep a
tombstone in the audit trail.

Failure posture
---------------
Malformed records fail CLOSED, never crash the read path: a corrupt
timestamp cannot prove a request is within its TTL or a cooldown has
elapsed, so expiry treats it as expired and the cooldown stays enforced.
A corrupt ``handshakes.json`` raises :class:`CorruptStoreError` and every
public entry point refuses to operate on it. Store writes are atomic
(tmp file + fsync + rename), so a torn write cannot leave a half-written
store behind.

Synthetic fixtures are always labeled: any test or demo handshake id
starts with ``SYNTHETIC-``. The module never invents mentors, people, or
sessions.

Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("veto-mcp.i07.consent")

BASE_DIR = Path(__file__).resolve().parent

#: Handshake records. Reassign in tests to redirect storage.
HANDSHAKES_FILE = BASE_DIR / "handshakes.json"

#: Append-only audit log. Reassign in tests.
AUDIT_FILE = BASE_DIR / "consent_audit.jsonl"

#: Pending requests expire after this long without a mentor answer.
REQUEST_TTL = timedelta(days=7)

#: After a decline (or a mentor withdrawal of a pending request), the
#: mentee must wait this long before re-requesting the same mentor.
DECLINE_COOLDOWN = timedelta(days=30)

#: Max new introduction requests per mentee per rolling 24h.
MAX_REQUESTS_PER_DAY = 5

#: Terminal states: no further transitions except re-request (new record).
TERMINAL_STATES = ("mutual", "declined", "withdrawn", "expired")

#: Fields that may appear in a pre-consent preview of a mentor card.
PREVIEW_FIELDS = (
    "id",
    "name",
    "industry",
    "role",
    "seniority",
    "topics",
    "bio",
    "availability",
    "remaining_capacity",
    "rating_summary",
    "reasons",
    "score",
)

#: Preview fields scrubbed for contact patterns (Contract s3). This covers
#: every text-rendered preview field — including the structured identity
#: fields (name, industry, role, seniority) — because a card can carry
#: contact details in ANY field ("name": "jane.doe@example.com"). It also
#: covers the numeric-typed fields (remaining_capacity, score): the scrub
#: passes non-string scalars through unchanged, so a hand-edited store
#: that stuffed a contact string into one of them can no longer bypass
#: the loop. The deliberate preferred_contact="linkedin_public" opt-in
#: keeps its URL only when the URL itself carries no contact payload —
#: a contact in its query string or path fails closed to [redacted].
PREVIEW_SCRUB_FIELDS = (
    "id",
    "name",
    "industry",
    "role",
    "seniority",
    "bio",
    "availability",
    "reasons",
    "topics",
    "rating_summary",
    "remaining_capacity",
    "score",
)

#: Contact fields: sealed until mutual consent.
CONTACT_FIELDS = ("linkedin_url", "email", "phone")


class CorruptStoreError(Exception):
    """``handshakes.json`` exists but cannot be parsed or is not an object.

    Raised by :func:`_load` instead of silently returning an empty store.
    Every public entry point catches this and refuses to operate
    (fail closed); :func:`list_handshakes` lets it propagate, loudly.
    """


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO timestamp; None on any malformed input (fail closed)."""
    try:
        return datetime.fromisoformat(value or "")
    except (ValueError, TypeError):
        return None


def _load() -> dict[str, dict[str, Any]]:
    """Load the handshake store. Raises CorruptStoreError on corruption.

    A missing file is not corruption — it is an empty store. A file that
    exists but does not parse, or parses to a non-object, is corruption:
    failing closed here is what keeps a torn or hand-edited store from
    being silently treated as "no handshakes".
    """
    try:
        raw = HANDSHAKES_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error(
            "CORRUPT handshake store at %s (%s). Refusing to operate; "
            "restore from backup before retrying.",
            HANDSHAKES_FILE,
            exc,
        )
        raise CorruptStoreError(
            f"handshake store at {HANDSHAKES_FILE} is corrupt ({exc}); "
            "refusing to operate. Restore from backup before retrying."
        ) from exc
    if not isinstance(data, dict):
        log.error(
            "CORRUPT handshake store at %s: top level is %s, not an object. "
            "Refusing to operate.",
            HANDSHAKES_FILE,
            type(data).__name__,
        )
        raise CorruptStoreError(
            f"handshake store at {HANDSHAKES_FILE} is corrupt "
            f"(top level is {type(data).__name__}, not an object); "
            "refusing to operate."
        )
    return data


def _guarded_load() -> tuple[dict[str, dict[str, Any]] | None, dict[str, Any] | None]:
    """Return (store, None), or (None, error-dict) on a corrupt store."""
    try:
        return _load(), None
    except CorruptStoreError as exc:
        return None, {"ok": False, "error": str(exc)}


def _save(store: dict[str, dict[str, Any]]) -> None:
    """Atomic store write: tmp file + fsync + rename + dir fsync.

    A crash mid-write can leave the tmp file behind but never a
    half-written ``handshakes.json`` — the rename is the commit point.
    """
    HANDSHAKES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = HANDSHAKES_FILE.with_name(HANDSHAKES_FILE.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(store, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, HANDSHAKES_FILE)
    try:  # make the rename itself durable
        dirfd = os.open(HANDSHAKES_FILE.parent, os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(dirfd)
    except OSError:
        pass
    finally:
        os.close(dirfd)


# ---------------------------------------------------------------------------
# Audit: tamper-evident hash chain
# ---------------------------------------------------------------------------

#: prev_hash of the first audit entry. A fixed genesis marker means a
#: forged "first" line cannot silently restart the chain.
_AUDIT_GENESIS_PREV = "GENESIS"


def _canonical(entry: dict[str, Any]) -> str:
    """Canonical JSON for hashing: sorted keys, no whitespace."""
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _audit_prev_hash() -> str:
    """The entry_hash of the last audit line, or the genesis marker."""
    try:
        raw = AUDIT_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _AUDIT_GENESIS_PREV
    last = ""
    for line in raw.splitlines():
        if line.strip():
            last = line
    if not last:
        return _AUDIT_GENESIS_PREV
    try:
        parsed = json.loads(last)
        return str(parsed.get("entry_hash") or _AUDIT_GENESIS_PREV)
    except (json.JSONDecodeError, AttributeError):
        # The tail is not a valid entry; verify_audit() will flag it.
        return _AUDIT_GENESIS_PREV


def _audit(event: str, handshake_id: str, actor: str, detail: str = "") -> None:
    """Append one audit line, chained to the previous line's hash.

    There is no API to edit or delete these. Each entry carries
    ``prev_hash`` (previous entry's ``entry_hash``) and ``entry_hash``
    (SHA-256 over the canonical record), so :func:`verify_audit` detects
    any forged, edited, reordered, deleted-prior-entry, or hash-less
    line. Known limits: tail truncation and appended self-consistent
    forgery by a filesystem-level attacker are NOT detected — see
    :func:`verify_audit`.
    """
    AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry: dict[str, Any] = {
        "at": _now(),
        "event": event,
        "handshake_id": handshake_id,
        "actor": actor,
        "detail": detail,
        "prev_hash": _audit_prev_hash(),
    }
    entry["entry_hash"] = hashlib.sha256(
        _canonical(entry).encode("utf-8")
    ).hexdigest()
    with AUDIT_FILE.open("a", encoding="utf-8") as fh:
        fh.write(_canonical(entry) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def verify_audit() -> dict[str, Any]:
    """Verify the tamper-evident hash chain of the whole audit file.

    Recomputes every ``entry_hash`` and checks every ``prev_hash`` link.
    Fails CLOSED: returns ``{"ok": False, "failed_line": n, "reason": ...}``
    on the first forged, edited, reordered, deleted-prior-entry,
    unparseable, or hash-less line. Verification is always whole-file —
    the chain links every entry regardless of handshake, so filtering
    would hide linkage breaks.

    Guarantee limits (documented, not bugs): the chain attests the
    integrity of *prior* entries. It does NOT detect tail truncation —
    deleting the newest line(s) leaves a still-valid chain — nor an
    appended self-consistent forgery by an attacker who can compute
    valid hashes themselves (e.g. anyone with filesystem write access).
    There is no notarization of the log against such an attacker; the
    "no API to edit or delete" claim is about the module's API surface,
    not the filesystem.

    Lines written before the hash chain existed (no hash fields) do NOT
    verify; archive pre-chain audit files instead of mixing formats.
    """
    try:
        raw = AUDIT_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"ok": True, "checked": 0}
    checked = 0
    expected_prev = _AUDIT_GENESIS_PREV
    for lineno, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return {
                "ok": False,
                "checked": checked,
                "failed_line": lineno,
                "reason": "line is not valid JSON",
            }
        if not isinstance(entry, dict):
            return {
                "ok": False,
                "checked": checked,
                "failed_line": lineno,
                "reason": "line is not a JSON object",
            }
        prev_hash = entry.get("prev_hash")
        entry_hash = entry.get("entry_hash")
        if not isinstance(prev_hash, str) or not isinstance(entry_hash, str):
            return {
                "ok": False,
                "checked": checked,
                "failed_line": lineno,
                "reason": (
                    "line has no hash fields — not a chain entry "
                    "(possible forgery, or a pre-chain legacy line)"
                ),
            }
        if prev_hash != expected_prev:
            return {
                "ok": False,
                "checked": checked,
                "failed_line": lineno,
                "reason": (
                    "prev_hash does not match the previous entry_hash — "
                    "chain broken (reorder, deletion, or forgery)"
                ),
            }
        recomputed = hashlib.sha256(
            _canonical(
                {k: v for k, v in entry.items() if k != "entry_hash"}
            ).encode("utf-8")
        ).hexdigest()
        if recomputed != entry_hash:
            return {
                "ok": False,
                "checked": checked,
                "failed_line": lineno,
                "reason": "entry_hash mismatch — entry was altered after writing",
            }
        expected_prev = entry_hash
        checked += 1
    return {"ok": True, "checked": checked}


def _new_id(synthetic: bool = False) -> str:
    return ("SYNTHETIC-" if synthetic else "hs-") + uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Contact-pattern scrubbing for pre-consent previews (Contract §3)
# ---------------------------------------------------------------------------
#
# ARCHITECTURE (round-10 rewrite): normalize-then-detect, default-deny.
#
# The pre-consent preview is built DEFAULT-DENY: only allowlisted fields
# are emitted, and every field goes through a safe-value policy before it
# reaches the output (see redacted_preview()):
#   * Closed-vocabulary fields (the hash-shaped "id", the seniority band,
#     the TOPICS enum) pass only values drawn from their vocabulary —
#     safe by construction, never inspected character-by-character.
#   * Numeric fields (remaining_capacity, score) pass only real numbers;
#     a contact STRING smuggled into one becomes the [redacted] marker.
#   * Free-text fields are normalized, then inspected for contact spans.
#
# Normalization first, detection second. Each field is projected onto a
# "scan" string: Unicode NFKD folding (fullwidth "＠" -> "@", and —
# structurally, via unicodedata — every accented Latin letter folds to
# its base: "à" -> "a", with the decomposed combining marks stripped),
# explicit folding of separators/punctuation NFKD leaves alone
# (ideographic full stop 。 -> ".", CJK commas 、 ， -> space, middle
# dots · ‧ -> ".", × -> "x"), stripping of invisible codepoints (the
# WHOLE Unicode Cf/Mn/Me classes — zero-width spaces, word joiners,
# LRM/RLM, Mongolian vowel separator, invisible plus, tag characters —
# never a hardcoded handful), blank-but-not-whitespace codepoints
# (braille blank U+2800, Hangul fillers U+115F/U+1160/U+3164) treated as
# separators, URL-encoded separators ("%40" -> "@") folded AFTER NFKD
# and invisible-stripping so split sequences rejoin, folding of dash
# lookalikes (en/em dashes, fullwidth hyphen -> "-"), and homoglyph
# folding driven by UTS #39 confusables.txt (not a hand-enumerated
# table: every non-ASCII, non-digit character with an ASCII-letter
# confusable folds to it) plus a small documented override set where
# confusables.txt dead-ends on pixel-identical Latin lookalikes
# (Cyrillic м/п/к/н -> m/n/k/H, palochka -> l, small-cap T -> t).
# Every detector below runs on the scan string, so an evasion that only
# re-encodes the text ("ｊａｎｅ＠ｅｘａｍｐｌｅ．ｃｏｍ") is caught by
# construction. Detected spans map back to the ORIGINAL text and the
# original span — invisible characters and all — is replaced with the
# [redacted] marker, so nothing survives beside it.
#
# Unicode bidi override characters (U+202A–U+202E) reorder text
# visually: no normalization can show what the READER sees, so a field
# containing them is withheld wholesale (the whole field becomes the
# marker) rather than scrubbed character-by-character. The modern
# isolate controls (U+2066–U+2069) are the RECOMMENDED mechanism and do
# not reorder hostilely, so legitimate RTL text using them is scrubbed
# normally instead of withheld.
#
# Detector classes (all run on the normalized scan):
#   * email: user@host.tld, Unicode/IDN labels, plus-tags,
#     whitespace-tolerant, zero-width-tolerant (via normalization).
#   * phone: every shape the old scrubber knew (NANP 10-digit, 7-digit
#     local, +international, 00/011 IDD, bare country code, parens,
#     contiguous 7–15, separated 2-group/3+-group, extension tails) —
#     plus spelled-out digits ("five five five 0132"), alphanumeric
#     vanity ("1-800-FLOWERS"), and (0) trunk prefixes.
#   * url: schemed (http/https, hxxp), www., bare domains on ANY TLD
#     (no TLD allowlist — an allowlist fails open on every new gTLD),
#     obfuscated dots ("linkedin[.]com", "linkedin(.)com",
#     "linkedin dot com"), IPv4 literals (with ports).
#   * prose-obfuscated contact: "jane at example dot com", "jane[at]…".
#   * messaging handles: Telegram/Signal/Discord/Skype-keyword forms,
#     "live:…" Skype names, name#1234 Discord tags, bare @handles.
#
# Fail-closed by design: a 7–15 digit run that is not a phone number
# (e.g. a 13-digit ISBN) is over-scrubbed rather than leaked. The
# documented guards stay byte-identical: ZIP+4, years, versions, 911,
# prices, dates, signed decimals, short counts, prose abbreviations,
# and @-mentions that are not handles.

#: Explicit marker left where contact patterns are scrubbed.
SCRUB_MARKER = "[redacted]"

#: Unicode bidi override controls. A field containing any of these is
#: withheld wholesale (see _scrub_contact_patterns): bidi OVERRIDES
#: reorder the VISUAL text, so span detection on the logical string
#: cannot see what the reader sees. Deliberately NARROW: only the
#: dangerous overrides (U+202A–U+202E). The modern isolate controls
#: (U+2066–U+2069) are the Unicode-recommended mechanism, do not reorder
#: hostilely, and appear in legitimate RTL text (Arabic-script bios) —
#: withholding on them was an availability bug, so they are scrubbed
#: normally instead.
_BIDI_OVERRIDES = frozenset("\u202a\u202b\u202c\u202d\u202e")


def _has_bidi_override(text: str) -> bool:
    return any(c in _BIDI_OVERRIDES for c in text)


#: Folding applied BEFORE NFKD: separators and punctuation that NFKD
#: leaves alone (or folds the wrong way for our purposes). CJK commas
#: become spaces (they are the standard digit separators in CJK text);
#: the ideographic full stop becomes "."; middle dots become "."; the
#: multiplication sign becomes "x" (vanity/extension detection).
_PRE_FOLD = {
    "、": " ",  # U+3001 IDEOGRAPHIC COMMA
    "，": " ",  # U+FF0C FULLWIDTH COMMA (NFKC would give "," — want space)
    "､": " ",  # U+FF64 HALFWIDTH IDEOGRAPHIC COMMA
    "。": ".",  # U+3002 IDEOGRAPHIC FULL STOP (NFKC leaves it)
    "·": ".",  # U+00B7 MIDDLE DOT
    "‧": ".",  # U+2027 HYPHENATION POINT
    "×": "x",  # U+00D7 MULTIPLICATION SIGN
    "\u2212": "-",  # MINUS SIGN (Sm, not Pd — NFKC leaves it)
}


#: URL-encoded separators that still read as contact syntax to a
#: human: "%40" -> "@", "%2E"/"%2e" -> ".". Applied as multi-char
#: sequences before the single-char fold (see _build_scan).
_SEQ_FOLD = {"%40": "@", "%2E": ".", "%2e": "."}


#: Homoglyph folding groups: (ASCII target, lookalike characters).
#:
#: GENERATED, not hand-enumerated — from Unicode confusables.txt v16.0.0
#: (UTS #39), keeping entries whose source is a single non-ASCII,
#: non-digit character and whose target is ASCII alphanumeric (digit
#: sources AND digit targets are excluded: folding must never corrupt
#: digit detection; zero-for-o is handled by a dedicated shadow
#: instead). Regenerate with ~/workspace/i07-adversary/gen_fold_table.py.
#: Unioned in:
#:   * legacy round-9 entries that confusables.txt lacks AND that NFKD
#:     + combining-mark stripping does not already fold (e.g. 'đ', 'ł',
#:     'ø', 'ß', 'æ' — 'à'->'a' style accents are covered structurally
#:     by unicodedata and need no table row).
#:   * OVERRIDES where confusables.txt is structurally deficient for
#:     Latin-lookalike detection: Cyrillic м/п/к/н map in the file to
#:     OTHER non-ASCII lookalikes (ʍ/π/ĸ/ʜ) that are themselves dead
#:     ends — the chain never reaches ASCII — while the characters are
#:     pixel-identical to Latin m/n/k/H; palochka U+04CF maps to 'i'
#:     but its spoof use is as 'l' ("live:"); small-cap T U+1D1B has no
#:     entry at all. These six fold to the visually-identical ASCII
#:     letter, cited here rather than scattered through the table.
_CONFUSABLE_GROUPS = (
    ('A', 'ΑАᎪᗅꓮＡ𐊠𖽀\U0001ccd6𝐀𝐴𝑨𝒜𝓐𝔄𝔸𝕬𝖠𝗔𝘈𝘼𝙰𝚨𝛢𝜜𝝖𝞐'),
    ('a', 'ɑαа⍺ａ𝐚𝑎𝒂𝒶𝓪𝔞𝕒𝖆𝖺𝗮𝘢𝙖𝚊𝛂𝛼𝜶𝝰𝞪'),
    ('AA', 'Ꜳ'),
    ('aa', 'ꜳ'),
    ('AE', 'ÆӔ'),
    ('ae', 'æӕ'),
    ('AO', 'Ꜵ'),
    ('ao', 'ꜵ'),
    ('AR', '🜇'),
    ('AU', 'Ꜷ'),
    ('au', 'ꜷ'),
    ('AV', 'ꜸꜺ'),
    ('av', 'ꜹꜻ'),
    ('AY', 'Ꜽ'),
    ('ay', 'ꜽ'),
    ('B', 'ΒВᏴᗷℬꓐꞴＢ𐊂𐊡𐌁\U0001ccd7𝐁𝐵𝑩𝓑𝔅𝔹𝕭𝖡𝗕𝘉𝘽𝙱𝚩𝛣𝜝𝝗𝞑'),
    ('b', 'ƄЬᏏᑲᖯ𝐛𝑏𝒃𝒷𝓫𝔟𝕓𝖇𝖻𝗯𝘣𝙗𝚋'),
    ('bl', 'Ы'),
    ('C', 'ϹСᏟℂℭⅭⲤꓚＣ𐊢𐌂𐐕𐔜𑣲\U0001ccd8𝐂𝐶𝑪𝒞𝓒𝕮𝖢𝗖𝘊𝘾𝙲🝌'),
    ('c', 'ϲсᴄⅽⲥꮯｃ𐐽𝐜𝑐𝒄𝒸𝓬𝔠𝕔𝖈𝖼𝗰𝘤𝙘𝚌'),
    ('D', 'ĐᎠᗞᗪⅅⅮꓓ\U0001ccd9𝐃𝐷𝑫𝒟𝓓𝔇𝔻𝕯𝖣𝗗𝘋𝘿𝙳'),
    ('d', 'đԁᏧᑯⅆⅾꓒ𝐝𝑑𝒅𝒹𝓭𝔡𝕕𝖉𝖽𝗱𝘥𝙙𝚍'),
    ('DZ', 'Ǳ'),
    ('Dz', 'ǲ'),
    ('dz', 'ǳʣ'),
    ('E', 'ΕЕᎬℰ⋿ⴹꓰＥ𐊆𑢦𑢮\U0001ccda𝐄𝐸𝑬𝓔𝔈𝔼𝕰𝖤𝗘𝘌𝙀𝙴𝚬𝛦𝜠𝝚𝞔'),
    ('e', 'εеҽ℮ℯⅇꬲｅ𝐞𝑒𝒆𝓮𝔢𝕖𝖊𝖾𝗲𝘦𝙚𝚎'),
    ('F', 'ϜᖴℱꓝꞘ𐊇𐊥𐔥𑢢𑣂\U0001ccdb𝈓𝐅𝐹𝑭𝓕𝔉𝔽𝕱𝖥𝗙𝘍𝙁𝙵𝟊'),
    ('f', 'ſքẝꞙꬵ𝐟𝑓𝒇𝒻𝓯𝔣𝕗𝖋𝖿𝗳𝘧𝙛𝚏'),
    ('FAX', '℻'),
    ('ff', 'ﬀ'),
    ('ffi', 'ﬃ'),
    ('ffl', 'ﬄ'),
    ('fi', 'ﬁ'),
    ('fl', 'ﬂ'),
    ('G', 'ԌᏀᏳꓖ\U0001ccdc𝐆𝐺𝑮𝒢𝓖𝔊𝔾𝕲𝖦𝗚𝘎𝙂𝙶'),
    ('g', 'ƍɡցᶃℊｇ𝐠𝑔𝒈𝓰𝔤𝕘𝖌𝗀𝗴𝘨𝙜𝚐'),
    ('H', 'ĦΗНнᎻᕼℋℌℍⲎꓧＨ𐋏\U0001ccdd𝐇𝐻𝑯𝓗𝕳𝖧𝗛𝘏𝙃𝙷𝚮𝛨𝜢𝝜𝞖'),
    ('h', 'ħһհᏂℎｈ𝐡𝒉𝒽𝓱𝔥𝕙𝖍𝗁𝗵𝘩𝙝𝚑'),
    ('I', 'ΙІ'),
    ('i', 'ıɩɪ˛ͺιіӏᎥιℹⅈⅰ⍳ꙇꭵｉ𑣃𝐢𝑖𝒊𝒾𝓲𝔦𝕚𝖎𝗂𝗶𝘪𝙞𝚒𝚤𝛊𝜄𝜾𝝸𝞲'),
    ('ii', 'ⅱ'),
    ('iii', 'ⅲ'),
    ('ij', 'ĳ'),
    ('iv', 'ⅳ'),
    ('ix', 'ⅸ'),
    ('J', 'ͿЈᎫᒍꓙꞲＪ\U0001ccdf𝐉𝐽𝑱𝒥𝓙𝔍𝕁𝕵𝖩𝗝𝘑𝙅𝙹'),
    ('j', 'ϳјⅉｊ𝐣𝑗𝒋𝒿𝓳𝔧𝕛𝖏𝗃𝗷𝘫𝙟𝚓'),
    ('K', 'ΚКᏦᛕKⲔꓗＫ𐔘\U0001cce0𝐊𝐾𝑲𝒦𝓚𝔎𝕂𝕶𝖪𝗞𝘒𝙆𝙺𝚱𝛫𝜥𝝟𝞙'),
    ('k', 'κк𝐤𝑘𝒌𝓀𝓴𝔨𝕜𝖐𝗄𝗸𝘬𝙠𝚔'),
    ('L', 'ĿŁᏞᒪℒⅬⳐꓡ𐐛𐔦𑢣𑢲𖼖\U0001cce1𝈪𝐋𝐿𝑳𝓛𝔏𝕃𝕷𝖫𝗟𝘓𝙇𝙻'),
    ('l', 'ŀłƖǀΙІӀӏ׀וןاߊᛁℐℑℓⅠⅼ∣⏽ⲒⵏꓲﺍﺎＩｌ￨𐊊𐌉𐌠𖼨\U0001ccde\U0001ccf1𝐈𝐥𝐼𝑙𝑰𝒍𝓁𝓘𝓵𝔩𝕀𝕝𝕴𝖑𝖨𝗅𝗜𝗹𝘐𝘭𝙄𝙡𝙸𝚕𝚰𝛪𝜤𝝞𝞘𞣇𞸀𞺀'),
    ('LJ', 'Ǉ'),
    ('Lj', 'ǈ'),
    ('lJ', 'Ĳ'),
    ('lj', 'ǉ'),
    ('ll', 'ǁװ‖Ⅱ∥'),
    ('lll', 'Ⅲ'),
    ('lO', 'Ю'),
    ('ls', 'ʪ'),
    ('lt', '₶'),
    ('lV', 'Ⅳ'),
    ('lX', 'Ⅸ'),
    ('lz', 'ʫ'),
    ('M', 'ΜϺМᎷᗰᛖℳⅯⲘꓟＭ𐊰𐌑\U0001cce2𝐌𝑀𝑴𝓜𝔐𝕄𝕸𝖬𝗠𝘔𝙈𝙼𝚳𝛭𝜧𝝡𝞛'),
    ('m', 'μм'),
    ('MB', '🝫'),
    ('N', 'ΝℕⲚꓠＮ𐔓\U0001cce3𝐍𝑁𝑵𝒩𝓝𝔑𝕹𝖭𝗡𝘕𝙉𝙽𝚴𝛮𝜨𝝢𝞜'),
    ('n', 'ŉηпոռ𝐧𝑛𝒏𝓃𝓷𝔫𝕟𝖓𝗇𝗻𝘯𝙣𝚗'),
    ('NJ', 'Ǌ'),
    ('Nj', 'ǋ'),
    ('nj', 'ǌ'),
    ('No', '№'),
    ('O', 'ØΟОՕଠዐⲞⵔ〇ꓳＯ𐊒𐊫𐐄𐓂𐔖𑢵\U0001cce4\U0001ccf0𝐎𝑂𝑶𝒪𝓞𝔒𝕆𝕺𝖮𝗢𝘖𝙊𝙾𝚶𝛰𝜪𝝤𝞞'),
    ('o', 'øοσоօסهھہەంಂംഠංဝჿᴏᴑℴⲟꬽﮦﮧﮨﮩﮪﮫﮬﮭﻩﻪﻫﻬｏ𐐬𐓪𑣈𑣗𝐨𝑜𝒐𝓸𝔬𝕠𝖔𝗈𝗼𝘰𝙤𝚘𝛐𝛔𝜊𝜎𝝄𝝈𝝾𝞂𝞸𝞼𞸤𞹤𞺄'),
    ('OE', 'Œ'),
    ('oe', 'œ'),
    ('OO', 'ꚘꝎ'),
    ('oo', '∞ꚙꝏ'),
    ('P', 'ΡРᏢᑭℙⲢꓑＰ𐊕\U0001cce5𝐏𝑃𝑷𝒫𝓟𝔓𝕻𝖯𝗣𝘗𝙋𝙿𝚸𝛲𝜬𝝦𝞠'),
    ('p', 'ρϱр⍴ⲣｐ𝐩𝑝𝒑𝓅𝓹𝔭𝕡𝖕𝗉𝗽𝘱𝙥𝚙𝛒𝛠𝜌𝜚𝝆𝝔𝞀𝞎𝞺𝟈'),
    ('Q', 'ℚⵕ\U0001cce6𝐐𝑄𝑸𝒬𝓠𝔔𝕼𝖰𝗤𝘘𝙌𝚀'),
    ('q', 'ԛգզ𝐪𝑞𝒒𝓆𝓺𝔮𝕢𝖖𝗊𝗾𝘲𝙦𝚚'),
    ('QE', '🜀'),
    ('R', 'ƦᎡᏒᖇℛℜℝꓣ𐒴𖼵\U0001cce7𝈖𝐑𝑅𝑹𝓡𝕽𝖱𝗥𝘙𝙍𝚁'),
    ('r', 'гᴦⲅꭇꭈꮁ𝐫𝑟𝒓𝓇𝓻𝔯𝕣𝖗𝗋𝗿𝘳𝙧𝚛'),
    ('rn', 'ⅿ𑜀𝐦𝑚𝒎𝓂𝓶𝔪𝕞𝖒𝗆𝗺𝘮𝙢𝚖'),
    ('Rs', '₨'),
    ('S', 'ЅՏᏕᏚꓢＳ𐊖𐐠𖼺\U0001cce8𝐒𝑆𝑺𝒮𝓢𝔖𝕊𝕾𝖲𝗦𝘚𝙎𝚂'),
    ('s', 'ƽѕꜱꮪｓ𐑈𑣁𝐬𝑠𝒔𝓈𝓼𝔰𝕤𝖘𝗌𝘀𝘴𝙨𝚜'),
    ('ss', 'ß'),
    ('sss', '🝜'),
    ('st', 'ﬆ'),
    ('T', 'ŦΤТᎢ⊤⟙ⲦꓔＴ𐊗𐊱𐌕𑢼𖼊\U0001cce9𝐓𝑇𝑻𝒯𝓣𝔗𝕋𝕿𝖳𝗧𝘛𝙏𝚃𝚻𝛵𝜯𝝩𝞣🝨'),
    ('t', 'ŧτᴛ𝐭𝑡𝒕𝓉𝓽𝔱𝕥𝖙𝗍𝘁𝘵𝙩𝚝'),
    ('TEL', '℡'),
    ('tf', 'ꝷ'),
    ('ts', 'ʦ'),
    ('U', 'ΥՍሀᑌ∪⋃ꓴ𐓎𑢸𖽂\U0001ccea𝐔𝑈𝑼𝒰𝓤𝔘𝕌𝖀𝖴𝗨𝘜𝙐𝚄'),
    ('u', 'ʋυսᴜꞟꭎꭒ𐓶𑣘𝐮𝑢𝒖𝓊𝓾𝔲𝕦𝖚𝗎𝘂𝘶𝙪𝚞𝛖𝜐𝝊𝞄𝞾'),
    ('ue', 'ᵫ'),
    ('uo', 'ꭣ'),
    ('V', 'ѴᏙᐯⅤⴸꓦꛟ𐔝𑢠𖼈\U0001cceb𝈍𝐕𝑉𝑽𝒱𝓥𝔙𝕍𝖁𝖵𝗩𝘝𝙑𝚅'),
    ('v', 'νѵטᴠⅴ∨⋁ꮩｖ𑜆𑣀𝐯𝑣𝒗𝓋𝓿𝔳𝕧𝖛𝗏𝘃𝘷𝙫𝚟𝛎𝜈𝝂𝝼𝞶'),
    ('VB', '🝬'),
    ('vi', 'ⅵ'),
    ('vii', 'ⅶ'),
    ('viii', 'ⅷ'),
    ('Vl', 'Ⅵ'),
    ('Vll', 'Ⅶ'),
    ('Vlll', 'Ⅷ'),
    ('W', 'ԜᎳᏔꓪ𑣯\U0001ccec𝐖𝑊𝑾𝒲𝓦𝔚𝕎𝖂𝖶𝗪𝘞𝙒𝚆'),
    ('w', 'ɯωѡԝաᴡꮃ𑜊𑜎𑜏𝐰𝑤𝒘𝓌𝔀𝔴𝕨𝖜𝗐𝘄𝘸𝙬𝚠'),
    ('X', 'ΧХ᙭ᚷⅩ╳ⲬⵝꓫꞳＸ𐊐𐊴𐌗𐌢𐔧𑣬\U0001cced𝐗𝑋𝑿𝒳𝓧𝔛𝕏𝖃𝖷𝗫𝘟𝙓𝚇𝚾𝛸𝜲𝝬𝞦'),
    ('x', '×χхᕁᕽ᙮ⅹ⤫⤬⨯ｘ𝐱𝑥𝒙𝓍𝔁𝔵𝕩𝖝𝗑𝘅𝘹𝙭𝚡'),
    ('xi', 'ⅺ'),
    ('xii', 'ⅻ'),
    ('Xl', 'Ⅺ'),
    ('Xll', 'Ⅻ'),
    ('Y', 'ΥϒУҮᎩᎽⲨꓬＹ𐊲𑢤𖽃\U0001ccee𝐘𝑌𝒀𝒴𝓨𝔜𝕐𝖄𝖸𝗬𝘠𝙔𝚈𝚼𝛶𝜰𝝪𝞤'),
    ('y', 'ɣʏγуүყᶌỿℽꭚｙ𑣜𝐲𝑦𝒚𝓎𝔂𝔶𝕪𝖞𝗒𝘆𝘺𝙮𝚢𝛄𝛾𝜸𝝲𝞬'),
    ('Z', 'ΖᏃℤℨꓜＺ𐋵𑢩\U0001ccef𝐙𝑍𝒁𝒵𝓩𝖅𝖹𝗭𝘡𝙕𝚉𝚭𝛧𝜡𝝛𝞕'),
    ('z', 'ᴢꮓ𑣄𝐳𝑧𝒛𝓏𝔃𝔷𝕫𝖟𝗓𝘇𝘻𝙯𝚣'),
)


def _build_fold_table() -> dict[str, str]:
    """Homoglyph folding: confusable lookalikes -> ASCII.

    Applied per character AFTER NFKD. This is detection-only: the scan
    string is never shown, so folding cannot change output — it only
    lets the detectors see through visual spoofs like "linkedin.ϲοm"
    (Greek omicron) or "jane@münchen.de".
    """
    table: dict[str, str] = {}
    for ascii_s, variants in _CONFUSABLE_GROUPS:
        for v in variants:
            table[v] = ascii_s
    return table
_FOLD = _build_fold_table()

#: Unicode categories stripped as invisible during scanning: Cf (format
#: controls: zero-width spaces, word joiners, LRM/RLM, Mongolian vowel
#: separator, invisible plus, tag characters — the whole class), Mn/Me
#: (combining marks, including the "zalgo" class of overlaid text used
#: to hide contact strings visually).
_INVISIBLE_CATEGORIES = frozenset(("Cf", "Mn", "Me"))


def _is_invisible(c: str) -> bool:
    return unicodedata.category(c) in _INVISIBLE_CATEGORIES


#: Codepoints that RENDER as blanks but are neither stripped as
#: invisible (Cf/Mn/Me) nor matched by \s: U+2800 BRAILLE PATTERN BLANK
#: (So), U+3164 HANGUL FILLER and U+115F/U+1160 HANGUL JAMO FILLERs
#: (Lo). Ordinary spaces (Zs, including U+00A0/U+3000) already match
#: \s. These are treated as separators during scanning, so
#: "555<braille-blank>0132" cannot smuggle a number past the detectors.
_BLANK_SEPARATORS = frozenset("\u2800\u3164\u115f\u1160")


def _build_scan(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Project ``text`` onto a normalized scan string for detection.

    Returns (scan, span_map) where span_map[i] is the (start, end)
    ORIGINAL span of scan[i], so detector spans map back to the
    original text — including multi-char folds ("%40" -> "@" covers
    all three original characters).

    Pass 1 (per character): blank separators -> space; explicit
    pre-fold; else NFKD, then per NFKD output char: drop invisibles
    (Cf/Mn/Me), fold Unicode dashes (Pd) to "-", fold homoglyphs,
    else keep. Linear-time: strictly per-character.
    Pass 2 (on the cleaned string): URL-encoded contact separators
    ("%40" -> "@", "%2E"/"%2e" -> "."). This runs AFTER NFKD and the
    invisible-strip on purpose: "％40" (fullwidth percent, NFKD ->
    "%") and "%<ZWSP>40" (split by an invisible that is now gone)
    both rejoin into "%40" before this check, closing the
    normalization-differential hole.
    """
    mid_chars: list[str] = []
    mid_map: list[tuple[int, int]] = []
    for offset, c in enumerate(text):
        span = (offset, offset + 1)
        if c in _BLANK_SEPARATORS:
            mid_chars.append(" ")
            mid_map.append(span)
            continue
        folded = _PRE_FOLD.get(c)
        if folded is not None:
            for fc in folded:
                mid_chars.append(fc)
                mid_map.append(span)
            continue
        # Visual confusable fold BEFORE NFKD: the fold table encodes
        # what the character LOOKS like (the spoof model); NFKD encodes
        # compatibility equivalence. When a character has both, the
        # visual fold is what the detectors need — e.g. U+03F2 (lunate
        # sigma, looks like 'c') NFKD-decomposes to U+03C2 'ς', which
        # would miss the "disϲord" spoof; the table fold 'c' catches it.
        # (48 table entries diverge from their NFKD decomposition, all
        # toward ASCII-letter folds; none produce digits, so digit
        # detection is unaffected.)
        if c in _FOLD:
            for hc in _FOLD[c]:
                mid_chars.append(hc)
                mid_map.append(span)
            continue
        for nc in unicodedata.normalize("NFKD", c):
            if _is_invisible(nc):
                continue
            # Separators NFKD folds the wrong way (or not at all) get the
            # same explicit fold as the pre-NFKD pass: "｡" (U+FF61)
            # NFKD-folds to "。" (U+3002), which must still become ".".
            nc = _PRE_FOLD.get(nc, nc)
            for fc in nc:
                cat = unicodedata.category(fc)
                if cat == "Pd":
                    mid_chars.append("-")
                    mid_map.append(span)
                elif fc in _FOLD:
                    for hc in _FOLD[fc]:
                        mid_chars.append(hc)
                        mid_map.append(span)
                else:
                    mid_chars.append(fc)
                    mid_map.append(span)
    mid = "".join(mid_chars)
    scan_chars: list[str] = []
    span_map: list[tuple[int, int]] = []
    i, n = 0, len(mid)
    while i < n:
        # Multi-char URL-encoded separators: "%40" -> "@", "%2E" -> ".".
        seq = _SEQ_FOLD.get(mid[i : i + 3])
        if seq is not None:
            seq_span = (mid_map[i][0], mid_map[i + 2][1])
            for fc in seq:
                scan_chars.append(fc)
                span_map.append(seq_span)
            i += 3
        else:
            scan_chars.append(mid_chars[i])
            span_map.append(mid_map[i])
            i += 1
    return "".join(scan_chars), span_map


# ---------------------------------------------------------------------------
# Detectors (all run on the normalized scan string)
# ---------------------------------------------------------------------------

#: Email: local@domain.tld. Local part is anything but whitespace/@
#: (and ":", so a "Email:jane@example.com" label is not swallowed into
#: the address — ":" is not valid in an unquoted local part anyway),
#: bounded to 64 chars (RFC 5321 §4.5.3.1.1: the local part is at most
#: 64 octets — the bound also keeps the lazy quantifier off its O(n^2)
#: path when "@" sits at the end of a long dot-run);
#: domain labels are anything but whitespace/@/dot; the final dot is
#: glued to a 2+ letter TLD, so "3.12"/"2.0"-style numeric dots can
#: never match. Whitespace tolerated around @ and dots (copy-paste
#: artifacts); zero-width tolerance comes from normalization.
_EMAIL_RE = re.compile(
    r"[^\s@:]{1,64}?\s*@\s*(?:[^\s@.]+\s*\.\s*)+[^\W\d_]{2,}",
    re.IGNORECASE,
)

#: Phone numbers, every shape. All separators are the plain ASCII set —
#: Unicode variants were folded away during normalization. Branches are
#: ordered longest/most-specific first so a longer number always wins
#: over a shorter prefix of itself.
_PHONE_RE = re.compile(
    # IDD-prefix token: "00" or "011" as its own token, optional (0)
    # trunk prefix, 1-3 digit country code, then >= 2 digit groups
    # totaling >= 7 digits after the cc. FIRST so "00442079460018"
    # cannot mangle to "[redacted]0018" via the 10-digit branch.
    r"(?<!\d)(?:\(0\)[\s.\-:/]*)?(?:00|011)[\s.\-:/]*\(?\d{1,3}\)?"
    r"(?=(?:\D*\d){7})(?:[\s.\-:/]*\(?\d{1,8}\)?){2,}"
    # International: +<cc> (1-4 digits) followed by one or more digit
    # groups of any length. A dot separator must be followed by 2+
    # digits, so signed decimals ("+3.5", "+2.0") cannot match.
    r"|\+\d{1,4}(?:\.\d{2,8}|[\s,:-]*\(?\d{1,8}\)?)+"
    # 16+ digit runs: card-number length, fail-closed, whole run.
    r"|(?<!\d)\d{16,}(?!\d)"
    # Bare country code (no IDD/+): 1-3 digit cc + >= 2 separated
    # groups totaling >= 7 digits, e.g. "44 20 7946 0018".
    r"|(?<!\d)(?:\(0\)[\s.\-:/]*)?\d{1,3}(?=(?:\D*\d){7})"
    r"(?:[\s.\-:/]+\(?\d{1,8}\)?){2,}(?!\d)"
    # Parenthesized area code, non-US groupings: "(020) 7946 0018",
    # "(555) 0199". (?<![\d(]) not (?<!\S): "Call(020) …" must match.
    r"|(?<![\d(])\(\d{2,5}\)[\s.\-:/]*(?:\d{4,}(?:[\s.\-:/]+\d+)*"
    r"|\d+(?:[\s.\-:/]+\d+){1,})"
    # NANP with country-code token "1": "1 415 555 0132",
    # "14155550132", and "1" glued to a 7-digit local ("1555-0132").
    r"|(?<!\d)1(?:[\s.\-:/]*\(?\d{3}\)?[\s.\-:/]*\d{3}[\s.\-:/]*\d{4}"
    r"|\d{3}[\s.\-:/]+\d{4})(?!\d)"
    # 10-digit, optional +<cc> prefix: "415-555-0132", "+1 415 555 0132".
    r"|(?<!\d)(?:\+\d{1,3}[\s.\-:/]*)?\(?\d{3}\)?[\s.\-:/]*\d{3}"
    r"[\s.\-:/]*\d{4}(?!\d)"
    # Separated 3+-group runs with a 4-8 digit first group,
    # e.g. "5555-555-0132". Middle/last groups are 3-8 digits so
    # date-like "2024 05 13" (4+2+2) can never match.
    r"|(?<!\d)\d{4,8}(?:[\s.\-:/]+\d{3,8}){2,}(?!\d)"
    # Separated 2-group runs totaling 7-15 digits, any grouping:
    # "5555-0132", "8123-4567", "555555-0132". Group lengths are
    # enumerated so the digit total stays in [7,15]; the 5+4 shape
    # (canonical US ZIP+4, "94110-1234") scrubs only with a leading-zero
    # second group ("55555-0132", trunk-style local part).
    r"|(?<!\d)(?:\d[\s.\-:/]+\d{6,11}|\d{2}[\s.\-:/]+\d{5,11}"
    r"|\d{3}[\s.\-:/]+\d{4,11}|\d{4}[\s.\-:/]+\d{3,11}"
    r"|\d{5}[\s.\-]+(?:\d{2,3}|\d{5,10}|0\d{3})"
    r"|\d{6}[\s.\-:/]+\d{1,9}|\d{7}[\s.\-:/]+\d{1,8}"
    r"|\d{8}[\s.\-:/]+\d{1,7})(?!\d)"
    # Contiguous 7-15 digit runs, no separators.
    r"|(?<!\d)\d{7,15}(?!\d)"
    # 7-digit local with separators: "555-0132". (?<!\d) not \b: CJK
    # "电话555-0132" must scrub; trailing (?!\w) is the word-boundary
    # twin that keeps "555-0132x" safe.
    r"|(?<!\d)\d{3}[\s.\-:/]+\d{4}(?!\w)"
)

#: Extension tail glued to a scrubbed number: "415 555 0132 x123",
#: "555-0132 #456", "555-0132 x12-34" (the whole tail, including trailing
#: "-digits", is consumed — a scrubbed "[redacted] -34" still leaks the
#: extension's digits). Applied to merged contact spans (not just phone
#: spans): an extension exists only to reach the number's owner.
#: "extra"/"extent" are safe — after "ext" the next token must be
#: digits. "#" is an ext marker only adjacent to a contact span, so
#: "Room #5" prose can never match.
_EXT_RE = re.compile(
    r"[\s.\-:/]*(?:ext\.?|[xX#])[\s.\-:/]*\d+(?:[\s.\-:/]*\d+)*",
    re.IGNORECASE,
)

#: URL: schemed (http/https, hxxp) and www.-prefixed. Bare
#: (scheme-less) domains are handled by _bare_domain_spans below, not
#: by regex: the natural pattern ``(?:label\.)+tld`` re-tries its
#: iteration count at every dot on a pathological dot-run ("a."*5000
#: took 5.5s — O(n^2)). The candidate+validate form is linear-time.
_URL_RE = re.compile(
    r"https?://[^\s)\]]+"
    r"|hxxps?://[^\s)\]]+"
    r"|www\.[^\s)\]]+",
    re.IGNORECASE,
)

#: Bare-domain candidate: any non-space run (brackets/quotes excluded so
#: markdown "[text](url)" and "(url)" don't glue). Validated in Python
#: (see _bare_domain_spans) — linear-time, no backtracking.
_BARE_CANDIDATE_RE = re.compile(r"(?<!\w)[^\s()\[\]<>\"']{1,300}")

_TRAILING_JUNK = ").,;:!?"


def _bare_domain_spans(scan: str) -> list[tuple[int, int]]:
    """Bare (scheme-less) domains: ``linkedin.com/in/x``, ``jane.me``.

    TLD-AGNOSTIC by design: there is no TLD allowlist. An allowlist
    fails open on every new gTLD (``.xyz``, ``.design``, …) and silently
    leaks them verbatim. Instead the final label must be all letters,
    2+ chars — that structurally excludes version numbers ("3.12"),
    decimals ("2.0"), and section refs ("4.5") from ever matching, so
    ordinary prose cannot over-scrub. Residual risk: rare prose
    dot-forms with letter labels ("noon.tomorrow") will scrub;
    accepted over a verbatim contact leak.
    """
    out = []
    for m in _BARE_CANDIDATE_RE.finditer(scan):
        cand = m.group(0)
        start = m.start()
        slash = cand.find("/")
        host = cand if slash < 0 else cand[:slash]
        host = host.rstrip(_TRAILING_JUNK)
        # Optional :port — "example.com:8080" is still a bare domain.
        port = ""
        pm = re.search(r":\d{1,5}$", host)
        if pm:
            port = pm.group(0)
            host = host[: pm.start()]
        labels = host.split(".")
        if len(labels) < 2:
            continue
        if any(not lbl or len(lbl) > 63 for lbl in labels):
            continue
        # Underscore is not a valid hostname character, but renderers
        # linkify "my_site.com" as a URL — and a scrubber that leaves a
        # clickable contact is a scrubber that failed. Fail closed.
        if not all(
            all(ch.isalnum() or ch in "-_" for ch in lbl) for lbl in labels
        ):
            continue
        tld = labels[-1]
        if len(tld) < 2 or not tld.isalpha():
            continue
        end = start + len(host) + len(port)
        if slash >= 0:
            # A "/path" belongs to the URL: include it, minus trailing junk.
            path = cand[slash:].rstrip(_TRAILING_JUNK)
            end = start + slash + len(path)
        if end > start:
            out.append((start, end))
    return out


def _url_spans(scan: str) -> list[tuple[int, int]]:
    out = _iter_spans(_URL_RE, scan)
    out.extend(_bare_domain_spans(scan))
    return out

#: IPv4 literals, optional port: "192.168.0.1", "10.0.0.1:8080".
_IPV4_RE = re.compile(r"(?<!\w)\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?(?!\w)")

#: Prose-obfuscated email: "jane at example dot com", "jane[at]…[dot]…".
#: The plain-dot alternative is sentence-aware: a dot followed by
#: whitespace ends the match ("email me at jane. Thanks" stays
#: verbatim) UNLESS the dot was itself space-separated ("jane @
#: example . com", the spaced-obfuscation unit). The trailing label
#: requirement (a real domain label after the last dot-word) is what
#: keeps "look at this. It is good" safe. The local part allows the
#: apostrophe ("o'brien at example dot com" is a real address shape).
_ATDOT_DOT = r"(?:(?<=\s)\.\s+|\.(?!\s)|\[dot\]|\(dot\)|\{dot\}|\bdot\b)"
_ATDOT_RE = re.compile(
    r"[A-Za-z0-9._%+\-']{1,40}\s*(?:@|\[at\]|\(at\)|\{at\}|\bat\b)\s*"
    r"[A-Za-z0-9.\-]{1,40}\s*" + _ATDOT_DOT + r"\s*"
    r"[A-Za-z0-9.\-]{1,40}"
    r"(?:\s*" + _ATDOT_DOT + r"\s*[A-Za-z0-9.\-]{1,40})*",
    re.IGNORECASE,
)

_OBF_SEP = r"(?:\[dot\]|\(dot\)|\{dot\}|\[\.\]|\{\.\}|\[\s*\.\s*\]|\(\.\))"

#: Obfuscated-dot domains: "linkedin[.]com", "linkedin(.)com",
#: "linkedin dot com". The word-"dot" form requires SPACES around the
#: word ("example dot com"); the hyphenated prose form ("the dot-com
#: bubble") is left alone — the bracket forms are unambiguous enough
#: without spaces.
_OBFDOT_RE = re.compile(
    r"(?<!\w)[A-Za-z0-9][A-Za-z0-9.\-]{0,64}"
    r"(?:\s*" + _OBF_SEP + r"\s*|\s+\bdot\b\s+)"
    r"[A-Za-z0-9][A-Za-z0-9.\-]{0,64}"
    r"(?:(?:\s*" + _OBF_SEP + r"\s*|\s+\bdot\b\s+)[A-Za-z0-9][A-Za-z0-9.\-]{0,64})*",
    re.IGNORECASE,
)

#: Spelled-out phone numbers: 7+ consecutive number-words
#: ("five five five 0132", "nine one one …"). The 7-token floor is the
#: digit-count floor (7+ digits = a dialable local number).
_NUM_WORDS = frozenset(
    ("zero", "one", "two", "three", "four", "five", "six", "seven",
     "eight", "nine", "oh")
)
_SPELLED_RE = re.compile(
    r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|oh)"
    r"(?:[\s.,:/\-]+(?:zero|one|two|three|four|five|six|seven|eight|nine|oh)){6,}\b",
    re.IGNORECASE,
)

#: Mixed words-and-digits: same idea, digit groups allowed between the
#: words ("five 555-0132", "five five five 0132"). Needs 3+ tokens AND
#: at least one number-word AND digit-value >= 7 — so "2024 05 13"
#: (a date, 8 digit-value but no words) and "1,000,000" (no words)
#: stay verbatim while "five five five 0132" (3+4) is caught.
_MIXED_RE = re.compile(
    r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|oh|\d{1,4})"
    r"(?:[\s.,:/\-]+(?:zero|one|two|three|four|five|six|seven|eight|nine|oh|\d{1,4})){2,}\b",
    re.IGNORECASE,
)

#: Alphanumeric vanity numbers: "1-800-FLOWERS", "1800FLOWERS",
#: "1 800 FLOWERS". Three structural shapes:
#:   * dash/dot/colon-separated ("1-800-FLOWERS"): any letter case —
#:     the punctuation separator is unambiguous;
#:   * letter-GLUED ("1800FLOWERS"): any case — a digit run glued to
#:     letters is not ordinary prose;
#:   * space-separated ("1 800 FLOWERS"): UPPER-CASE letters only —
#:     this is what keeps "1800 flowers delivered" (a quantity, not a
#:     number) verbatim while catching the spaced vanity form.
#: No re.IGNORECASE: the case distinction in the third branch is load-
#: bearing, so letter classes spell out both cases explicitly.
_VANITY_RE = re.compile(
    r"\b(?:1[\s\-.:]*)?8(?:00|88|77|66|55|44|33)"
    r"(?:[-.:]+[A-Za-z]{2,}(?:[-.:]*[A-Za-z]{2,})*"
    r"|[A-Za-z]{2,}(?:[-.:]*[A-Za-z]{2,})*"
    r"|[ \t]+[A-Z]{2,}(?:[\s\-.:]*[A-Z]{2,})*)"
)

#: Messaging handles, keyword-anchored: "Telegram: @janedoe_dev",
#: "Signal: @janedoe.42", "Discord: janedoe#1234". The colon form takes
#: a handle with or without @; the bare-space form requires @ (so
#: "telegram me at home" cannot match "me").
_HANDLE_KW_RE = re.compile(
    r"\b(?:telegram|signal|discord|skype|wechat|tg)\b\s*:\s*"
    r"(@?[A-Za-z0-9_.][A-Za-z0-9_.#\-]{1,31})"
    r"|\b(?:telegram|signal|discord|skype|wechat|tg)\b\s+"
    r"(@[A-Za-z0-9_.][A-Za-z0-9_.#\-]{1,31})",
    re.IGNORECASE,
)

#: Keyword-proximity handles with no punctuation: "my skype name is
#: janedoe". The "name is" declaration is what makes this safe — a bare
#: "<keyword> <word>" form would false-positive on ordinary prose
#: ("a strong signal here"), so the keyword alone is never enough
#: without @, :, or this declaration. "telegram me at home" has no
#: "name is" and stays verbatim.
_HANDLE_NAMEIS_RE = re.compile(
    r"\b(?:telegram|signal|discord|skype|wechat|tg)\b\s+"
    r"(?:my\s+|the\s+)?name\s+is\s+"
    r"(@?[A-Za-z0-9_.][A-Za-z0-9_.#\-]{1,31})",
    re.IGNORECASE,
)

#: Skype "live:" names: "live:janedoe123".
_SKYPE_LIVE_RE = re.compile(r"\blive:[A-Za-z0-9_.]{2,}\b", re.IGNORECASE)

#: Bare Discord tags: "janedoe#1234".
_DISCORD_TAG_RE = re.compile(r"\b[A-Za-z0-9_.]{2,}#\d{2,}\b")

#: Bare @handles with no keyword: "@janedoe_dev". 4+ chars after @ —
#: the floor is 4, not 5, because real handles are short ("@jane"),
#: with one documented exemption: "@ noon" (a time expression, not a
#: handle) is guarded by the verbatim battery, so the 4-char form
#: refuses the word "noon". The lookbehind is the ASCII word class, not
#: \w, on purpose: Python's \w matches CJK ideographs, so (?<!\w)
#: fails after "微信" and "微信@janedoe" would leak — the same narrowing
#: the phone branch uses ("电话555-0132" must scrub). E-mail addresses
#: never reach this: the email detector consumes them first.
_BARE_AT_RE = re.compile(r"(?<![A-Za-z0-9_])@(?!noon\b)[A-Za-z0-9_][A-Za-z0-9_]{3,}\b")


def _iter_spans(rx: "re.Pattern[str]", scan: str, group: int = 0) -> list[tuple[int, int]]:
    """All non-empty spans of ``rx`` (or its capture ``group``) in ``scan``."""
    out = []
    for m in rx.finditer(scan):
        s, e = m.span(group)
        if e > s:
            out.append((s, e))
    return out


def _phone_spans(scan: str) -> list[tuple[int, int]]:
    """Phone spans, each extended over a glued extension tail ("x123")."""
    out = []
    for m in _PHONE_RE.finditer(scan):
        s, e = m.span()
        if e > s:
            out.append((s, e))
    return out


def _spelled_spans(scan: str) -> list[tuple[int, int]]:
    return _iter_spans(_SPELLED_RE, scan)


def _num_token_value(token: str) -> int:
    """Digit-value of a mixed token: 1 per number-word, else digit count."""
    if token.lower() in _NUM_WORDS:
        return 1
    return len(token)


def _mixed_spans(scan: str) -> list[tuple[int, int]]:
    """Mixed word/digit spans that clear the digit-value floor (see _MIXED_RE)."""
    out = []
    for m in _MIXED_RE.finditer(scan):
        text = m.group(0)
        tokens = re.findall(r"[A-Za-z]+|\d+", text)
        if not any(t.lower() in _NUM_WORDS for t in tokens):
            continue
        if sum(_num_token_value(t) for t in tokens) < 7:
            continue
        s, e = m.span()
        if e > s:
            out.append((s, e))
    return out


def _handle_spans(scan: str) -> list[tuple[int, int]]:
    """Messaging-handle spans (keyword forms use their capture group)."""
    out = []
    for m in _HANDLE_KW_RE.finditer(scan):
        for g in (1, 2):
            s, e = m.span(g)
            if s >= 0 and e > s:
                out.append((s, e))
                break
    for m in _HANDLE_NAMEIS_RE.finditer(scan):
        s, e = m.span(1)
        if s >= 0 and e > s:
            out.append((s, e))
    out.extend(_iter_spans(_SKYPE_LIVE_RE, scan))
    out.extend(_iter_spans(_DISCORD_TAG_RE, scan))
    out.extend(_iter_spans(_BARE_AT_RE, scan))
    return out


# ---------------------------------------------------------------------------
# Span application
# ---------------------------------------------------------------------------


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping/adjacent detector spans (scan coordinates)."""
    merged: list[list[int]] = []
    for s, e in sorted(spans):
        if e <= s:
            continue
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def _extend_extensions(
    spans: list[tuple[int, int]], scan: str
) -> list[tuple[int, int]]:
    """Extend each contact span over a glued extension tail ("x123").

    An extension exists only to reach the number's owner; "415 555 0132
    x123" scrubbed to "[redacted] x123" still leaks the extension's
    digits. Runs on merged spans (any detector class), mirroring the
    old marker-adjacency semantics: it only fires directly adjacent to
    an actual contact span, so prose like "section 4.5 x2" can never
    match. Applied BEFORE merging with nothing else — extensions of two
    adjacent spans cannot chain past each other because each extension
    match must start with digits or ext/x.
    """
    out = []
    for s, e in spans:
        em = _EXT_RE.match(scan, e)
        if em and em.end() > e:
            e = em.end()
        out.append((s, e))
    return _merge_spans(out)


def _apply_spans(
    text: str, span_map: list[tuple[int, int]], spans: list[tuple[int, int]]
) -> str:
    """Replace scan ``spans`` with the marker, mapped back to ``text``.

    The original span — invisible characters and all — is replaced, and
    any invisible codepoints glued to its edges in the original are eaten
    too, so no zero-width residue survives beside the marker. Spans are
    applied from the end so earlier offsets stay valid. Linear-time.
    """
    if not spans or not span_map:
        return text
    orig: list[tuple[int, int]] = []
    for s, e in _merge_spans(spans):
        os_ = span_map[s][0]
        oe = span_map[e - 1][1]
        while os_ > 0 and _is_invisible(text[os_ - 1]):
            os_ -= 1
        while oe < len(text) and _is_invisible(text[oe]):
            oe += 1
        orig.append((os_, oe))
    result = text
    for os_, oe in sorted(orig, reverse=True):
        result = result[:os_] + SCRUB_MARKER + result[oe:]
    return result


#: Digit-lookalike shadow for the prose-obfuscation detectors: "0" is
#: read as "o" ("d0t" -> "dot") and "4" as "a" ("4t" -> "at"). A shadow
#: — not the fold table — because folding digits globally would corrupt
#: digit detection ("0" must stay a digit for phone spans); the shadow
#: only feeds the at/dot and obfuscated-dot detectors, where digits-as-
#: letters are the spoof and digits-as-digits never legitimately occur.
_AT_SHADOW = {ord("0"): "o", ord("4"): "a"}


def _scrub_contact_patterns(text: Any) -> Any:
    """Replace contact spans in ``text`` with the [redacted] marker.

    Normalize-then-detect: the text is projected onto a scan string
    (NFKD, invisible-stripped, homoglyph-folded); every detector runs
    on the scan; spans map back onto the original text. A field with
    bidi override characters is withheld wholesale — visual reordering
    cannot be span-detected honestly.
    """
    if not isinstance(text, str):
        return text
    if _has_bidi_override(text):
        return SCRUB_MARKER
    scan, span_map = _build_scan(text)
    if not scan:
        return text
    # "0" -> "o" shadow: catches zero-for-o homoglyph domains the fold
    # table must NOT contain (it would corrupt digit detection).
    scan2 = scan.replace("0", "o")
    # Digit-lookalike shadow for the prose-obfuscation detectors (same
    # length as scan, so span mapping is unaffected).
    scan3 = scan.translate(_AT_SHADOW)
    spans: list[tuple[int, int]] = []
    # The "@" pre-check keeps the email detector off the O(n^2) lazy-
    # quantifier path on @-less input ("a."*5000); "@" is unaffected by
    # the 0->o shadow, so one check covers both runs.
    if "@" in scan:
        spans.extend(_iter_spans(_EMAIL_RE, scan))
        spans.extend(_iter_spans(_EMAIL_RE, scan2))
    spans.extend(_phone_spans(scan))
    spans.extend(_url_spans(scan))
    spans.extend(_url_spans(scan2))
    spans.extend(_iter_spans(_IPV4_RE, scan))
    # "@"/"at" pre-check keeps the at/dot detector off its O(n*40)
    # local-part path on at-less input ("5"*200000); every alternative
    # in the detector's separator group ("@", "[at]", "(at)", "{at}",
    # word "at") contains "@" or the substring "at".
    if "@" in scan or "at" in scan.lower():
        spans.extend(_iter_spans(_ATDOT_RE, scan))
        spans.extend(_iter_spans(_ATDOT_RE, scan3))
    spans.extend(_iter_spans(_OBFDOT_RE, scan))
    spans.extend(_iter_spans(_OBFDOT_RE, scan3))
    spans.extend(_spelled_spans(scan))
    spans.extend(_mixed_spans(scan))
    spans.extend(_iter_spans(_VANITY_RE, scan))
    spans.extend(_handle_spans(scan))
    spans = _extend_extensions(_merge_spans(spans), scan)
    return _apply_spans(text, span_map, spans)


#: Maximum container-nesting depth _scrub_value will descend. Past
#: the cap it fails closed (returns the marker) instead of recursing —
#: a 3000-deep hand-built structure must not RecursionError the
#: preview path (DoS).
_SCRUB_MAX_DEPTH = 100


def _scrub_key(key: Any) -> Any:
    """Scrub a dict key while keeping it hashable.

    String keys are scrubbed like any text. Tuple keys are scrubbed
    element-wise and rebuilt as tuples. Anything that would come out
    unhashable (a tuple containing a dict, a frozenset, …) is left
    untouched: producing an unhashable key would TypeError the dict
    build — a crash the scrubber must never cause.
    """
    if isinstance(key, str):
        return _scrub_contact_patterns(key)
    if isinstance(key, tuple):
        scrubbed = tuple(_scrub_key(k) for k in key)
        try:
            hash(scrubbed)
        except TypeError:
            return key
        return scrubbed
    return key


def _optin_url_has_contact(url: str) -> bool:
    """Does the ``linkedin_public`` opt-in URL smuggle a contact payload?

    The opt-in covers the LinkedIn URL itself — not an email in its
    query string ("...?x=jane@example.com") or a contact in its path.
    Runs every detector EXCEPT the URL detectors themselves (the URL is
    allowed to look like a URL): email, phone, prose-obfuscated
    contacts, handles, vanity/spelled numbers, IPv4. A hit fails closed
    — the URL is replaced with the marker, not passed through.
    """
    scan, _ = _build_scan(url)
    if not scan:
        return False
    scan2 = scan.replace("0", "o")
    scan3 = scan.translate(_AT_SHADOW)
    spans: list[tuple[int, int]] = []
    if "@" in scan:
        spans.extend(_iter_spans(_EMAIL_RE, scan))
        spans.extend(_iter_spans(_EMAIL_RE, scan2))
    spans.extend(_phone_spans(scan))
    spans.extend(_iter_spans(_IPV4_RE, scan))
    spans.extend(_iter_spans(_ATDOT_RE, scan))
    spans.extend(_iter_spans(_ATDOT_RE, scan3))
    spans.extend(_iter_spans(_OBFDOT_RE, scan))
    spans.extend(_iter_spans(_OBFDOT_RE, scan3))
    spans.extend(_spelled_spans(scan))
    spans.extend(_mixed_spans(scan))
    spans.extend(_iter_spans(_VANITY_RE, scan))
    spans.extend(_handle_spans(scan))
    return any(e > s for s, e in spans)


def _scrub_value(value: Any, _depth: int = 0) -> Any:
    """Recursively scrub contact patterns from strings, even when nested
    inside dicts/lists/tuples/sets of a scrubbed field. Dict KEYS are
    scrubbed too — a contact hidden in a key ("jane@example.com": "x")
    is still a contact — but keys are always kept hashable (see
    _scrub_key): the scrubber never produces an unhashable key.
    Container TYPES are preserved (a tuple stays a tuple; a frozenset
    stays a frozenset): a set/frozenset member that would scrub to an
    unhashable value keeps its original member instead of crashing the
    set build. Bytes are decoded as UTF-8 and inspected: a bytes value
    carrying a contact pattern fails closed to the (bytes) marker;
    undecodable bytes pass through untouched. Past _SCRUB_MAX_DEPTH the
    value fails closed to the marker. Not reachable via validated
    ``mentor_opt_in`` today (defense in depth); the validated path only
    ever passes str / flat list values through here, so this recursion
    cannot change its output."""
    if _depth > _SCRUB_MAX_DEPTH:
        return SCRUB_MARKER
    if isinstance(value, str):
        return _scrub_contact_patterns(value)
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            return value
        if _scrub_contact_patterns(text) != text:
            # Contact pattern inside bytes: fail closed, type-preserving.
            return SCRUB_MARKER.encode("utf-8")
        return value
    if isinstance(value, dict):
        return {
            _scrub_key(k): _scrub_value(v, _depth + 1) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub_value(v, _depth + 1) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(v, _depth + 1) for v in value)
    if isinstance(value, (set, frozenset)):
        members = []
        for v in value:
            sv = _scrub_value(v, _depth + 1)
            try:
                hash(sv)
            except TypeError:
                sv = v  # never produce unhashable members; keep original
            members.append(sv)
        return frozenset(members) if isinstance(value, frozenset) else set(members)
    return value

# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def _get_mentor_card(mentor_id: str) -> dict[str, Any] | None:
    """Read-only lookup of a mentor card from the local directory."""
    import mentors as _mentors

    directory = _mentors._load_directory()  # noqa: SLF001 - same-project read
    card = directory.get(mentor_id)
    return dict(card) if card else None


def _party_ids_present(hs: dict[str, Any]) -> bool:
    """Both party ids are present and non-empty.

    A hand-edited store can null a party id; ``str(None) == "None"``
    would then sail through the block check as a literal party name.
    Every gate that consults party identity fails closed here first.
    """
    return bool(hs.get("mentee_id")) and bool(hs.get("mentor_id"))


def _pair_key(mentor_id: str, mentee_id: str) -> str:
    return f"{mentor_id}::{mentee_id}"


def _apply_expiry(
    store: dict[str, dict[str, Any]], now: datetime | None = None
) -> bool:
    """Move past-TTL ``awaiting_mentor`` handshakes to ``expired``.

    Called lazily on every read so the TTL is enforced even though no
    background sweeper exists in the local-first deployment. Returns True
    if anything changed (caller persists).

    Fail-closed on malformed records: a corrupt ``expires_at`` cannot
    prove the request is within its TTL, so the handshake is expired
    rather than left actionable — loudly (error log + audit entry).
    """
    moment = now or datetime.now(timezone.utc)
    changed = False
    for hs in store.values():
        if hs.get("state") != "awaiting_mentor":
            continue
        hid = hs.get("id", "<unknown>")
        exp = _parse_ts(hs.get("expires_at"))
        if exp is None:
            log.error(
                "CORRUPT handshake %s: expires_at=%r is not a valid timestamp; "
                "failing closed (expiring the request)",
                hid,
                hs.get("expires_at"),
            )
            hs["state"] = "expired"
            hs["updated_at"] = _now()
            changed = True
            _audit(
                "expire_corrupt",
                hid,
                "system",
                "expires_at malformed; failed closed to expired",
            )
        elif exp <= moment:
            hs["state"] = "expired"
            hs["updated_at"] = _now()
            changed = True
            _audit("expire", hs.get("id", "<unknown>"), "system", "request TTL elapsed")
    return changed


def _pending_for_pair(
    store: dict[str, dict[str, Any]], mentor_id: str, mentee_id: str
) -> dict[str, Any] | None:
    for hs in store.values():
        if (
            hs.get("mentor_id") == mentor_id
            and hs.get("mentee_id") == mentee_id
            and hs.get("state") == "awaiting_mentor"
        ):
            return hs
    return None


def _last_terminal_for_pair(
    store: dict[str, dict[str, Any]], mentor_id: str, mentee_id: str
) -> dict[str, Any] | None:
    cands = [
        hs
        for hs in store.values()
        if hs.get("mentor_id") == mentor_id
        and hs.get("mentee_id") == mentee_id
        and hs.get("state") in TERMINAL_STATES
    ]
    if not cands:
        return None
    # str(None) guard: a corrupt updated_at must not crash the comparison.
    return max(cands, key=lambda h: str(h.get("updated_at") or ""))


def _cooldown_armed(last: dict[str, Any], mentor_id: str) -> bool:
    """Does the pair's last terminal handshake arm the 30-day cooldown?

    A mentor decline arms it. So does a mentor withdrawal of a PENDING
    request (Contract s5): withdrawing a pending request is a refusal by
    another name, and letting the mentee re-request immediately would let
    a decline be laundered through a withdraw. A mentee's withdrawal of
    their own pending request does NOT arm it — that is the requester's
    own consent loop, not a refusal.
    """
    if last.get("state") == "declined":
        return True
    return (
        last.get("state") == "withdrawn"
        and last.get("withdrawn_by") == mentor_id
        and last.get("withdrawn_from") == "awaiting_mentor"
    )


# ---------------------------------------------------------------------------
# Mentee side
# ---------------------------------------------------------------------------


def request_introduction(
    mentor_id: str,
    mentee_id: str,
    mentee_label: str,
    goal: str,
    *,
    context: str = "",
    urgency: str = "exploring",
    expected_outcome: str = "",
    contact_path: str = "",
    synthetic: bool = False,
) -> dict[str, Any]:
    """Record the mentee's consent: request an introduction to a mentor.

    This creates the handshake in ``awaiting_mentor`` — it does NOT contact
    the mentor. The request itself is the mentee's explicit consent.

    Guardrails (all enforced, all returning ``{"ok": False}`` on breach):
      * unknown mentor, or mentor with no remaining capacity
      * either party has blocked the other (:mod:`initiatives.i07.safety`)
      * an ``awaiting_mentor`` handshake already exists for this pair
      * a decline (or a mentor withdrawal of a pending request) exists and
        the 30-day cooldown has not elapsed
      * more than MAX_REQUESTS_PER_DAY new requests in the last 24h
      * missing goal or mentee label (a request without a stated goal is
        not a consent-bearing request)

    A corrupt handshake store is refused outright (fail closed). Malformed
    timestamps fail closed too: they count as recent for the rate limit
    and keep the cooldown enforced, never as a way around it.
    """
    from . import safety

    if not str(mentor_id or "").strip():
        return {"ok": False, "error": "mentor_id is required"}
    if not str(mentee_id or "").strip():
        return {"ok": False, "error": "mentee_id is required"}
    if not str(mentee_label or "").strip():
        return {"ok": False, "error": "mentee_label is required (name or initials)"}
    if not str(goal or "").strip():
        return {"ok": False, "error": "goal is required: a request without a stated goal is not consent"}

    card = _get_mentor_card(mentor_id)
    if card is None:
        return {"ok": False, "error": f"unknown mentor id {mentor_id!r}"}
    import mentors as _mentors

    if _mentors.remaining_capacity(card) <= 0:
        return {"ok": False, "error": "mentor has no remaining capacity"}

    block = safety.consent_block_check(mentee_id, mentor_id)
    if block.get("blocked"):
        return {"ok": False, "error": block["error"]}

    store, err = _guarded_load()
    if err:
        return err
    assert store is not None
    _apply_expiry(store)  # TTL is enforced lazily; no background sweeper.
    _save(store)
    if _pending_for_pair(store, mentor_id, mentee_id):
        return {
            "ok": False,
            "error": "an introduction request to this mentor is already pending; withdraw it first to re-request",
        }
    last = _last_terminal_for_pair(store, mentor_id, mentee_id)
    if last and _cooldown_armed(last, mentor_id):
        hid = last.get("id", "<unknown>")
        dt = _parse_ts(last.get("updated_at"))
        if dt is None:
            # Fail closed: a corrupt timestamp must not silently lift the
            # anti-pestering cooldown.
            log.error(
                "CORRUPT handshake %s: updated_at=%r is not a valid timestamp; "
                "keeping the re-request cooldown enforced",
                hid,
                last.get("updated_at"),
            )
            return {
                "ok": False,
                "error": (
                    f"handshake {hid} has a corrupt timestamp; the re-request "
                    "cooldown stays enforced until the record is repaired"
                ),
            }
        if datetime.now(timezone.utc) - dt < DECLINE_COOLDOWN:
            return {
                "ok": False,
                "error": "this mentor declined (or withdrew the pending request) recently; the cooldown has not elapsed",
            }

    day_ago = datetime.now(timezone.utc) - timedelta(hours=24)
    recent = 0
    for hs in store.values():
        if hs.get("mentee_id") == mentee_id:
            created = _parse_ts(hs.get("created_at"))
            if created is None:
                # Fail closed: an unreadable timestamp counts as recent.
                log.error(
                    "CORRUPT handshake %s: created_at=%r is not a valid timestamp; "
                    "counting it toward the rate limit",
                    hs.get("id", "<unknown>"),
                    hs.get("created_at"),
                )
                recent += 1
            elif created >= day_ago:
                recent += 1
    if recent >= MAX_REQUESTS_PER_DAY:
        return {
            "ok": False,
            "error": f"rate limit: at most {MAX_REQUESTS_PER_DAY} new introduction requests per 24h",
        }

    now = _now()
    handshake_id = _new_id(synthetic=synthetic)
    hs = {
        "id": handshake_id,
        "mentor_id": mentor_id,
        "mentee_id": mentee_id,
        "mentee_label": str(mentee_label).strip(),
        "goal": str(goal).strip(),
        "context": str(context or "").strip(),
        "urgency": str(urgency or "exploring").strip(),
        "expected_outcome": str(expected_outcome or "").strip(),
        "mentee_contact_path": str(contact_path or "").strip(),
        "state": "awaiting_mentor",
        "mentee_consented_at": now,
        "mentor_consented_at": None,
        "mentor_decision_channel": None,
        "created_at": now,
        "updated_at": now,
        "expires_at": (datetime.now(timezone.utc) + REQUEST_TTL).isoformat(timespec="seconds"),
        "synthetic": bool(synthetic),
        "version": 1,
    }
    store[handshake_id] = hs
    _save(store)
    _audit("request", handshake_id, mentee_id, f"goal={hs['goal'][:80]}")
    return {
        "ok": True,
        "handshake": _public_view(hs, mentee_id),
        "sent": False,
        "note": (
            "Your consent is recorded. Nothing was sent to the mentor — "
            "deliver the request yourself (LinkedIn, email), then record "
            "their reply with mentor_respond."
        ),
    }


# ---------------------------------------------------------------------------
# Mentor side (recorded by the local user from the mentor's out-of-band reply)
# ---------------------------------------------------------------------------


def mentor_respond(
    handshake_id: str,
    decision: str,
    *,
    channel: str,
    note: str = "",
) -> dict[str, Any]:
    """Record the mentor's decision from their out-of-band reply.

    ``decision`` is "approve" or "decline". ``channel`` names HOW the
    mentor communicated it (e.g. "linkedin_dm", "email", "in_person") and
    is REQUIRED — consent is never recorded without provenance, and it is
    never fabricated: there is no code path that approves on the mentor's
    behalf without this explicit record.

    Approve -> state ``mutual``: both consents are now on record and
    contact details may be revealed (see :func:`reveal_contact`).
    Decline -> state ``declined``: starts the re-request cooldown.

    (The dead ``synthetic`` parameter that used to sit here was removed:
    nothing ever passed it and it did nothing. Synthetic handshakes are
    still labeled at creation in :func:`request_introduction`.)
    """
    if decision not in ("approve", "decline"):
        return {"ok": False, "error": "decision must be 'approve' or 'decline'"}
    if not str(channel or "").strip():
        return {
            "ok": False,
            "error": "channel is required: consent needs provenance (how the mentor replied)",
        }
    store, err = _guarded_load()
    if err:
        return err
    assert store is not None
    hs = store.get(handshake_id)
    if hs is None:
        return {"ok": False, "error": f"unknown handshake id {handshake_id!r}"}
    if _apply_expiry(store):
        _save(store)
        hs = store.get(handshake_id)
    if hs.get("state") != "awaiting_mentor":
        return {
            "ok": False,
            "error": f"handshake is {hs.get('state')!r}; only 'awaiting_mentor' can be answered",
        }
    # Safety (Contract s9/s11): a blocked or quarantined party cannot
    # answer a handshake. Recording "mutual" here would report a success
    # the safety seal immediately makes unfulfillable — reveal_contact
    # refuses on a blocked pair — so refuse BEFORE any mutation or
    # capacity consumption. Fail closed on unreadable safety state.
    # Fail closed on a null party id too: str(None) == "None" must never
    # pass the block check as a party name.
    from . import safety

    if not _party_ids_present(hs):
        _audit(
            "respond_refused",
            handshake_id,
            "system",
            "handshake record has a missing party id; failing closed",
        )
        return {
            "ok": False,
            "error": "handshake record is missing a party id; refusing to operate",
        }

    block = safety.consent_block_check(hs.get("mentee_id"), hs.get("mentor_id"))
    if block.get("blocked"):
        _audit(
            "respond_refused",
            handshake_id,
            hs.get("mentor_id", ""),
            f"blocked: {block.get('error')}",
        )
        return {"ok": False, "error": block.get("error")}
    now = _now()
    hs["mentor_decision_channel"] = str(channel).strip()
    hs["mentor_decision_note"] = str(note or "").strip()
    hs["updated_at"] = now
    if decision == "approve":
        # Re-check capacity at approve time: the directory may have filled
        # while the request was pending. Approving consumes one spot so the
        # directory stays honest (mirrors record_outreach on the cold path).
        import mentors as _mentors

        card = _get_mentor_card(hs["mentor_id"])
        if card is None or _mentors.remaining_capacity(card) <= 0:
            hs["state"] = "expired"
            hs["updated_at"] = now
            _save(store)
            _audit("approve_refused", handshake_id, hs["mentor_id"], "no remaining capacity")
            return {"ok": False, "error": "mentor has no remaining capacity; request closed"}
        directory = _mentors._load_directory()  # noqa: SLF001
        live = directory.get(hs["mentor_id"])
        if live is not None:
            live["mentee_count"] = int(live.get("mentee_count", 0)) + 1
            _mentors._save_directory(directory)  # noqa: SLF001
        hs["state"] = "mutual"
        hs["mentor_consented_at"] = now
        _audit("mentor_approve", handshake_id, hs["mentor_id"], f"channel={channel}")
    else:
        hs["state"] = "declined"
        _audit("mentor_decline", handshake_id, hs["mentor_id"], f"channel={channel}")
    _save(store)
    return {"ok": True, "handshake": _public_view(hs, hs["mentee_id"])}


# ---------------------------------------------------------------------------
# Withdrawal — either party, any live stage
# ---------------------------------------------------------------------------


def withdraw(handshake_id: str, actor: str, *, reason: str = "") -> dict[str, Any]:
    """Withdraw from a handshake. Either party, at any live stage.

    The handshake must still be live (``awaiting_mentor`` or ``mutual``).
    Terminal outcomes (``declined``, ``expired``) cannot be withdrawn from
    — the handshake already ended, so there is nothing left to withdraw —
    and a second withdraw of an already-``withdrawn`` handshake is
    refused. (Contract s4 states this explicitly; the code enforces it.)

    Withdrawal is immediate and final: the handshake moves to
    ``withdrawn`` and sealed contact details can no longer be revealed
    through this handshake (see :func:`reveal_contact`). Withdrawing after
    mutual consent cannot un-see already-shared details — the audit log
    records the revocation so both sides have a receipt. A mentor's
    withdrawal of a PENDING request arms the 30-day re-request cooldown
    for the pair (Contract s5).

    ``actor`` is normally one of the two parties. The reserved actor
    ``"system"`` is accepted for safety automation (block/quarantine);
    such withdrawals are audited as system actions with the reason given.
    """
    store, err = _guarded_load()
    if err:
        return err
    assert store is not None
    hs = store.get(handshake_id)
    if hs is None:
        return {"ok": False, "error": f"unknown handshake id {handshake_id!r}"}
    if actor != "system" and actor not in (hs.get("mentee_id"), hs.get("mentor_id")):
        return {"ok": False, "error": "actor is not a party to this handshake"}
    if hs.get("state") == "withdrawn":
        return {"ok": False, "error": "handshake is already withdrawn"}
    if hs.get("state") in ("declined", "expired"):
        return {
            "ok": False,
            "error": f"handshake already ended as {hs.get('state')!r}; nothing to withdraw",
        }
    was_state = hs.get("state")
    was_mutual = was_state == "mutual"
    hs["state"] = "withdrawn"
    hs["withdrawn_by"] = actor
    hs["withdrawn_from"] = was_state
    hs["withdraw_reason"] = str(reason or "").strip()
    hs["updated_at"] = _now()
    _save(store)
    _audit(
        "withdraw",
        handshake_id,
        actor,
        f"was_mutual={was_mutual}"
        + (f" reason={reason[:80]}" if reason else "")
        + (" cooldown_armed=1" if _cooldown_armed(hs, hs.get("mentor_id", "")) else ""),
    )
    result: dict[str, Any] = {"ok": True, "handshake": _public_view(hs, actor)}
    if was_mutual:
        # Withdrawing after mutual consent seals this handshake's contact
        # details and ends session access: notes, agenda, and other
        # session-kit reads for this handshake will now refuse, for both
        # parties. Say so at confirm time — already-shared details cannot
        # be un-seen, and the audit log records the revocation as receipt.
        result["note"] = (
            "Withdrawing after mutual consent seals this handshake's contact "
            "details and ends session access: notes, agenda, and other "
            "session-kit reads for this handshake will now refuse, for both "
            "parties. Already-shared details cannot be un-seen."
        )
    return result


# ---------------------------------------------------------------------------
# Revelation — sealed until mutual
# ---------------------------------------------------------------------------


def reveal_contact(handshake_id: str, actor: str) -> dict[str, Any]:
    """Reveal contact details to a party — ONLY in ``mutual`` state.

    Returns the mentor's contact detail to the mentee and the mentee's
    chosen contact path to the mentor. Any other state (including
    ``withdrawn`` after a mutual) refuses. Block status is re-checked at
    reveal time.
    """
    from . import safety

    store, err = _guarded_load()
    if err:
        return err
    assert store is not None
    hs = store.get(handshake_id)
    if hs is None:
        return {"ok": False, "error": f"unknown handshake id {handshake_id!r}"}
    if actor not in (hs.get("mentee_id"), hs.get("mentor_id")):
        return {"ok": False, "error": "actor is not a party to this handshake"}
    if hs.get("state") != "mutual":
        return {
            "ok": False,
            "error": (
                "contact details are sealed: this handshake is "
                f"{hs.get('state')!r}, not 'mutual'. Both parties must approve first."
            ),
        }
    # Same fail-closed party-id gate as mentor_respond: a null party id
    # must never reach the block check as the string "None".
    if not _party_ids_present(hs):
        return {
            "ok": False,
            "error": "handshake record is missing a party id; refusing to operate",
        }
    block = safety.consent_block_check(hs["mentee_id"], hs["mentor_id"])
    if block.get("blocked"):
        return {"ok": False, "error": block["error"]}

    card = _get_mentor_card(hs["mentor_id"]) or {}
    mentor_contact = {f: card.get(f) for f in CONTACT_FIELDS if card.get(f)}
    mentee_contact = {"mentee_contact_path": hs.get("mentee_contact_path") or "(not provided)"}
    _audit("reveal", handshake_id, actor, "contact details revealed post-mutual-consent")
    if actor == hs["mentee_id"]:
        return {"ok": True, "mentor_contact": mentor_contact}
    return {"ok": True, "mentee_contact": mentee_contact}


def redacted_preview(card: dict[str, Any]) -> dict[str, Any]:
    """Pre-consent view of a mentor card: discovery fields only, no contact.

    DEFAULT-DENY: only allowlisted fields are emitted, and every field
    goes through a safe-value policy before it reaches the output —
    unknown card keys are dropped, never passed through.

    * ``id``: only a hash-shaped opaque identifier (``[A-Za-z0-9_-]``,
      1-64 chars) passes, by construction.
    * ``seniority`` / ``topics``: closed vocabularies — only members of
      the directory's seniority bands / TOPICS enum pass, by
      construction. A hostile non-vocabulary entry is dropped, not
      scrubbed.
    * ``remaining_capacity`` / ``score``: only real numbers pass; a
      contact STRING smuggled into one becomes the ``[redacted]``
      marker (fail closed).
    * Free-text fields (``name``, ``industry``, ``role``, ``bio``,
      ``availability``, ``reasons``, ``rating_summary``): scrubbed for
      contact patterns — email addresses, phone numbers (every shape:
      NANP 10-digit, 7-digit local, +international, 00/011 IDD, bare
      country code, parens, contiguous 7-15, separated groups,
      extension tails, spelled-out digits, vanity numbers, (0) trunk),
      URLs (schemed, hxxp, www., bare domains on any TLD, obfuscated
      dots, IPv4), prose-obfuscated contacts ("jane at example dot
      com"), and messaging handles — via normalize-then-detect (NFKD,
      invisible-stripped, homoglyph-folded; see the section header
      above). Detected spans are replaced with the ``[redacted]``
      marker.

    The deliberate ``preferred_contact="linkedin_public"`` opt-in is the
    only exception — and only when the URL actually looks like a
    LinkedIn URL (fail closed on a hand-edited store) and carries no
    contact payload itself: an email in its query string or path fails
    closed to the marker rather than passing through. A URL containing
    bidi override controls (U+202A–U+202E) fails closed to the marker
    too — overrides reorder what the reader SEES, so no logical-order
    check can see the leak (Contract §3).
    """
    import mentors as _mentors  # lazy: same-project read, mirrors _get_mentor_card

    topics_vocab = set(getattr(_mentors, "TOPICS", ()) or ())
    bands = set(getattr(_mentors, "_VALID_BANDS", ()) or ())
    linkedin_re = getattr(_mentors, "_LINKEDIN_RE", None)

    preview: dict[str, Any] = {}
    # id: hash-shaped opaque identifier, safe by construction.
    cid = card.get("id")
    if isinstance(cid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cid):
        preview["id"] = cid
    # Free-text fields: scrubbed for contact patterns.
    for field in (
        "name",
        "industry",
        "role",
        "bio",
        "availability",
        "reasons",
        "rating_summary",
    ):
        if field in card:
            preview[field] = _scrub_value(card[field])
    # Closed vocabularies: only vocabulary members pass, by construction.
    seniority = card.get("seniority")
    if isinstance(seniority, str) and seniority in bands:
        preview["seniority"] = seniority
    if "topics" in card:
        topics = card.get("topics")
        if isinstance(topics, (list, tuple)):
            preview["topics"] = [
                t for t in topics if isinstance(t, str) and t in topics_vocab
            ]
        elif isinstance(topics, str):
            # Malformed (non-list) topics: scrub as free text — still
            # safe, and preserves the long-standing scrub contract.
            preview["topics"] = _scrub_contact_patterns(topics)
        # Any other type is dropped (fail closed).
    # Numeric fields: real numbers pass; contact strings become the marker.
    for field in ("remaining_capacity", "score"):
        if field in card:
            value = card[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                preview[field] = SCRUB_MARKER if isinstance(value, str) else value
            else:
                preview[field] = value
    # Deliberate opt-in: public LinkedIn URL — but only when it
    # actually looks like a LinkedIn URL (fail closed on a hand-edited
    # store), AND only when the URL itself carries no contact payload:
    # an email in the query string or path ("...?x=jane@example.com")
    # is a contact leak wearing a LinkedIn costume, so the URL fails
    # closed to the marker instead of passing through untouched.
    if card.get("preferred_contact") == "linkedin_public":
        url = card.get("linkedin_url")
        if isinstance(url, str) and url:
            if _has_bidi_override(url):
                # Bidi OVERRIDE controls reorder the VISUAL text: the
                # reader may see a contact payload the logical-order
                # checks cannot detect, so withhold the field wholesale
                # (fail closed, CONTRACTS.md §3) — before the LinkedIn
                # prefix match and the contact-payload checks, same as
                # _scrub_contact_patterns does for free text.
                preview["linkedin_url"] = SCRUB_MARKER
            elif linkedin_re is None or linkedin_re.match(url.strip()):
                preview["linkedin_url"] = (
                    SCRUB_MARKER if _optin_url_has_contact(url) else url
                )
    return preview


# ---------------------------------------------------------------------------
# Views, listing, expiry
# ---------------------------------------------------------------------------


def _public_view(hs: dict[str, Any], actor: str) -> dict[str, Any]:
    """Handshake as seen by ``actor``: full detail for parties, sealed contact."""
    view = dict(hs)
    mentee_id = hs.get("mentee_id")
    # Never leak the other party's raw contact path beyond what consent
    # allows. The mentee may always review the contact path THEY entered;
    # everyone else sees it only after mutual consent.
    if actor != mentee_id and (
        hs.get("state") != "mutual" or actor not in (mentee_id, hs.get("mentor_id"))
    ):
        view.pop("mentee_contact_path", None)
    # A mutual handshake under a block/quarantine still DISPLAYS
    # "mutual", but reveal_contact and every session-kit read refuse
    # while the block stands — say so on the view instead of implying a
    # working connection. (Informational only: reveal_contact enforces
    # the seal itself, so a failed annotation can never leak.)
    if view.get("state") == "mutual":
        try:
            from . import safety

            block = safety.consent_block_check(mentee_id, hs.get("mentor_id"))
        except Exception:  # listing must never crash; the seal still holds
            block = {}
        if isinstance(block, dict) and block.get("blocked"):
            view["safety_hold"] = True
            view["safety_hold_note"] = (
                "contact reveal and session reads are currently refused: "
                + str(block.get("error") or "a block exists between these parties")
            )
    return view


def get_handshake(handshake_id: str, actor: str) -> dict[str, Any]:
    store, err = _guarded_load()
    if err:
        return err
    assert store is not None
    if _apply_expiry(store):
        _save(store)
    hs = store.get(handshake_id)
    if hs is None:
        return {"ok": False, "error": f"unknown handshake id {handshake_id!r}"}
    if actor not in (hs.get("mentee_id"), hs.get("mentor_id"), "owner"):
        return {"ok": False, "error": "actor is not a party to this handshake"}
    return {"ok": True, "handshake": _public_view(hs, actor)}


def list_handshakes(
    actor: str | None = None, state: str | None = None
) -> list[dict[str, Any]]:
    """List handshakes, optionally filtered by party and/or state.

    Raises :class:`CorruptStoreError` on a corrupt store (fail closed,
    loudly) — a listing must not silently show "nothing" when the store
    is unreadable.
    """
    store = _load()  # raises CorruptStoreError; documented, deliberate.
    if _apply_expiry(store):
        _save(store)
    out = []
    for hs in store.values():
        if actor and actor not in (hs.get("mentee_id"), hs.get("mentor_id")):
            continue
        if state and hs.get("state") != state:
            continue
        out.append(_public_view(hs, actor or "owner"))
    out.sort(key=lambda h: str(h.get("updated_at") or ""), reverse=True)
    return out


def expire_stale(now: datetime | None = None) -> dict[str, Any]:
    """Move ``awaiting_mentor`` handshakes past their TTL to ``expired``.

    Explicit sweep entry point; the same enforcement also runs lazily on
    every read (see :func:`_apply_expiry`), so the TTL holds with no
    background process. ``now`` is injectable for tests. Malformed
    records fail closed (expired) and are logged loudly — never crash.
    """
    store, err = _guarded_load()
    if err:
        return err
    assert store is not None
    before = {
        hid for hid, hs in store.items() if hs.get("state") == "awaiting_mentor"
    }
    changed = _apply_expiry(store, now=now)
    expired = [
        hid for hid in before if store.get(hid, {}).get("state") != "awaiting_mentor"
    ]
    if changed:
        _save(store)
    return {"ok": True, "expired": expired}


def audit_trail(handshake_id: str | None = None) -> list[dict[str, Any]]:
    """Read the append-only audit trail, optionally for one handshake.

    This is a reader, not a verifier: unparseable lines are skipped here;
    use :func:`verify_audit` to detect tampering.
    """
    entries: list[dict[str, Any]] = []
    try:
        with AUDIT_FILE.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if handshake_id and entry.get("handshake_id") != handshake_id:
                    continue
                entries.append(entry)
    except FileNotFoundError:
        pass
    return entries

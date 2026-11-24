"""Overwrite and extend data/ipa.json (and data/dict.txt) with Wiktionary readings.

Sources (wiktextract JSONL as published by kaikki.org, plain or .gz):

  --en  English Wiktionary, Thai entries:
        https://kaikki.org/dictionary/Thai/kaikki.org-dictionary-Thai.jsonl.gz
  --th  Thai Wiktionary, full extract (only lang_code == "th" entries are used):
        https://kaikki.org/thwiktionary/raw-wiktextract-data.jsonl.gz

Both editions render IPA from the same {{th-pron}} module, one IPA per accepted reading
(สัปดาห์ -> /sap̚˨˩.daː˧/ and /sap̚˨˩.pa˨˩.daː˧/), so the IPA is taken as-is. A reading is used only if:

  - it has no tags or notes (tagged IPA is Chinese-dialect etymology, "theoretical" readings, ...);
  - it matches THAI_PHONOTACTICS regex (see below). This also drops bound forms used only at
    the start of compounds (/ʔaː˧./, trailing dot) and narrow phonetic transcriptions ([mäː˩˥...]).

For each word with at least one valid reading:

  - in data/sources/manual_overrides.json: left alone (manual overrides have the highest priority);
  - already in ipa.json and its current reading is one of Wiktionary's: left alone (ipa.json holds one reading,
    and that one already matches Wiktionary's);
  - already in ipa.json with another reading: overwritten;
  - not in ipa.json: added to ipa.json and to dict.txt (G2P only looks up dict.txt tokens, so an ipa.json entry
    without a dict.txt word is never used). New entries are appended at the end of both files. New words shorter
    than --min-new-length characters (default 3) are skipped: most are bare letters and undotted abbreviations
    (ก, จ, กจ, ง.), and as tokenizer words they cut up other text ("ตนเอง..." -> ตน|เอ|ง.|.., "ซวง" -> ซ|วง).
    New words that are compositional are skipped too (--keep-compositional adds them): every way of splitting the
    word into the fewest dictionary words gives the same reading as Wiktionary's (การสอบ = การ + สอบ). They add no
    reading, but as tokenizer words they make newmm pick other splits of the text around them, which lose linking
    syllables: กรรมการ|สอบ -> กรรม|การสอบ (/kam.ma.kaːn/ -> /kam.kaːn/), ราชการ|ไป -> ราช|การไป. Splits are made from
    the existing words (with their overwritten readings) and the new words already kept, shortest words first.
    Readings are compared without final /ʔ/, which Wiktionary writes only at the end of a word.

When a word has several readings, the one listed most often across entries and both editions is chosen (เพลา:
/pʰlaw˧/ in three entries, /pʰeː˧.laː˧/ in one); ties go to the first one seen (English edition first).

Usage (from the repo root):

  python scripts/update_ipa_from_wiktionary.py --en EN.jsonl.gz --th TH.jsonl.gz --report changes.tsv
  python scripts/update_ipa_from_wiktionary.py ... --dry-run     # report only, files untouched
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
IPA_PATH = DATA_DIR / "ipa.json"
DICT_PATH = DATA_DIR / "dict.txt"
MANUAL_PATH = DATA_DIR / "sources" / "manual_overrides.json"

# --- Thai phonotactics ----------------------------------------------------------

THAI_ONSETS_1 = sorted({
    "m",  "n",         "ŋ",
    "b",  "d",
    "p",  "t",  "t͡ɕ",  "k", "ʔ",
    "pʰ", "tʰ", "t͡ɕʰ", "kʰ",
    "f",  "s",               "h",
    "w",  "l",  "j",
          "r",
}, key=len, reverse=True)

THAI_ONSETS_2 = sorted({
    "r", "l", "w"
}, key=len, reverse=True)

THAI_VOWELS = sorted({
    "i", "ɯ", "u",
    "iː", "ɯː", "uː",
    "e", "ɤ", "o",
    "eː", "ɤː", "oː",
    "ɛ", "a", "ɔ",
    "ɛː", "aː", "ɔː",
    "ia̯", "ɯa̯", "ua̯",
}, key=len, reverse=True)

THAI_CODAS = sorted({
    "m",  "n",       "ŋ",
    "p̚", "t̚",      "k̚", "ʔ",
    "w",        "j",
    # Only in foreign loanwords.
    "f", "s", "l", "s̚", "t͡ɕʰ",
    # A final consonant followed by /s/: ควินซ์ /kʰwins˦˥/, โรดส์ /roːt̚s˦˥/, ไซเบอร์เนติกส์ /...tik̚s˨˩/
    *(coda + "s" for coda in ("m", "n", "ŋ", "p̚", "t̚", "k̚", "w", "j", "l")),
}, key=len, reverse=True)

THAI_TONES = sorted({
    "˧",    # สามัญ
    "˨˩",   # เอก
    "˥˩",   # โท
    "˦˥",   # ตรี
    "˩˩˦",  # จัตวา
}, key=len, reverse=True)

THAI_SYLLABLE_PATTERN = rf"({'|'.join(THAI_ONSETS_1)})({'|'.join(THAI_ONSETS_2)})?({'|'.join(THAI_VOWELS)})({'|'.join(THAI_CODAS)})?({'|'.join(THAI_TONES)})"
THAI_PHONOTACTICS = re.compile(rf"/{THAI_SYLLABLE_PATTERN}(\.{THAI_SYLLABLE_PATTERN})*/")

# A headword we can put in dict.txt: Thai script, plus the spaces and dots dict.txt already uses
# ("ก ข ไม่กระดิกหู", "ก.ต.", "อด ๆ อยาก ๆ").
THAI_WORD = re.compile(r"[ก-๛][ก-๛ .]*")


@dataclass
class Readings:
    counts: Counter = field(default_factory=Counter)  # valid IPA -> times listed
    order: list[str] = field(default_factory=list)    # valid IPA, first-seen order
    sources: set[str] = field(default_factory=set)
    rejected: list[str] = field(default_factory=list)  # IPA failing THAI_PHONOTACTICS

    def add(self, ipa: str, source: str) -> None:
        if not THAI_PHONOTACTICS.fullmatch(ipa):
            if ipa not in self.rejected:
                self.rejected.append(ipa)
            return
        if ipa not in self.counts:
            self.order.append(ipa)
        self.counts[ipa] += 1
        self.sources.add(source)

    def best(self) -> str:
        top = max(self.counts.values())
        return next(ipa for ipa in self.order if self.counts[ipa] == top)


_FINAL_GLOTTAL = re.compile(r"ʔ(?=[˥˦˧˨˩])")
_MAX_SPLIT_READINGS = 16


def comparable(ipa: str) -> str:
    return _FINAL_GLOTTAL.sub("", ipa.strip("/"))


def split_readings(word: str, lookup: dict[str, str]) -> set[str] | None:
    """Readings of word split into the fewest (at least two) words of lookup; None if it cannot be split.

    Spaces are free pieces with no reading, as G2P skips whitespace tokens. More than _MAX_SPLIT_READINGS different
    readings count as unsplittable (the word is then kept).
    """
    # best[i]: (pieces, readings) of the fewest-pieces splits of word[:i].
    best: list[tuple[int, set[str]] | None] = [None] * (len(word) + 1)
    best[0] = (0, {""})
    for end in range(1, len(word) + 1):
        for start in range(end):
            if best[start] is None or (start == 0 and end == len(word)):
                continue
            piece = word[start:end]
            if piece.isspace():
                pieces, reading = 0, ""
            elif piece in lookup:
                pieces, reading = 1, comparable(lookup[piece])
            else:
                continue
            count = best[start][0] + pieces
            readings = {".".join(filter(None, (r, reading))) for r in best[start][1]}
            if best[end] is None or count < best[end][0]:
                best[end] = (count, readings)
            elif count == best[end][0]:
                best[end][1].update(readings)
        if best[end] is not None and len(best[end][1]) > _MAX_SPLIT_READINGS:
            return None
    return best[-1][1] if best[-1] is not None and best[-1][0] >= 2 else None


def read_jsonl(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as lines:
        for line in lines:
            yield json.loads(line)


def collect(sources: list[tuple[str, Path]]) -> dict[str, Readings]:
    readings: dict[str, Readings] = {}
    for name, path in sources:
        for entry in read_jsonl(path):
            if entry.get("lang_code") != "th":
                continue
            word = entry.get("word", "").strip()
            if not THAI_WORD.fullmatch(word):
                continue
            for sound in entry.get("sounds", []):
                ipa = sound.get("ipa")
                if not ipa or sound.get("tags") or sound.get("raw_tags") or sound.get("note"):
                    continue
                readings.setdefault(word, Readings()).add(ipa.strip(), name)
    return readings


def write_ipa(path: Path, ipa: dict[str, str]) -> None:
    """Same layout as the current ipa.json: one entry per line, no trailing newline."""
    lines = [json.dumps(word, ensure_ascii=False) + ": " + json.dumps(value, ensure_ascii=False)
             for word, value in ipa.items()]
    write_atomic(path, "{\n" + ",\n".join(lines) + "\n}")


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--en", type=Path, help="kaikki.org English-Wiktionary Thai JSONL(.gz)")
    parser.add_argument("--th", type=Path, help="kaikki.org Thai-Wiktionary raw JSONL(.gz)")
    parser.add_argument("--report", type=Path, help="write a TSV of every overwritten, new and rejected word")
    parser.add_argument("--dry-run", action="store_true", help="do not modify ipa.json or dict.txt")
    parser.add_argument("--no-new", action="store_true", help="only overwrite existing words, add none")
    parser.add_argument("--min-new-length", type=int, default=3, metavar="N",
                        help="skip new words shorter than N characters (default: 3; 1 adds all)")
    parser.add_argument("--keep-compositional", action="store_true",
                        help="also add new words whose reading is that of the dictionary words they split into")
    args = parser.parse_args()

    sources = [(name, path) for name, path in (("en", args.en), ("th", args.th)) if path]
    if not sources:
        parser.error("give --en and/or --th")

    ipa: dict[str, str] = json.loads(IPA_PATH.read_text("utf-8"))
    dict_text = DICT_PATH.read_text("utf-8")
    dict_words = dict_text.splitlines()
    dict_set = set(dict_words)
    manual = json.loads(MANUAL_PATH.read_text("utf-8")) if MANUAL_PATH.exists() else {}

    readings = collect(sources)

    stats = Counter()
    rows = []  # (word, action, old, new, all readings or split, sources)
    candidates = []  # new words: (word, new, listed, sources_used)
    for word, found in readings.items():
        old = ipa.get(word, "")
        listed = " ".join(found.order)
        if not found.counts:
            stats["rejected (no reading matches the phonotactics regex)"] += 1
            rows.append((word, "rejected", old, "", " ".join(found.rejected), ""))
            continue
        if word in manual:
            stats["kept: manual override"] += 1
            continue
        if old in found.counts:
            stats["kept: current reading is one of Wiktionary's"] += 1
            continue
        new = found.best()
        sources_used = ",".join(sorted(found.sources))
        if old:
            stats["overwritten"] += 1
            rows.append((word, "overwritten", old, new, listed, sources_used))
            ipa[word] = new
        elif args.no_new:
            stats["skipped new: --no-new"] += 1
        elif len(word) < args.min_new_length:
            stats[f"skipped new: shorter than {args.min_new_length} characters"] += 1
            rows.append((word, "skipped-short", "", new, listed, sources_used))
        else:
            candidates.append((word, new, listed, sources_used))

    # New words, after all overwrites; shortest first, so the words a compound splits into are already decided.
    lookup = dict(ipa)
    kept = set()
    for word, new, listed, sources_used in sorted(candidates, key=lambda c: len(c[0])):
        if not args.keep_compositional and split_readings(word, lookup) == {comparable(new)}:
            stats["skipped new: compositional (reads as the dictionary words it splits into)"] += 1
            rows.append((word, "skipped-compositional", "", new, listed, sources_used))
            continue
        lookup[word] = new
        kept.add(word)
    for word, new, listed, sources_used in candidates:  # appended in Wiktionary order
        if word not in kept:
            continue
        stats["new"] += 1
        rows.append((word, "new", "", new, listed, sources_used))
        ipa[word] = new
        if word not in dict_set:
            dict_words.append(word)
            dict_set.add(word)
            stats["new: also added to dict.txt"] += 1

    print(f"Wiktionary words: {len(readings):,} ({', '.join(f'{n}={p}' for n, p in sources)})")
    for key, value in sorted(stats.items()):
        print(f"  {key}: {value:,}")

    # Every reading this script writes passes the regex (rejected words keep their old reading, if any).
    assert all(THAI_PHONOTACTICS.fullmatch(ipa[word]) for word, action, *_ in rows if action in ("overwritten", "new"))
    # G2P keys must stay in dict.txt.
    assert set(ipa) <= dict_set

    if args.report:
        with open(args.report, "w", encoding="utf-8", newline="") as out:
            writer = csv.writer(out, delimiter="\t", lineterminator="\n")
            writer.writerow(["word", "action", "old_ipa", "new_ipa", "wiktionary_readings", "sources"])
            writer.writerows(sorted(rows, key=lambda r: (r[1], r[0])))
        print(f"Report: {args.report} ({len(rows):,} rows)")

    if args.dry_run:
        print("Dry run: ipa.json and dict.txt not modified.")
        return
    write_ipa(IPA_PATH, ipa)
    trailing = "\n" if dict_text.endswith("\n") else ""
    write_atomic(DICT_PATH, "\n".join(dict_words) + trailing)
    print(f"Written: {IPA_PATH} ({len(ipa):,} entries), {DICT_PATH} ({len(dict_words):,} words)")


if __name__ == "__main__":
    main()

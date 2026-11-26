"""Extract Thai IPA readings from Wiktionary into data/sources/wiktionary_ipa.json.

Sources (wiktextract JSONL as published by kaikki.org, plain or .gz):

  --en  English Wiktionary, Thai entries (default: data/sources/kaikki.org-dictionary-Thai.jsonl.gz):
        https://kaikki.org/dictionary/Thai/kaikki.org-dictionary-Thai.jsonl.gz
  --th  Thai Wiktionary, full extract; only lang_code == "th" entries are used
        (default: data/sources/kaikki.org-thwiktionary-raw.jsonl.gz):
        https://kaikki.org/thwiktionary/raw-wiktextract-data.jsonl.gz

Both editions render IPA from the same {{th-pron}} module, one IPA per accepted reading
(สัปดาห์ -> /sap̚˨˩.daː˧/ and /sap̚˨˩.pa˨˩.daː˧/), so the IPA is taken as-is. A reading is kept only if:

  - it has no tags or notes (tagged IPA is Chinese-dialect etymology, "theoretical" readings, ...);
  - it matches the THAI_PHONOTACTICS regex (see below). This drops narrow phonetic transcriptions ([mäː˩˥...]) and
    readings outside the Thai sound system, such as English /l/ codas.

Bound forms used only at the start of compounds (กุมภ /kum˧.pʰa˦˥./, trailing dot) are kept with their dot, after the
word's full readings: merge_ipa.py uses one (without the dot) only for a dict.txt word that has no other reading.

Output: {word: [ipa, ...]} for every Thai headword with untagged IPA, in first-seen order of the words (English edition
first); [] when every reading was rejected (the word then gets a reading only from a manual override). A word's
readings are ordered by preference: full readings listed most often across entries and both editions first (เพลา:
/pʰlaw˧/ in three entries, /pʰeː˧.laː˧/ in one; ties go to the first one seen), then bound forms. Which words and readings end up in ipa.json and dict.txt is decided by merge_ipa.py.

Usage (from the repo root):

  python scripts/extract_wiktionary_ipa.py                             # both dumps at their default paths
  python scripts/extract_wiktionary_ipa.py --en EN.jsonl.gz --th TH.jsonl.gz
  python scripts/extract_wiktionary_ipa.py ... --report rejected.tsv   # also list every rejected reading
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
KAIKKI_PATH = DATA_DIR / "sources" / "kaikki.org-dictionary-Thai.jsonl.gz"
TH_WIKTIONARY_PATH = DATA_DIR / "sources" / "kaikki.org-thwiktionary-raw.jsonl.gz"
OUTPUT_PATH = DATA_DIR / "sources" / "wiktionary_ipa.json"

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
    # Only in foreign loanwords. No /l/: Thai speakers say a final English /l/ as /w/ or /n/ (เซลล์ /sew˧/,
    # บิลเลียด /bin˧.lia̯t̚˥˩/), so Wiktionary's /l/ codas are rejected here and corrected in the manual overrides.
    "f", "s", "s̚", "t͡ɕʰ",
    # A final consonant followed by /s/: ควินซ์ /kʰwins˦˥/, โรดส์ /roːt̚s˦˥/, ไซเบอร์เนติกส์ /...tik̚s˨˩/
    *(coda + "s" for coda in ("m", "n", "ŋ", "p̚", "t̚", "k̚", "w", "j")),
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

# A Thai headword: Thai script, plus the spaces, dots and slashes dict.txt already uses
# ("ก ข ไม่กระดิกหู", "ก.ต.", "อด ๆ อยาก ๆ", "บ/ช"). merge_ipa.py adds new words to dict.txt only without slashes.
THAI_HEADWORD = re.compile(r"[ก-๛][ก-๛ ./]*")

BOUND_SUFFIX = "./"


def is_bound_form(ipa: str) -> bool:
    return ipa.endswith(BOUND_SUFFIX)


def full_readings(listed: list[str]) -> list[str]:
    """A wiktionary_ipa.json entry without its bound forms, preferred reading first."""
    return [ipa for ipa in listed if not is_bound_form(ipa)]


def unbound(ipa: str) -> str:
    """กุมภ /kum˧.pʰa˦˥./ -> /kum˧.pʰa˦˥/"""
    return ipa[: -len(BOUND_SUFFIX)] + "/" if is_bound_form(ipa) else ipa


@dataclass
class Readings:
    counts: Counter = field(default_factory=Counter)  # valid full IPA -> times listed
    order: list[str] = field(default_factory=list)    # valid full IPA, first-seen order
    bound: list[str] = field(default_factory=list)    # valid bound forms (with their dot), first-seen order
    rejected: list[str] = field(default_factory=list)  # IPA failing THAI_PHONOTACTICS

    def add(self, ipa: str) -> None:
        if THAI_PHONOTACTICS.fullmatch(ipa):
            if ipa not in self.counts:
                self.order.append(ipa)
            self.counts[ipa] += 1
        elif is_bound_form(ipa) and THAI_PHONOTACTICS.fullmatch(unbound(ipa)):
            if ipa not in self.bound:
                self.bound.append(ipa)
        elif ipa not in self.rejected:
            self.rejected.append(ipa)

    def preferred(self) -> list[str]:
        """Full readings, most often listed first (ties: first seen), then bound forms."""
        return sorted(self.order, key=lambda ipa: -self.counts[ipa]) + self.bound


def read_jsonl(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as lines:
        for line in lines:
            yield json.loads(line)


def collect(paths: list[Path]) -> dict[str, Readings]:
    readings: dict[str, Readings] = {}
    for path in paths:
        for entry in read_jsonl(path):
            if entry.get("lang_code") != "th":
                continue
            word = entry.get("word", "").strip()
            if not THAI_HEADWORD.fullmatch(word):
                continue
            for sound in entry.get("sounds", []):
                ipa = sound.get("ipa")
                if not ipa or sound.get("tags") or sound.get("raw_tags") or sound.get("note"):
                    continue
                readings.setdefault(word, Readings()).add(ipa.strip())
    return readings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--en", type=Path, default=KAIKKI_PATH, help="kaikki.org English-Wiktionary Thai JSONL(.gz)")
    parser.add_argument("--th", type=Path, default=TH_WIKTIONARY_PATH, help="kaikki.org Thai-Wiktionary raw JSONL(.gz)")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--report", type=Path, help="write a TSV of every word with rejected readings")
    args = parser.parse_args()

    # Both editions are needed: with one missing, the output silently loses readings.
    missing = [str(path) for path in (args.en, args.th) if not path.exists()]
    if missing:
        parser.error("dump not found: " + ", ".join(missing) + " (download commands in data/README.md)")
    readings = collect([args.en, args.th])
    entries = {word: found.preferred() for word, found in readings.items()}

    lines = [json.dumps(word, ensure_ascii=False) + ": " + json.dumps(ipa, ensure_ascii=False)
             for word, ipa in entries.items()]
    args.output.write_text("{\n" + ",\n".join(lines) + "\n}\n", encoding="utf-8")

    rejected = {word: found for word, found in readings.items() if found.rejected}
    print(f"Wiktionary headwords with IPA: {len(readings):,}")
    print(f"  with full readings: {sum(1 for f in readings.values() if f.counts):,}")
    print(f"  with only bound forms: {sum(1 for f in readings.values() if f.bound and not f.counts):,}")
    print(f"  with only rejected readings ([]): {sum(1 for f in readings.values() if not f.counts and not f.bound):,}")
    print(f"  with some rejected readings: {len(rejected):,}")
    print(f"Written: {args.output} ({len(entries):,} words)")

    if args.report:
        with open(args.report, "w", encoding="utf-8", newline="") as out:
            writer = csv.writer(out, delimiter="\t", lineterminator="\n")
            writer.writerow(["word", "rejected_readings", "kept_readings"])
            for word, found in rejected.items():
                writer.writerow([word, " ".join(found.rejected), " ".join(found.preferred())])
        print(f"Report: {args.report} ({len(rejected):,} rows)")


if __name__ == "__main__":
    main()

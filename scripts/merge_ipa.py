"""Merge generated IPA, Wiktionary IPA and manual overrides into ipa.json, ipa_metadata.json and dict.txt.

Inputs (data/sources/):
  generated_ipa.json      {word: ipa} from scripts/generate_ipa.py (lowest priority)
  wiktionary_ipa.json     {word: [ipa, ...]} from scripts/extract_wiktionary_ipa.py, most preferred reading first
                          ([] if Wiktionary has the word but none of its readings passed the regex)
  manual_overrides.json   {word: ipa}, hand corrections (highest priority; written without any filter)

Produces:
  data/ipa.json           flat {word: ipa_string} for runtime lookup
  data/ipa_metadata.json  rich {word: {ipa, source, pos, origin, ...}} for auditing
  data/dict.txt           the input word list, plus the new words kept below (appended at the end)

Words already in dict.txt get, in order of priority:

  1. their manual override;
  2. Wiktionary's reading: the generated reading if Wiktionary lists it too, else Wiktionary's preferred one;
  3. if Wiktionary lists only bound forms, its preferred bound form without the dot (กุมภ /kum˧.pʰa˦˥./ ->
     /kum˧.pʰa˦˥/). These are compound-initial spellings (ลักษณ, ประดิษฐ, ภาว), which G2P only meets when the
     tokenizer splits a compound that is not in dict.txt, where the linking syllable is said: ลักษณ|การ is
     /lak̚˦˥.sa˨˩.na˨˩/ /kaːn˧/, not the standalone reading the generated IPA gives (/lak̚˦˥.son˩˩˦/);
  4. the generated reading.

Wiktionary words not in dict.txt are new words, added to dict.txt and ipa.json (G2P only looks up dict.txt tokens, so
an ipa.json entry without a dict.txt word is never used) unless:

  - they have no full reading (none passed the regex, or only bound forms), or contain a slash;
  - they are shorter than --min-new-length characters (default 3): most are bare letters and undotted abbreviations
    (ก, จ, กจ, ง.), and as tokenizer words they cut up other text ("ตนเอง..." -> ตน|เอ|ง.|.., "ซวง" -> ซ|วง);
  - they are compositional (--keep-compositional adds them): every way of splitting the word into the fewest
    dictionary words gives the same reading as Wiktionary's (การสอบ = การ + สอบ). They add no reading, but as tokenizer
    words they make newmm pick other splits of the text around them, which lose linking syllables: กรรมการ|สอบ ->
    กรรม|การสอบ (/kam.ma.kaːn/ -> /kam.kaːn/), ราชการ|ไป -> ราช|การไป. Splits are made from the dict.txt words (with
    their merged readings), the overrides and the new words already kept, shortest words first. Readings are compared
    without final /ʔ/, which Wiktionary writes only at the end of a word.

A manual override of a new Wiktionary word skips the first two checks but not the compositional one (an override that
reads as the word's parts makes the word drop out). Overrides of words that are neither in dict.txt nor in Wiktionary
are always added. New words are appended in Wiktionary order, then overrides in file order.

ipa.json keeps the line order of the existing file (words no longer in it are dropped, new words appended in dict.txt
order), so a rebuild only shows real changes. Running the script again on its own output changes nothing.

Usage (from the repo root):

  python scripts/merge_ipa.py --report changes.tsv    # TSV of every reading that differs from the current ipa.json
  python scripts/merge_ipa.py --dry-run               # report only, files untouched
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from extract_wiktionary_ipa import KAIKKI_PATH, THAI_PHONOTACTICS, full_readings, read_jsonl, unbound  # noqa: E402

DATA_DIR = Path(__file__).parent.parent / "data"
DICT_PATH = DATA_DIR / "dict.txt"
MANUAL_PATH = DATA_DIR / "sources" / "manual_overrides.json"
WIKTIONARY_PATH = DATA_DIR / "sources" / "wiktionary_ipa.json"
GENERATED_PATH = DATA_DIR / "sources" / "generated_ipa.json"
OUTPUT_PATH = DATA_DIR / "ipa.json"
METADATA_PATH = DATA_DIR / "ipa_metadata.json"
GENERATED_MODEL = "claude-opus-4.6"

# A headword that can be added to dict.txt: Thai script, plus the spaces and dots dict.txt already uses
# ("ก ข ไม่กระดิกหู", "ก.ต.", "อด ๆ อยาก ๆ").
NEW_WORD = re.compile(r"[ก-๛][ก-๛ .]*")

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


def choose_wiktionary(listed: list[str], generated: str | None) -> str | None:
    """The generated reading if Wiktionary lists it, else Wiktionary's preferred full reading."""
    full = full_readings(listed)
    if generated in full:
        return generated
    return full[0] if full else None


def load_kaikki_metadata(path: Path) -> dict[str, dict]:
    """pos and origin language of each word, from the first English-Wiktionary entry."""
    metadata = {}
    if not path or not path.exists():
        return metadata
    for entry in read_jsonl(path):
        word = entry.get("word", "")
        if word in metadata:
            continue
        etym = entry.get("etymology_text", "") or ""
        origin = ""
        if "Pali" in etym or "Sanskrit" in etym:
            origin = "pali-sanskrit"
        elif "Proto-Tai" in etym:
            origin = "proto-tai"
        elif "Khmer" in etym:
            origin = "khmer"
        elif "Chinese" in etym or "Hokkien" in etym or "Teochew" in etym:
            origin = "chinese"
        elif "English" in etym:
            origin = "english"
        elif "Malay" in etym:
            origin = "malay"
        metadata[word] = {"pos": entry.get("pos", ""), "origin": origin}
    return metadata


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kaikki", type=Path, default=KAIKKI_PATH,
                        help="English-Wiktionary Thai JSONL(.gz) for pos/origin in ipa_metadata.json")
    parser.add_argument("--report", type=Path, help="write a TSV of every reading that differs from the current ipa.json")
    parser.add_argument("--dry-run", action="store_true", help="do not write ipa.json, ipa_metadata.json or dict.txt")
    parser.add_argument("--no-new", action="store_true", help="add no new Wiktionary words")
    parser.add_argument("--min-new-length", type=int, default=3, metavar="N",
                        help="skip new Wiktionary words shorter than N characters (default: 3; 1 adds all)")
    parser.add_argument("--keep-compositional", action="store_true",
                        help="also add new words whose reading is that of the dictionary words they split into")
    args = parser.parse_args()

    dict_text = DICT_PATH.read_text("utf-8")
    dict_words = [w for w in dict_text.splitlines() if w]
    dict_set = set(dict_words)
    manual = json.loads(MANUAL_PATH.read_text("utf-8")) if MANUAL_PATH.exists() else {}
    wiktionary = json.loads(WIKTIONARY_PATH.read_text("utf-8")) if WIKTIONARY_PATH.exists() else {}
    generated = json.loads(GENERATED_PATH.read_text("utf-8")) if GENERATED_PATH.exists() else {}
    previous = json.loads(OUTPUT_PATH.read_text("utf-8")) if OUTPUT_PATH.exists() else {}

    print(f"Dict words: {len(dict_words):,}")
    print(f"Manual overrides: {len(manual):,}")
    print(f"Wiktionary words: {len(wiktionary):,}")
    print(f"Generated entries: {len(generated):,}")

    stats = Counter()
    ipa: dict[str, str] = {}
    source: dict[str, str] = {}

    # Words already in dict.txt.
    for word in dict_words:
        listed = wiktionary.get(word, [])
        if word in manual:
            ipa[word], source[word] = manual[word], "manual"
        elif (chosen := choose_wiktionary(listed, generated.get(word))) is not None:
            ipa[word], source[word] = chosen, "wiktionary"
        elif listed:  # only bound forms
            ipa[word], source[word] = unbound(listed[0]), "wiktionary"
        elif word in generated:
            ipa[word], source[word] = generated[word], "generated"
        else:
            stats["dict.txt words without any reading"] += 1
            continue
        stats[f"dict.txt words from {source[word]}"] += 1

    # New Wiktionary words, decided shortest first so the words a compound splits into are already decided.
    candidates = []
    for word, listed in wiktionary.items():
        if word in dict_set:
            continue
        if word in manual:
            candidates.append((word, manual[word], "manual"))
            continue
        chosen = choose_wiktionary(listed, None)
        if chosen is None:
            stats["skipped new: no full reading"] += 1
        elif not NEW_WORD.fullmatch(word):
            stats["skipped new: not a dict.txt headword"] += 1
        elif args.no_new:
            stats["skipped new: --no-new"] += 1
        elif len(word) < args.min_new_length:
            stats[f"skipped new: shorter than {args.min_new_length} characters"] += 1
        else:
            candidates.append((word, chosen, "wiktionary"))
    manual_only = {w: v for w, v in manual.items() if w not in dict_set and w not in wiktionary}
    lookup = dict(ipa) | manual_only
    kept = set()
    for word, new, _ in sorted(candidates, key=lambda c: len(c[0])):
        if not args.keep_compositional and split_readings(word, lookup) == {comparable(new)}:
            stats["skipped new: compositional"] += 1
            continue
        lookup[word] = new
        kept.add(word)
    new_words = [(w, v, s) for w, v, s in candidates if w in kept] + [(w, v, "manual") for w, v in manual_only.items()]
    for word, new, src in new_words:
        ipa[word], source[word] = new, src
        dict_words.append(word)
        stats[f"new words from {src}"] += 1

    # Every Wiktionary reading and override passes the regex (generated readings are checked by validate_ipa.py).
    bad = [w for w, s in source.items() if s != "generated" and not THAI_PHONOTACTICS.fullmatch(ipa[w])]
    assert not bad, f"readings failing THAI_PHONOTACTICS: {bad[:10]}"
    assert set(ipa) <= set(dict_words)

    # Keep the existing line order; append the rest in dict.txt order.
    order = [w for w in previous if w in ipa] + [w for w in dict_words if w not in previous and w in ipa]
    ipa = {w: ipa[w] for w in order}

    for key, value in sorted(stats.items()):
        print(f"  {key}: {value:,}")
    changed = [w for w in ipa if previous.get(w) != ipa[w]]
    removed = [w for w in previous if w not in ipa]
    print(f"Merged IPA entries: {len(ipa):,} ({len(changed):,} differ from the current ipa.json, "
          f"{len(removed):,} removed)")

    if args.report:
        with open(args.report, "w", encoding="utf-8", newline="") as out:
            writer = csv.writer(out, delimiter="\t", lineterminator="\n")
            writer.writerow(["word", "source", "old_ipa", "new_ipa", "wiktionary_readings", "generated_ipa"])
            for word in changed + removed:
                writer.writerow([word, source.get(word, "removed"), previous.get(word, ""), ipa.get(word, ""),
                                 " ".join(wiktionary.get(word, [])), generated.get(word, "")])
        print(f"Report: {args.report} ({len(changed) + len(removed):,} rows)")

    if args.dry_run:
        print("Dry run: nothing written.")
        return

    kaikki = load_kaikki_metadata(args.kaikki)
    if not kaikki:
        print(f"Warning: {args.kaikki} not found; ipa_metadata.json gets no pos/origin")
    metadata = {}
    for word, value in ipa.items():
        meta = {"ipa": value, "source": source[word]}
        if source[word] == "generated":
            meta["model"] = GENERATED_MODEL
        elif source[word] == "wiktionary" and word in kaikki:
            meta["pos"] = kaikki[word]["pos"]
            if kaikki[word]["origin"]:
                meta["origin"] = kaikki[word]["origin"]
        metadata[word] = meta

    lines = ",\n".join(f"{json.dumps(w, ensure_ascii=False)}: {json.dumps(v, ensure_ascii=False)}"
                       for w, v in ipa.items())
    write_atomic(OUTPUT_PATH, "{\n" + lines + "\n}")
    write_atomic(METADATA_PATH, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    trailing = "\n" if dict_text.endswith("\n") else ""
    write_atomic(DICT_PATH, "\n".join(dict_words) + trailing)
    print(f"Written: {OUTPUT_PATH} ({len(ipa):,} entries), {METADATA_PATH}, {DICT_PATH} ({len(dict_words):,} words)")
    print(f"Coverage: {len(ipa) / len(dict_words) * 100:.1f}% of dict.txt")


if __name__ == "__main__":
    main()

# Data Directory

## Core Files (runtime)

| File | Description | Used by |
|------|-------------|---------|
| `dict.txt` | Word list (62k), one per line. Drives both tokenization and IPA lookup keys. | `Tokenizer`, `G2P` |
| `ipa.json` | Flat `{word: "/ipa/"}` for runtime lookup. Merged from Wiktionary + generated. | `G2P` |
| `ipa_metadata.json` | Rich `{word: {ipa, source, pos, origin, ...}}` for auditing. | analysis only |

## Fallback G2P Data (vendored from tltk)

| File | Size | Description |
|------|------|-------------|
| `fallback/sylrule.lts` | 4 KB | Syllable pattern regex rules |
| `fallback/thaisyl.dict` | 31 KB | Exception syllable pronunciations |
| `fallback/sylseg.3g` | 15 MB | Syllable trigram statistics |
| `fallback/PhSTrigram.sts` | 1.3 MB | Pronunciation selection trigrams |
| `fallback/thdict` | 2.3 MB | Thai word dictionary (for syllable segmentation) |
| `fallback/sylform_var.pkl` | 244 KB | Syllable form variants |

## Source Files (for rebuild/audit)

| File | Description | Git? |
|------|-------------|------|
| `sources/manual_overrides.json` | User corrections (highest merge priority) | ✓ |
| `sources/wiktionary_ipa.json` | `{word: [ipa, ...]}` for 35k Wiktionary headwords (English + Thai editions), preferred reading first | ✓ |
| `sources/generated_ipa.json` | ~49k IPA entries from Claude Opus 4.6 | ✓ |
| `sources/generated_ipa_invalid.json` | Entries that failed validation | ✗ (not committed) |
| `sources/kaikki.org-dictionary-Thai.jsonl.gz` | Raw English-Wiktionary dump, Thai entries (kaikki.org) | ✗ (not committed) |
| `sources/kaikki.org-thwiktionary-raw.jsonl.gz` | Raw Thai-Wiktionary dump (kaikki.org) | ✗ (not committed) |
| `sources/pali_sanskrit_words.txt` | Pali/Sanskrit word list for validation exclusion | ✓ |

## Test Files (prompt validation)

| File | Description |
|------|-------------|
| `tests/ground_truth_ipa.json` | 34 manually verified IPA (initial test set) |
| `tests/blind_test_ipa.json` | 17 blind test words |

---

## Updating the Dictionary

### Adding new words to dict.txt

1. Add word(s) to `data/dict.txt`
2. Generate IPA: run `scripts/generate_ipa.py` (only generates for missing words)
3. Re-merge: run `scripts/merge_ipa.py`
4. Result: `ipa.json` updated with new entries

### Correcting wrong IPA

1. Add correction to `data/sources/manual_overrides.json`: `{"word": "/correct_ipa/"}`
2. Re-merge: run `scripts/merge_ipa.py`
3. Result: `ipa.json` updated — manual overrides have highest priority

Merge priority: **manual_overrides.json > wiktionary_ipa.json > generated_ipa.json**. When Wiktionary lists several
readings, the generated one is kept if it is among them. `merge_ipa.py` also adds Wiktionary words that are not in
`dict.txt` yet to both `dict.txt` and `ipa.json`, except words shorter than 3 characters and words that read exactly
as the dictionary words they split into (see the script's docstring). Add an override only when the pipeline cannot
derive the reading: an override that equals what merge would produce anyway is redundant.

### Full rebuild from scratch

```bash
# 1. Make sure data/dict.txt exists

# 2. Download kaikki.org data (English edition, Thai entries; and the Thai edition)
curl -L -o data/sources/kaikki.org-dictionary-Thai.jsonl.gz "https://kaikki.org/dictionary/Thai/kaikki.org-dictionary-Thai.jsonl.gz"
curl -L -o data/sources/kaikki.org-thwiktionary-raw.jsonl.gz "https://kaikki.org/thwiktionary/raw-wiktextract-data.jsonl.gz"

# 3. Extract Wiktionary IPA
uv run python3 scripts/extract_wiktionary_ipa.py

# 4. Generate IPA for remaining words (requires AWS Bedrock access)
uv run python3 scripts/generate_ipa.py

# 5. Merge everything (reads the English dump for pos/origin in ipa_metadata.json; --report: TSV of every changed reading)
uv run python3 scripts/merge_ipa.py --report changes.tsv
```

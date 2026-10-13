"""Builds dictionary.json (same format as characters.json) from CC-CEDICT, most common characters first.

    pip install requests pypinyin wordfreq
    python build_dictionary.py                       # downloads CC-CEDICT
    python build_dictionary.py my_characters.json    # your own entries/meanings win

If the download fails, grab CC-CEDICT manually from https://cc-cedict.org/wiki/ and save it
next to this script as cedict_ts.u8. CC-CEDICT is CC BY-SA licensed; keep the attribution.
"""
import gzip, json, os, re, sys

from pypinyin import Style, pinyin
from wordfreq import zipf_frequency

URL = "https://www.mdbg.net/chinese/export/cedict/cedict_1_0_ts_utf-8_mdbg.txt.gz"
LINE = re.compile(r"^(\S+) (\S+) \[([^\]]+)\] /(.+)/$")
SKIP = ("variant of", "old variant", "surname", "see ", "CL:", "abbr. for", "erhua variant")


def read_cedict():
    if os.path.exists("cedict_ts.u8"):
        return open("cedict_ts.u8", encoding="utf-8").read()
    import requests
    return gzip.decompress(requests.get(URL, timeout=60).content).decode("utf-8")


def clean(defs):
    good = [d.strip() for d in defs if d.strip() and not d.startswith(SKIP)]
    return " / ".join((good or defs)[:3])[:70]


def main():
    readings = {}                                   # simplified char -> [(pinyin, defs)]
    for line in read_cedict().splitlines():
        m = LINE.match(line)
        if m and len(m.group(2)) == 1:
            py = m.group(3).lower().replace("u:", "v").replace(" ", "")
            readings.setdefault(m.group(2), []).append((py, m.group(4).split("/")))

    out = {}
    for ch, opts in readings.items():
        freq = zipf_frequency(ch, "zh")
        if freq <= 0:
            continue
        common = pinyin(ch, style=Style.TONE3, neutral_tone_with_five=True)[0][0]
        py, defs = next((o for o in opts if o[0] == common), opts[0])
        tone = int(py[-1]) if py[-1].isdigit() else 0
        out[ch] = (freq, {"pinyin": py.rstrip("012345"), "tone": 0 if tone == 5 else tone, "meaning": clean(defs)})

    result = {c: v for c, (_, v) in sorted(out.items(), key=lambda kv: -kv[1][0])}
    if len(sys.argv) > 1:                           # merge your own curated characters.json on top
        for c, e in json.load(open(sys.argv[1], encoding="utf-8")).items():
            result[c] = {k: e[k] for k in ("pinyin", "tone", "meaning", "alt") if k in e}

    with open("dictionary.json", "w", encoding="utf-8") as f:
        f.write("{\n" + ",\n".join(f"  {json.dumps(c, ensure_ascii=False)}: {json.dumps(e, ensure_ascii=False)}"
                                    for c, e in result.items()) + "\n}\n")
    print(f"Wrote dictionary.json with {len(result)} characters")


if __name__ == "__main__":
    main()

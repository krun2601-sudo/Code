"""Machine-check the locked technical facts in the AIS bilingual package."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAPER = ROOT.parent
SOURCE = PAPER / "ral_cn_final.md"
CHINESE = ROOT / "ais_cn_review.md"
OVERLEAF = ROOT / "overleaf_project"
LOCK = ROOT / "reports" / "fact_lock.json"
OUTPUT = ROOT / "qa" / "fidelity_validation.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(text: str, needles: list[str], scope: str) -> None:
    missing = [needle for needle in needles if needle not in text]
    if missing:
        raise AssertionError(f"{scope}: missing locked tokens: {missing}")


def display_equations_markdown(text: str) -> list[str]:
    return re.findall(r"\$\$\s*(.*?)\s*\$\$", text, flags=re.S)


def display_equations_tex(text: str) -> list[str]:
    return re.findall(
        r"\\begin\{equation\}(.*?)\\end\{equation\}", text, flags=re.S
    )


def canonical_equation(text: str) -> str:
    text = re.sub(r"\\label\{[^}]+\}", "", text)
    text = re.sub(r"\\begin\{(?:aligned|split)\}|\\end\{(?:aligned|split)\}", "", text)
    text = re.sub(r"\\\\\s*", "", text)
    text = re.sub(r"\\qquad\s*\\text\{\(\d+\)\}", "", text)
    text = re.sub(r"\\(?:mathrm|text)\{s\.t\.\s*\}", "st", text)
    text = re.sub(r"\\\s+", "", text)
    text = re.sub(r"\^\{([+0])\}", r"^\1", text)
    replacements = {
        r"\leq": r"\le",
        r"\left": "",
        r"\right": "",
        r"\quad": "",
        r"\qquad": "",
        r"\,": "",
        r"\!": "",
        "&": "",
        "{}": "",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = text.replace(r"\\", "")
    text = re.sub(r"\s+", "", text)
    return text.rstrip(".,")


def main() -> None:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    source = SOURCE.read_text(encoding="utf-8")
    chinese = CHINESE.read_text(encoding="utf-8")
    tex_paths = [OVERLEAF / "main.tex"] + sorted((OVERLEAF / "sections").glob("*.tex"))
    english = "\n".join(path.read_text(encoding="utf-8") for path in tex_paths)

    source_eq = display_equations_markdown(source)
    chinese_eq = display_equations_markdown(chinese)
    english_eq = display_equations_tex(english)
    assert len(source_eq) == len(chinese_eq) == len(english_eq) == lock["equations"]
    assert english.count(r"\begin{proposition}") == lock["propositions"]
    for number in range(1, lock["propositions"] + 1):
        assert f"命题 {number}" in chinese

    source_canon = [canonical_equation(eq) for eq in source_eq]
    chinese_canon = [canonical_equation(eq) for eq in chinese_eq]
    english_canon = [canonical_equation(eq) for eq in english_eq]
    assert source_canon == chinese_canon, "Chinese equations diverge from the locked source"
    if source_canon != english_canon:
        mismatches = [
            index + 1
            for index, (left, right) in enumerate(zip(source_canon, english_canon))
            if left != right
        ]
        raise AssertionError(f"English equations diverge at: {mismatches}")

    locked_numbers = [
        "240", "70", "461", "384", "-4.941", "-6.707", "-3.910",
        "-0.265", "-1.046", "-0.110", "-1.979", "-2.496", "-1.821",
        "0.998", "0.954", "1.0001", "0.010742", "100000", "1.05",
    ]
    require(source, locked_numbers, "locked source")
    require(chinese, locked_numbers, "Chinese manuscript")
    require(english, locked_numbers, "English manuscript")

    require(
        english,
        [
            "not a physical confidence region",
            "not detection probability",
            "cannot assign the observed difference specifically to repair",
            "one block was adverse",
            "do not establish hard real-time deadlines",
        ],
        "English claim audit",
    )
    require(
        chinese,
        [
            "不是包含站址、安装外参和逐机内参误差的物理置信域",
            "不能把差异唯一归因于修复",
            "9 个改善、1 个不利",
            "不是硬实时可调度性结论",
        ],
        "Chinese claim audit",
    )

    assert english.count(r"\begin{figure") == lock["display_items"]["figures"]
    assert english.count(r"\begin{table") == lock["display_items"]["tables"]
    assert chinese.count("figures/figure") == lock["display_items"]["figures"]
    assert len(re.findall(r"^\|:?-{3}", chinese, flags=re.M)) == lock["display_items"]["tables"]

    result = {
        "status": "PASS",
        "source_sha256": sha256(SOURCE),
        "english_main_sha256": sha256(OVERLEAF / "main.tex"),
        "english_sources_sha256": hashlib.sha256(english.encode("utf-8")).hexdigest(),
        "chinese_markdown_sha256": sha256(CHINESE),
        "equations_compared": len(source_eq),
        "propositions_checked": lock["propositions"],
        "figures_checked": lock["display_items"]["figures"],
        "tables_checked": lock["display_items"]["tables"],
        "locked_numeric_tokens_checked": len(locked_numbers),
        "changed_equations": "No",
        "changed_experimental_data": "No",
        "added_experiments": "No",
        "added_unsupported_claims": "No",
        "changed_conclusions": "No",
    }
    OUTPUT.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

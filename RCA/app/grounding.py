"""Mechanical grounding for the narrator (docs/RCA_OUTPUT_CONTRACT.md §2 narration rules):
every number the LLM writes must already exist in the ledger, at some rounding, or the
narrative is discarded in favor of a plain summary built straight from the ledger."""

import re

from app.utils import iter_leaves

NUMBER_RE = re.compile(r"-?\d+\.\d+|-?\d+")


def fallback_summary(ledger: dict) -> str:
    """Built directly from the ledger, no LLM — used when narrate has nothing to say
    (unreproduced/undecomposed alerts) or when the LLM's output fails the grounding check."""
    lines = [f"Diagnosis for {ledger.get('metric_id')}: verdict={ledger.get('verdict', 'unknown')}."]

    window = ledger.get("window")
    if window:
        lines.append(f"Window: {window['start']} to {window['end']}.")

    decomposition = ledger.get("decomposition")
    if decomposition:
        for f in decomposition["factors"]:
            lines.append(
                f"  factor={f['metric_id']} contribution_rel={f['contribution_rel']:.4f} "
                f"verdict={f['verdict']}"
            )

    for finding in ledger.get("findings", []):
        g = finding["global"]
        lines.append(
            f"  {finding['factor']}: actual={g['actual']:.4f} expected={g['expected']:.4f} "
            f"peak|z|={g['peak_abs_z']:.2f} verdict={finding['verdict']}"
        )
        holdout = finding.get("holdout")
        if holdout:
            c = holdout["candidate"]
            lines.append(f"    top candidate: {c['dim_name']}={c['dim_value']} (holdout: {holdout['verdict']})")

    return "\n".join(lines)


def allowed_numbers(ledger: dict) -> set[str]:
    """Every number a grounded narrative is allowed to contain — raw ledger values plus the
    forms prose commonly renders them in (percentage/pp, sign dropped). Also pulls digit
    runs out of string leaves (dim values like 'Android 15', ISO timestamps) so a mention
    of the window date or segment name isn't flagged as fabricated."""
    allowed = set()
    for leaf in iter_leaves(ledger):
        if isinstance(leaf, bool):
            continue
        if isinstance(leaf, (int, float)):
            for v in (leaf, leaf * 100, abs(leaf), abs(leaf) * 100):
                # 0-6dp: covers both a verbatim echo of ClickHouse's raw float precision
                # and prose that rounds to 1-2 significant digits for readability
                for nd in range(7):
                    allowed.add(f"{v:.{nd}f}")
                if float(v).is_integer():
                    allowed.add(str(int(v)))
        elif isinstance(leaf, str):
            allowed.update(NUMBER_RE.findall(leaf))
    return allowed


def check_grounding(text: str, allowed: set[str]) -> list[str]:
    normalized = text.replace("−", "-")  # unicode minus, in case the model writes one
    found = NUMBER_RE.findall(normalized)
    return sorted({n for n in found if n not in allowed})

"""Form/answer parsing and the item-analysis engine.

The engine is deliberately pure: it takes the current forms, answer rows and
human review decisions, and returns a complete, deterministic snapshot.  The
caller persists that snapshot in one transaction together with the change that
triggered it, so forms, mapping corrections, review decisions and statistics
always become visible at the same moment.
"""
from __future__ import annotations

import csv
import io
import json
import re
from collections import OrderedDict


class ValidationError(ValueError):
    """Raised for bad CSV input or an invalid form/mapping edit."""


# --------------------------------------------------------------------------
# CSV parsing
# --------------------------------------------------------------------------

FORM_HEADERS = {
    "q_no": {"题号", "卷式题号", "印刷题号", "q_no", "qno", "question", "题号\n"},
    "letter": {"选项字母", "字母", "涂选字母", "option", "letter", "选项"},
    "std_code": {"标准题码", "题码", "标准题号", "std_code", "code", "standard_code"},
    "std_option": {"标准选项", "标准答案选项", "std_option", "standard_option"},
}
ANSWER_HEADERS = {
    "card_no": {"卡号", "答题卡号", "卡编号", "card_no", "card", "card_id"},
    "q_no": {"题号", "卷式题号", "印刷题号", "q_no", "qno", "question"},
    "letter": {"涂选字母", "选项字母", "字母", "letter", "option", "作答"},
    "scanned_code": {"实扫题码", "扫描题码", "实扫题号", "scanned_code", "scan_code", "scanned"},
}


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValidationError("无法识别文件编码，请使用 UTF-8 或 GBK 编码的 CSV")


def _norm(value) -> str:
    return (value or "").strip()


def _read_rows(raw: bytes, spec: dict, kind: str):
    """Return (data_rows, text) so callers can persist the decoded CSV."""
    text = _decode(raw)
    reader = csv.reader(io.StringIO(text))
    rows = [[_norm(c) for c in r] for r in reader if any(_norm(c) for c in r)]
    if not rows:
        raise ValidationError(f"{kind}为空")

    def locate(header):
        found = {}
        for idx, cell in enumerate(header):
            key = cell.lower().replace(" ", "")
            if not key:
                continue
            for field, aliases in spec.items():
                if key in {a.lower() for a in aliases} and field not in found:
                    found[field] = idx
        return found

    positions = locate(rows[0])
    if positions:
        missing = [f for f in spec if f not in positions]
        if missing:
            raise ValidationError(f"{kind}缺少列：{', '.join(missing)}")
        data_rows = rows[1:]
    else:
        # Headerless file: accept the canonical column order.
        positions = {field: i for i, field in enumerate(spec)}
        data_rows = rows

    out = []
    for r in data_rows:
        out.append(tuple(r[positions[f]] if positions[f] < len(r) else "" for f in spec))
    return out, text


def decode_csv(raw: bytes) -> str:
    """Decode an uploaded CSV to text so job payloads stay UTF-8 safe."""
    return _decode(raw)


def validate_form_rows(rows):
    """Validate one form's mapping rows. Returns the same rows on success."""
    seen = set()
    q_to_code: dict[str, str] = {}
    code_to_q: dict[str, str] = {}
    code_letters: dict[str, dict[str, str]] = {}
    for q_no, letter, std_code, std_option in rows:
        if not q_no or not letter:
            raise ValidationError(f"卷式行缺少题号或选项字母：{q_no or '?'}/{letter or '?'}")
        if not std_code or not std_option:
            raise ValidationError(f"卷式行缺少标准题码或标准选项：{q_no}/{letter}")
        if (q_no, letter) in seen:
            raise ValidationError(f"卷式内题号 {q_no} 的选项 {letter} 重复")
        seen.add((q_no, letter))
        if q_to_code.setdefault(q_no, std_code) != std_code:
            raise ValidationError(
                f"卷式内题号 {q_no} 映射到多个标准题码（{q_to_code[q_no]} / {std_code}）"
            )
        if code_to_q.setdefault(std_code, q_no) != q_no:
            raise ValidationError(
                f"标准题码 {std_code} 在本卷出现多次（题号 {code_to_q[std_code]} / {q_no}）"
            )
        letters = code_letters.setdefault(std_code, {})
        if std_option in letters:
            raise ValidationError(
                f"标准题码 {std_code} 的标准选项 {std_option} 重复"
            )
        letters[std_option] = letter
    return rows


def parse_form_csv(raw: bytes):
    """Return [(q_no, letter, std_code, std_option), ...] validated per form."""
    rows, _ = _read_rows(raw, FORM_HEADERS, "卷式 CSV")
    if not rows:
        raise ValidationError("卷式 CSV 没有数据行")
    return validate_form_rows(rows)


def parse_answer_csv(raw: bytes):
    """Return [{card_no, q_no, letter, scanned_code}, ...]."""
    rows, _ = _read_rows(raw, ANSWER_HEADERS, "答题 CSV")
    if not rows:
        raise ValidationError("答题 CSV 没有数据行")
    seen = set()
    out = []
    for card_no, q_no, letter, scanned in rows:
        if not card_no or not q_no:
            raise ValidationError("答题行缺少卡号或题号")
        if (card_no, q_no) in seen:
            raise ValidationError(f"卡号 {card_no} 的题号 {q_no} 出现多行")
        seen.add((card_no, q_no))
        out.append(
            {"card_no": card_no, "q_no": q_no, "letter": letter, "scanned_code": scanned}
        )
    return out


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def _natural_key(text: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", text)]


def build_forms(form_rows) -> "OrderedDict[int, dict]":
    """form_rows: iterable of (form_id, form_name, q_no, letter, std_code, std_option)."""
    forms: "OrderedDict[int, dict]" = OrderedDict()
    for form_id, form_name, q_no, letter, std_code, std_option in form_rows:
        form = forms.setdefault(
            form_id,
            {"id": form_id, "name": form_name, "qmap": {}, "codes": {},
             "options": OrderedDict()},
        )
        entry = form["qmap"].setdefault(
            q_no, {"std_code": std_code, "letters": {}, "rev": {}}
        )
        entry["letters"][letter] = std_option
        entry["rev"][std_option] = letter
        form["codes"][std_code] = q_no
        form["options"].setdefault(std_code, set()).add(std_option)
    return forms


def form_is_compatible(form: dict, card_rows: dict) -> bool:
    """Full-card question numbers match and every non-empty code/letter fits."""
    if not form["qmap"]:
        # A form with no mappings cannot be a candidate for any card.
        return False
    if set(form["qmap"].keys()) != set(card_rows.keys()):
        return False
    for q_no, row in card_rows.items():
        mapping = form["qmap"][q_no]
        if row["scanned_code"] and mapping["std_code"] != row["scanned_code"]:
            return False
        if row["letter"] and row["letter"] not in mapping["letters"]:
            return False
    return True


class Analysis:
    def __init__(self):
        self.question_codes: list[str] = []
        self.options: dict[str, list[str]] = {}
        self.stats: dict[str, dict] = {}
        self.card_results: list[dict] = []
        self.unmatched_cards: list[dict] = []
        self.flagged: list[dict] = []
        # Judgement inputs needed to re-run "these two options on these two
        # questions" queries against this snapshot later, after forms change.
        self.forms: list[dict] = []
        self.cards: list[dict] = []
        self.cards_total = 0
        self.cards_matched = 0

    def summary(self) -> dict:
        return {
            "cards_total": self.cards_total,
            "cards_matched": self.cards_matched,
            "cards_unmatched": len(self.unmatched_cards),
            "questions": len(self.question_codes),
            "flagged_cells": len(self.flagged),
        }


def analyse(forms, card_rows_by_card: dict, decisions: dict) -> Analysis:
    """Compute the whole analysis snapshot.

    forms: OrderedDict from build_forms
    card_rows_by_card: {card_no: {q_no: {"letter":..., "scanned_code":...}}}
    decisions: {(card_no, std_code): {"resolution":..., "note":...}}
    """
    result = Analysis()

    # Standard questions and their option universe are defined by live forms.
    codes = set()
    options: dict[str, set[str]] = {}
    for form in forms.values():
        form_questions = []
        for q_no in sorted(form["qmap"], key=_natural_key):
            entry = form["qmap"][q_no]
            form_questions.append({
                "q_no": q_no,
                "std_code": entry["std_code"],
                "letters": dict(entry["letters"]),
            })
        result.forms.append({
            "id": form["id"],
            "name": form["name"],
            "questions": form_questions,
            "codes": sorted(form["codes"], key=_natural_key),
        })
        for code, letters in form["options"].items():
            codes.add(code)
            options.setdefault(code, set()).update(letters)
    result.question_codes = sorted(codes, key=_natural_key)
    result.options = {c: sorted(options[c]) for c in result.question_codes}
    result.cards = [
        {
            "card_no": card_no,
            "rows": [
                {
                    "q_no": q_no,
                    "letter": card_rows_by_card[card_no][q_no]["letter"],
                    "scanned_code": card_rows_by_card[card_no][q_no]["scanned_code"],
                }
                for q_no in sorted(card_rows_by_card[card_no], key=_natural_key)
            ],
            # Filled in below: the compatible forms judged for this snapshot.
            # Pair queries reuse this list instead of re-judging the card.
            "form_ids": [],
        }
        for card_no in sorted(card_rows_by_card)
    ]
    card_details = {c["card_no"]: c for c in result.cards}

    all_forms = list(forms.values())
    for code in result.question_codes:
        result.stats[code] = {
            "counted": 0, "blank": 0, "flagged": 0,
            "options": {o: 0 for o in result.options[code]},
        }

    result.cards_total = len(card_rows_by_card)
    for card_no in sorted(card_rows_by_card):
        rows = card_rows_by_card[card_no]
        compatible = [f for f in all_forms if form_is_compatible(f, rows)]
        card_details[card_no]["form_ids"] = [f["id"] for f in compatible]
        if not compatible:
            detail = "无可相容卷式（整卡题号不一致或非空题码/涂选字母不吻合）"
            result.unmatched_cards.append({"card_no": card_no, "detail": detail})
            result.card_results.append(
                {"card_no": card_no, "std_code": "", "status": "unmatched",
                 "std_option": None, "detail": detail}
            )
            continue
        result.cards_matched += 1
        form_names = "/".join(f["name"] for f in compatible)

        for code in result.question_codes:
            stat = result.stats[code]
            decision = decisions.get((card_no, code))

            containing = [f for f in compatible if code in f["codes"]]
            if not containing:
                # No compatible form printed this question: skip entirely.
                continue

            answers = []
            for form in containing:
                q_no = form["codes"][code]
                letter = rows[q_no]["letter"]
                if letter:
                    std_option = form["qmap"][q_no]["letters"][letter]
                else:
                    std_option = ""
                answers.append((form["name"], letter, std_option))

            if len(containing) != len(compatible):
                missing = [f["name"] for f in compatible if code not in f["codes"]]
                status, option, detail = (
                    "flagged", None,
                    f"部分相容卷式未出该题：{'/'.join(missing)}",
                )
            else:
                distinct = {a[2] for a in answers}
                if len(distinct) == 1:
                    option = answers[0][2]
                    if option:
                        status, detail = "counted", f"相容卷式：{form_names}"
                    else:
                        status, detail = "blank", f"相容卷式：{form_names}（空答）"
                else:
                    shown = "；".join(
                        f"{name}: {letter or '空'}" for name, letter, _ in answers
                    )
                    status, option, detail = (
                        "flagged", None, f"卷式归一作答分歧：{shown}"
                    )

            applied = apply_decision(decision, status, stat["options"])
            if applied is not None:
                status, option, detail = applied

            if status == "skipped":
                continue
            if status == "counted":
                stat["counted"] += 1
                stat["options"][option] += 1
            elif status == "blank":
                stat["blank"] += 1
            else:
                stat["flagged"] += 1
                result.flagged.append(
                    {"card_no": card_no, "std_code": code, "detail": detail}
                )
            result.card_results.append(
                {"card_no": card_no, "std_code": code, "status": status,
                 "std_option": option, "detail": detail}
            )

    result.flagged.sort(key=lambda x: (_natural_key(x["card_no"]), _natural_key(x["std_code"])))
    return result


def apply_decision(decision, status, option_universe):
    """Apply a human conclusion to an automatic cell verdict.

    Returns (status, option, detail) or None when the conclusion does not
    apply.  A conclusion is only meaningful where the automatic verdict could
    not be trusted (disagreement / unprinted / blank); once a correction makes
    the cell consistent, recalculated statistics win and the stale conclusion
    stops applying.
    """
    if not decision or status not in ("flagged", "blank"):
        return None
    note = f"；{decision['note']}" if decision.get("note") else ""
    res = decision["resolution"]
    if res == "skip":
        return "skipped", None, "人工核对：跳过（不计入）" + note
    if res == "blank":
        return "blank", None, "人工核对：漏答" + note
    if res.startswith("opt:") and res[4:] in option_universe:
        chosen = res[4:]
        return "counted", chosen, "人工核对：" + chosen + note
    return None


# --------------------------------------------------------------------------
# Paired-option query over a saved snapshot
# --------------------------------------------------------------------------

DETAIL_VERSION = 1


def load_detail(raw):
    """Parse a snapshot's stored judgement data, or None if unavailable."""
    if not raw:
        return None
    try:
        detail = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(detail, dict) or detail.get("version") != DETAIL_VERSION:
        return None
    if not isinstance(detail.get("forms"), list) or not isinstance(detail.get("cards"), list):
        return None
    return detail


def detail_questions(detail) -> "OrderedDict[str, list[str]]":
    """Question codes and their option universe as recorded in a snapshot."""
    options: dict[str, set[str]] = {}
    for form in detail["forms"]:
        for question in form.get("questions", []):
            options.setdefault(question["std_code"], set()).update(question.get("letters", {}).values())
    return OrderedDict(
        (code, sorted(options[code], key=_natural_key))
        for code in sorted(options, key=_natural_key)
    )


def _normalise(question: dict, rows: dict) -> str:
    row = rows.get(question["q_no"]) or {}
    letter = (row.get("letter") or "").strip()
    if not letter:
        return ""
    return question.get("letters", {}).get(letter, "")


def pair_query(detail, q1: str, o1: str, q2: str, o2: str) -> dict:
    """Min/max share of cards answering ``o2`` on the second question among
    cards that answered ``o1`` on the first one.

    Every card with at least one compatible form independently picks one of
    those forms -- the snapshot's already-judged compatible forms are reused,
    never re-derived.  A pick counts in the denominator when that form printed
    both questions and the card's first answer normalises to ``o1``.  It counts
    in the numerator too when the second answer normalises to ``o2``; a blank
    or different second answer stays in the denominator.

    Different picks per card form the possible "配卷情形".  The function
    returns the smallest and largest ratio over all scenarios whose denominator
    is non-zero, each with a numerator/denominator pair that reaches it.
    """
    options = detail_questions(detail)
    if q1 == q2:
        raise ValidationError("两道标准题必须不同")
    if q1 not in options or q2 not in options:
        raise ValidationError("所选标准题不在该快照中")
    if o1 not in options[q1]:
        raise ValidationError(f"标准题 {q1} 在该快照中没有选项 {o1}")
    if o2 not in options[q2]:
        raise ValidationError(f"标准题 {q2} 在该快照中没有选项 {o2}")

    forms: dict[int, dict] = {}
    for form in detail["forms"]:
        by_code = {
            question["std_code"]: question
            for question in form.get("questions", [])
        }
        forms[form["id"]] = {
            "name": form.get("name", ""),
            q1: by_code.get(q1),
            q2: by_code.get(q2),
        }

    # Per card, the set of possible (denominator, numerator) contributions.
    # A card "can be excluded" when one of its allowed forms does not print
    # both questions or its first answer does not normalise to o1.
    outcomes_by_card: list[set[tuple[int, int]]] = []
    for card in detail["cards"]:
        rows = {row["q_no"]: row for row in card.get("rows", [])}
        outcomes: set[tuple[int, int]] = set()
        for form_id in card.get("form_ids", []):
            form = forms.get(form_id)
            if form is None:
                outcomes.add((0, 0))
                continue
            first, second = form[q1], form[q2]
            if first is None or second is None:
                # This form did not print both questions: the card cannot use
                # it for this pair.
                outcomes.add((0, 0))
                continue
            if _normalise(first, rows) != o1:
                outcomes.add((0, 0))
                continue
            outcomes.add((1, 1) if _normalise(second, rows) == o2 else (1, 0))
        if not outcomes:
            outcomes.add((0, 0))
        outcomes_by_card.append(outcomes)

    cards_total = len(outcomes_by_card)
    cards_judged = sum(1 for s in outcomes_by_card if s != {(0, 0)})

    forced = [s for s in outcomes_by_card if (0, 0) not in s]  # always in denominator
    optional = [s for s in outcomes_by_card if (0, 0) in s]

    contributes = any((1, 0) in s or (1, 1) in s for s in outcomes_by_card)
    no_sample = not contributes

    def band(numerator: int, denominator: int):
        return {
            "numerator": numerator,
            "denominator": denominator,
            "ratio": (numerator / denominator) if denominator else None,
        }

    min_band = max_band = None
    if not no_sample:
        forced_zero = sum(1 for s in forced if (1, 0) not in s)      # only (1,1)
        optional_zero = sum(1 for s in optional if (1, 0) in s)      # can add a 0
        optional_one = sum(1 for s in optional if (1, 1) in s)

        # Smallest share: all forced cards join (with a zero answer whenever
        # the form offers one), plus every optional card offering a zero
        # answer.  Cards that can only answer the target are left out unless
        # the denominator would otherwise be zero.
        min_num = forced_zero
        min_den = len(forced) + optional_zero
        if min_den == 0:
            min_den = 1
            min_num = 1 if optional_one else 0

        # Largest share: every card that can answer the target does so and
        # no card is dropped if it would take the denominator below one;
        # otherwise only cards that cannot be excluded remain.
        max_num = sum(1 for s in outcomes_by_card if (1, 1) in s)
        max_den = len(forced) + optional_one
        if max_den == 0:
            max_den = 1  # only zero answers are available: 0 / 1
            max_num = 0

        min_band, max_band = band(min_num, min_den), band(max_num, max_den)

    return {
        "q1": q1, "o1": o1, "q2": q2, "o2": o2,
        "cards_total": cards_total,
        "cards_judged": cards_judged,
        "no_sample": no_sample,
        "min": min_band,
        "max": max_band,
        "options": options,
    }


# --------------------------------------------------------------------------
# DB loading + persistence
# --------------------------------------------------------------------------

def load_forms(conn):
    rows = conn.execute(
        "SELECT f.id, f.name, m.q_no, m.letter, m.std_code, m.std_option "
        "FROM forms f JOIN form_mappings m ON m.form_id = f.id "
        "ORDER BY f.id, m.q_no, m.letter"
    ).fetchall()
    return build_forms(
        [(r["id"], r["name"], r["q_no"], r["letter"], r["std_code"], r["std_option"])
         for r in rows]
    )


def load_card_rows(conn):
    cards: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT card_no, q_no, letter, scanned_code FROM answer_rows "
        "ORDER BY card_no, q_no"
    ):
        cards.setdefault(r["card_no"], {})[r["q_no"]] = {
            "letter": r["letter"], "scanned_code": r["scanned_code"],
        }
    return cards


def load_decisions(conn):
    return {
        (r["card_no"], r["std_code"]): {
            "resolution": r["resolution"], "note": r["note"],
        }
        for r in conn.execute("SELECT card_no, std_code, resolution, note FROM review_decisions")
    }


def compute(conn) -> Analysis:
    return analyse(load_forms(conn), load_card_rows(conn), load_decisions(conn))


def persist_run(conn, analysis: Analysis, created_at: str) -> int:
    """Insert a full snapshot and return its run id. Caller owns the transaction."""
    cur = conn.execute(
        "INSERT INTO runs(created_at, summary_json, detail_json) VALUES (?, '{}', NULL)",
        (created_at,),
    )
    run_id = cur.lastrowid
    conn.execute(
        "UPDATE runs SET summary_json = ?, detail_json = ? WHERE id = ?",
        (
            json.dumps(analysis.summary(), ensure_ascii=False),
            json.dumps(
                {
                    "version": 1,
                    "forms": analysis.forms,
                    "cards": analysis.cards,
                },
                ensure_ascii=False,
            ),
            run_id,
        ),
    )
    conn.executemany(
        "INSERT INTO run_question_stats(run_id, std_code, counted, blank, flagged) "
        "VALUES (?, ?, ?, ?, ?)",
        [(run_id, code, st["counted"], st["blank"], st["flagged"])
         for code, st in analysis.stats.items()],
    )
    conn.executemany(
        "INSERT INTO run_option_counts(run_id, std_code, std_option, cnt) VALUES (?, ?, ?, ?)",
        [(run_id, code, opt, cnt)
         for code, st in analysis.stats.items() for opt, cnt in st["options"].items()],
    )
    conn.executemany(
        "INSERT INTO run_card_results(run_id, card_no, std_code, status, std_option, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(run_id, r["card_no"], r["std_code"], r["status"], r["std_option"], r["detail"])
         for r in analysis.card_results],
    )
    conn.executemany(
        "INSERT INTO run_unmatched(run_id, card_no, detail) VALUES (?, ?, ?)",
        [(run_id, u["card_no"], u["detail"]) for u in analysis.unmatched_cards],
    )
    return run_id


def prune_runs(conn, keep: int = 4) -> None:
    rows = conn.execute(
        "SELECT id FROM runs ORDER BY id DESC LIMIT -1 OFFSET ?", (keep,)
    ).fetchall()
    for r in rows:
        conn.execute("DELETE FROM runs WHERE id = ?", (r["id"],))

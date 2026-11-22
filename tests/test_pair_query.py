import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from examapp import db, engine  # noqa: E402
from examapp.app import create_app, read_run  # noqa: E402


FORM_HEADER = "题号,选项字母,标准题码,标准选项\n"
ANSWER_HEADER = "卡号,题号,涂选字母,实扫题码\n"


def form_csv(rows):
    return FORM_HEADER + "".join(f"{q},{l},{c},{o}\n" for q, l, c, o in rows)


def answer_csv(rows):
    return ANSWER_HEADER + "".join(
        f"{card},{q},{letter},{code}\n" for card, q, letter, code in rows
    )


@pytest.fixture()
def app(tmp_path):
    return create_app(str(tmp_path / "test.db"), sync_jobs=True)


@pytest.fixture()
def client(app):
    return app.test_client()


def upload(client, path, csv_text, name=None):
    data = {"file": (io.BytesIO(csv_text.encode("utf-8")), "upload.csv")}
    if name is not None:
        data["name"] = name
    response = client.post(
        path, data=data, content_type="multipart/form-data", follow_redirects=True
    )
    assert response.status_code == 200
    return response


def current_run(app):
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        try:
            return read_run(conn, db.active_run_id(conn))
        finally:
            conn.close()


def run_detail(app, run_id):
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        try:
            row = conn.execute(
                "SELECT detail_json FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            return engine.load_detail(row["detail_json"])
        finally:
            conn.close()


# Two forms printing the same questions but with reversed letter maps, so a
# card is compatible with both while the normalised answer depends on which
# form it is paired with.
FORM_A = form_csv([
    ("1", "A", "Q1", "A"), ("1", "B", "Q1", "B"),
    ("2", "A", "Q2", "A"), ("2", "B", "Q2", "B"),
])
FORM_B = form_csv([
    ("1", "A", "Q1", "B"), ("1", "B", "Q1", "A"),
    ("2", "A", "Q2", "B"), ("2", "B", "Q2", "A"),
])


def test_pair_query_reports_min_and_max_with_witnesses(client, app):
    upload(client, "/forms", FORM_A, name="A卷")
    upload(client, "/forms", FORM_B, name="B卷")
    upload(client, "/answers", answer_csv([
        # C1: A卷 -> (Q1=A, Q2=A); B卷 -> (Q1=B, Q2=B)
        ("C1", "1", "A", ""), ("C1", "2", "A", ""),
        # C2: A卷 -> (Q1=A, Q2=B); B卷 -> (Q1=B, Q2=A)
        ("C2", "1", "A", ""), ("C2", "2", "B", ""),
    ]))
    run = current_run(app)
    detail = run_detail(app, run["id"])
    result = engine.pair_query(detail, "Q1", "A", "Q2", "A")

    assert result["no_sample"] is False
    # Min: C2 paired with A卷 (0/1).  Max: C1 paired with A卷 (1/1).
    assert (result["min"]["numerator"], result["min"]["denominator"]) == (0, 1)
    assert (result["max"]["numerator"], result["max"]["denominator"]) == (1, 1)
    assert result["min"]["ratio"] == 0
    assert result["max"]["ratio"] == 1


def test_blank_second_answer_stays_in_denominator(client, app):
    upload(client, "/forms", FORM_A, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", ""), ("C1", "2", "", ""),
        ("C2", "1", "A", ""), ("C2", "2", "A", ""),
    ]))
    detail = run_detail(app, current_run(app)["id"])
    result = engine.pair_query(detail, "Q1", "A", "Q2", "A")
    assert (result["min"]["numerator"], result["min"]["denominator"]) == (1, 2)
    assert (result["max"]["numerator"], result["max"]["denominator"]) == (1, 2)


def test_only_cards_whose_first_answer_matches_enter_denominator(client, app):
    upload(client, "/forms", FORM_A, name="A卷")
    upload(client, "/answers", answer_csv([
        # First answer is B: not the queried option, so the card is excluded.
        ("C1", "1", "B", ""), ("C1", "2", "B", ""),
        ("C2", "1", "A", ""), ("C2", "2", "B", ""),
    ]))
    result = engine.pair_query(
        run_detail(app, current_run(app)["id"]), "Q1", "A", "Q2", "A")
    assert (result["min"]["numerator"], result["min"]["denominator"]) == (0, 1)
    assert (result["max"]["numerator"], result["max"]["denominator"]) == (0, 1)


def test_unmatched_cards_never_enter_denominator(client, app):
    upload(client, "/forms", FORM_A, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", "Q9"), ("C1", "2", "A", ""),   # incompatible scan
        ("C2", "1", "A", ""), ("C2", "2", "A", ""),
    ]))
    result = engine.pair_query(
        run_detail(app, current_run(app)["id"]), "Q1", "A", "Q2", "A")
    assert (result["max"]["numerator"], result["max"]["denominator"]) == (1, 1)


def test_no_sample_when_every_denominator_is_zero(client, app):
    upload(client, "/forms", FORM_A, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "B", ""), ("C1", "2", "A", ""),
    ]))
    result = engine.pair_query(
        run_detail(app, current_run(app)["id"]), "Q1", "A", "Q2", "A")
    assert result["no_sample"] is True
    assert result["min"] is None and result["max"] is None


def test_forms_missing_one_question_make_the_card_excludable(client, app):
    # Both forms print two questions and are compatible with the card, but
    # form B's second printed question is Q3, so a card paired with B cannot
    # contribute to the Q1/Q2 denominator.
    form_b = form_csv([
        ("1", "A", "Q1", "A"), ("1", "B", "Q1", "B"),
        ("2", "A", "Q3", "A"), ("2", "B", "Q3", "B"),
    ])
    upload(client, "/forms", FORM_A, name="A卷")
    upload(client, "/forms", form_b, name="B卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", ""), ("C1", "2", "A", ""),
    ]))
    result = engine.pair_query(
        run_detail(app, current_run(app)["id"]), "Q1", "A", "Q2", "A")
    # Only A卷 offers a non-zero denominator (and it matches on both
    # questions), so both extremes are 1/1; pairing with B卷 yields 0/0 and
    # is not a valid scenario.
    assert (result["min"]["numerator"], result["min"]["denominator"]) == (1, 1)
    assert (result["max"]["numerator"], result["max"]["denominator"]) == (1, 1)


def test_changing_query_within_snapshot_reuses_judged_forms(client, app):
    upload(client, "/forms", FORM_A, name="A卷")
    upload(client, "/forms", FORM_B, name="B卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", ""), ("C1", "2", "A", ""),
    ]))
    detail = run_detail(app, current_run(app)["id"])
    # Switching the second question's target option must only re-read the
    # recorded answers; the compatible forms stay exactly as judged.
    first = engine.pair_query(detail, "Q1", "A", "Q2", "A")
    second = engine.pair_query(detail, "Q1", "A", "Q2", "B")
    assert first["cards_total"] == second["cards_total"] == 1
    assert (first["max"]["numerator"], first["max"]["denominator"]) == (1, 1)
    # With Q2 = B only the A卷 pairing can stay in the denominator (B卷's
    # first answer normalises to B, not A), and its Q2 answer is A: 0/1.
    assert (second["min"]["numerator"], second["min"]["denominator"]) == (0, 1)
    assert (second["max"]["numerator"], second["max"]["denominator"]) == (0, 1)


def test_pair_query_requires_two_distinct_questions(client, app):
    upload(client, "/forms", FORM_A, name="A卷")
    upload(client, "/answers", answer_csv([("C1", "1", "A", "")]))
    detail = run_detail(app, current_run(app)["id"])
    with pytest.raises(engine.ValidationError):
        engine.pair_query(detail, "Q1", "A", "Q1", "A")


def test_new_snapshot_keeps_judgement_data_for_pair_query(client, app):
    upload(client, "/forms", FORM_A, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", ""), ("C1", "2", "A", ""),
    ]))
    detail = run_detail(app, current_run(app)["id"])
    assert detail is not None
    assert detail["cards"][0]["form_ids"]
    assert detail["forms"][0]["questions"]


def test_old_snapshot_keeps_its_own_forms_after_correction(client, app):
    upload(client, "/forms", FORM_A, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", ""), ("C1", "2", "A", ""),
    ]))
    old_id = current_run(app)["id"]
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        form_id = conn.execute("SELECT id FROM forms").fetchone()["id"]
        conn.close()
    # Correct the first printed question to a new standard code.
    client.post("/forms/correct", data={
        "form_id": str(form_id), "q_no": "1", "new_code": "Q1X",
    }, follow_redirects=True)

    # The old snapshot still answers queries about Q1 as it was then.
    old_detail = run_detail(app, old_id)
    assert "Q1" in engine.detail_questions(old_detail)
    result = engine.pair_query(old_detail, "Q1", "A", "Q2", "A")
    assert (result["max"]["numerator"], result["max"]["denominator"]) == (1, 1)

    # The new snapshot no longer knows Q1.
    new_detail = run_detail(app, current_run(app)["id"])
    assert "Q1" not in engine.detail_questions(new_detail)
    with pytest.raises(engine.ValidationError):
        engine.pair_query(new_detail, "Q1", "A", "Q2", "A")


def test_old_snapshot_keeps_its_own_answers_after_reimport(client, app):
    upload(client, "/forms", FORM_A, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", ""), ("C1", "2", "A", ""),
    ]))
    old_id = current_run(app)["id"]
    upload(client, "/answers", answer_csv([
        ("C1", "1", "B", ""), ("C1", "2", "B", ""),
    ]))
    result = engine.pair_query(
        run_detail(app, old_id), "Q1", "A", "Q2", "A")
    assert (result["max"]["numerator"], result["max"]["denominator"]) == (1, 1)

    new_result = engine.pair_query(
        run_detail(app, current_run(app)["id"]), "Q1", "A", "Q2", "A")
    assert new_result["no_sample"] is True


def test_snapshot_without_detail_reports_unavailable(client, app):
    upload(client, "/forms", FORM_A, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", ""), ("C1", "2", "A", ""),
    ]))
    run_id = current_run(app)["id"]
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        conn.execute("UPDATE runs SET detail_json = NULL WHERE id = ?", (run_id,))
        conn.close()

    page = client.get(f"/pair?run={run_id}")
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "缺少判定资料" in html
    page = client.get(f"/pair?run={run_id}&q1=Q1&o1=A&q2=Q2&o2=A")
    assert "无法分析" in page.get_data(as_text=True)


def test_pair_page_renders_result_and_options(client, app):
    upload(client, "/forms", FORM_A, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", ""), ("C1", "2", "A", ""),
    ]))
    run_id = current_run(app)["id"]
    page = client.get(f"/pair?run={run_id}&q1=Q1&o1=A&q2=Q2&o2=A")
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "最小" in html and "最大" in html
    assert "1 / 1" in html
    assert "Q1" in html and "Q2" in html


def test_pair_page_no_sample_message(client, app):
    upload(client, "/forms", FORM_A, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "B", ""), ("C1", "2", "A", ""),
    ]))
    run_id = current_run(app)["id"]
    html = client.get(
        f"/pair?run={run_id}&q1=Q1&o1=A&q2=Q2&o2=A"
    ).get_data(as_text=True)
    assert "无样本" in html


def test_current_run_navigation_links_to_pair_query(client, app):
    upload(client, "/forms", FORM_A, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", ""), ("C1", "2", "A", ""),
    ]))
    run_id = current_run(app)["id"]
    html = client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert f"/pair?run={run_id}" in html


def test_legacy_database_gains_detail_column(tmp_path):
    # A database created before the detail column existed must still open.
    import sqlite3
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            summary_json TEXT NOT NULL DEFAULT '{}'
        );
        INSERT INTO runs(created_at) VALUES ('2026-01-01 00:00:00');
    """)
    conn.close()
    db.init_db(path)
    conn = db.connect(path)
    row = conn.execute("SELECT detail_json FROM runs WHERE id = 1").fetchone()
    conn.close()
    assert row["detail_json"] is None

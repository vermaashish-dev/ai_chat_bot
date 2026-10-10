from sqlalchemy import func, select

from app import db
from app.models import ConversationTurn, IngestionFile, Supplier, SupplierAlias, SupplierConversation
from app.services.ingestion_service import (
    IngestionService,
    extract_name_from_greeting,
    extract_name_from_summary,
    parse_markdown_conversations,
)
from tests.conftest import BOT_HEADER, SELLER_HEADER, bot, seller, write_csv


def run(env, force=False):
    return IngestionService(env["settings"], db.session_factory()).run(force=force)


def by_type(summary, name):
    return next(f for f in summary.files if f.file_path.endswith(name))


def count(model, *where):
    with db.session_factory()() as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def test_initial_ingestion_counts(ingested):
    s = ingested["summary"]
    assert by_type(s, "sellers_part1.csv").imported == 7
    assert by_type(s, "sellers_part2.csv").imported == 1
    assert by_type(s, "bot_calls_a.csv").imported == 11
    assert count(Supplier) == 8
    assert count(SupplierConversation, SupplierConversation.channel == "executive_call") == 2
    assert s.missing_inputs == []


def test_rerun_is_idempotent_and_skips_unchanged_files(ingested):
    second = run(ingested)
    assert {f.status for f in second.files} == {"skipped_unchanged"}
    assert second.totals() == {"imported": 0, "updated": 0, "skipped": 0, "failed": 0}
    forced = run(ingested, force=True)
    assert forced.totals()["imported"] == 0 and forced.totals()["updated"] == 0
    assert count(Supplier) == 8


def test_changed_row_is_updated_not_duplicated(ingested):
    write_csv(ingested["data"] / "sellers_part2.csv", SELLER_HEADER, [seller(1008, seller_state="Tamil Nadu", seller_city="Madurai")])
    res = by_type(run(ingested), "sellers_part2.csv")
    assert (res.imported, res.updated) == (0, 1)
    with db.session_factory()() as s:
        assert s.scalar(select(Supplier.city).where(Supplier.glid == 1008)) == "Madurai"
    assert count(Supplier) == 8


def test_duplicate_call_across_files_is_skipped(ingested):
    res = by_type(ingested["summary"], "recording_sample.csv")
    assert (res.imported, res.skipped) == (0, 1)
    assert count(SupplierConversation, SupplierConversation.source_record_id == "1", SupplierConversation.channel == "bot_call") == 1


def test_malformed_rows_reported_without_losing_good_rows(env):
    p = env["data"] / "bot_calls_a.csv"
    with p.open("a", encoding="utf-8") as f:
        f.write('99,1001,PUA,main_vani,1,"2026-09-09 10:00:00",30\n')  # too few fields
        f.write('abc,1001,PUA,main_vani,1,2026-09-09 10:00:00,30,"bad id",,General (talked),0,0,1\n')  # invalid attempt_id
    res = by_type(run(env), "bot_calls_a.csv")
    assert res.status == "partial"
    assert res.failed == 2 and res.imported == 11
    assert any("expected 13 fields" in e for e in res.errors)
    with db.session_factory()() as s:
        rec = s.scalar(select(IngestionFile).where(IngestionFile.file_path.like("%bot_calls_a.csv")))
        assert rec.status == "partial" and "expected 13 fields" in rec.error_summary


def test_unlinked_bot_call_flagged_for_review(ingested):
    with db.session_factory()() as s:
        c = s.scalar(select(SupplierConversation).where(SupplierConversation.source_record_id == "9"))
        assert c.supplier_id is None and c.link_status == "needs_review" and c.glid == 9999


def test_invalid_timestamp_kept_but_marked(ingested):
    with db.session_factory()() as s:
        c = s.scalar(select(SupplierConversation).where(SupplierConversation.source_record_id == "4"))
        assert c.conversation_ts is None and c.ts_valid is False and c.summary == "Undated call summary."


def test_second_transcript_variant_kept_identical_row_skipped(ingested):
    res = by_type(ingested["summary"], "call_turns.csv")
    assert (res.imported, res.skipped) == (3, 1)
    with db.session_factory()() as s:
        variants = s.scalars(select(ConversationTurn.transcript_variant).where(ConversationTurn.turn_no == 1)).all()
        assert sorted(variants) == [0, 1]


def test_executive_calls_do_not_store_employee_id(ingested):
    with db.session_factory()() as s:
        c = s.scalar(select(SupplierConversation).where(SupplierConversation.channel == "executive_call"))
        assert "fk_employeeid" not in c.details_json and "777" not in str(c.details_json)


def test_unrecognized_csv_and_missing_dirs(env, tmp_path, monkeypatch):
    write_csv(env["data"] / "random.csv", ["foo", "bar"], [[1, 2]])
    s = run(env)
    assert by_type(s, "random.csv").status == "unrecognized"
    from app.config import Settings

    missing = Settings(supplier_data_dir=tmp_path / "nope", conversation_data_dir=tmp_path / "nope2", database_url=env["settings"].database_url)
    s2 = IngestionService(missing, db.session_factory()).run()
    assert any("SUPPLIER_DATA_DIR not found" in m for m in s2.missing_inputs)
    assert any("No seller file" in m for m in s2.missing_inputs)


def test_cp1252_file_is_decoded_with_warning(env):
    write_csv(env["data"] / "bot_calls_b.csv", BOT_HEADER, [bot(50, 1007, "2026-09-09 10:00:00", "Café Owner said call later.")], encoding="cp1252")
    res = by_type(run(env), "bot_calls_b.csv")
    assert res.imported == 1 and any("cp1252" in e for e in res.errors)


def test_aliases_derived_from_summaries_and_greetings(ingested):
    with db.session_factory()() as s:
        rows = s.execute(select(SupplierAlias.business_name, SupplierAlias.source)).all()
    names = {n for n, _ in rows}
    assert {"Alpha Traders", "Beta Exports", "Common Name Co"} <= names
    assert ("Alpha Traders", "bot_turn_greeting") in rows


def test_name_extraction_rejects_prose():
    assert extract_name_from_summary("asking if the lead was from BS Industries. The lead confirmed") == "BS Industries"
    assert extract_name_from_summary("called the lead from A One Traders. The lead expressed frustration") == "A One Traders"
    assert extract_name_from_summary("asking if they were from Jaguar Locks for a follow-up discussion.") == "Jaguar Locks"
    assert extract_name_from_summary("asking if the lead was from AS Traders and located in Pune") == "AS Traders"
    assert extract_name_from_summary("asking if the lead was from IndiaMART.") is None
    assert extract_name_from_summary("The call was disconnected.") is None
    assert extract_name_from_greeting("Hello, kya aap MT Enterprises se bol rahe hain?") == "MT Enterprises"


def test_markdown_conversations_linking(ingested):
    conv = ingested["data"] / "conversations"
    (conv / "by_glid.md").write_text("---\nglid: 1001\ndate: 2026-09-20 11:00\noutcome: Callback\n---\n# Call\nSeller: busy, call next week.\n", encoding="utf-8")
    (conv / "by_name.md").write_text("business_name: Beta Exports\ndate: 21/09/2026\n\nSeller asked about pricing.\n", encoding="utf-8")
    (conv / "unknown.md").write_text("business_name: Common Name Co\n\nAmbiguous seller text without date.\n", encoding="utf-8")
    (conv / "multi.md").write_text("# Log\nglid: 1008\n\n## Call one\ndate: 2026-09-01\nFirst call.\n\n## Call two\ndate: 2026-09-15\nSecond call.\n", encoding="utf-8")
    (conv / "empty.md").write_text("---\nglid: 1001\n---\n   \n", encoding="utf-8")
    s = run(ingested)
    with db.session_factory()() as ses:
        md = {c.source_record_id: c for c in ses.scalars(select(SupplierConversation).where(SupplierConversation.channel == "markdown"))}
    by = {k.split("/")[-1]: v for k, v in md.items()}
    assert by["by_glid.md#0"].glid == 1001 and by["by_glid.md#0"].ts_valid
    assert by["by_name.md#0"].glid == 1002 and by["by_name.md#0"].details_json["link_method"] == "business_name_exact"
    assert by["unknown.md#0"].link_status == "needs_review" and by["unknown.md#0"].ts_valid is False
    assert by["multi.md#0"].glid == 1008 and by["multi.md#1"].conversation_ts.day == 15
    assert by_type(s, "empty.md").failed == 1
    # Re-ingesting a changed Markdown file replaces its records instead of duplicating them.
    (conv / "by_glid.md").write_text("---\nglid: 1001\ndate: 2026-09-20 11:00\n---\nSeller: busy, call next Monday.\n", encoding="utf-8")
    run(ingested)
    assert count(SupplierConversation, SupplierConversation.source_file.like("%by_glid.md")) == 1


def test_parse_markdown_single_conversation_default():
    out = parse_markdown_conversations("## Notes\nNo metadata here.\n## More\nStill one conversation.\n")
    assert len(out) == 1

import pytest
from sqlalchemy import inspect

from app import db
from app.models import Supplier
from app.services.conversation_repository import ConversationRepository
from app.services.evidence_service import get_evidence
from app.services.supplier_repository import AmbiguousSupplier, SupplierNotFound, SupplierRepository, normalize_name


def test_normalize_name():
    assert normalize_name("M/s. Gamma Industries Pvt. Ltd.") == "gamma industries pvt ltd"
    assert normalize_name("Alpha & Sons Private Limited") == "alpha and sons pvt ltd"
    assert normalize_name("  ALPHA-traders ") == "alpha traders"


def test_resolution_order(session):
    repo = SupplierRepository(session)
    assert repo.resolve(glid=1001).match_method == "glid"
    exact = repo.resolve(business_name="Alpha Traders")
    assert (exact.supplier.glid, exact.match_method) == (1001, "exact_name")
    norm = repo.resolve(business_name="  ALPHA-traders ")
    assert (norm.supplier.glid, norm.match_method) == (1001, "normalized_name")


def test_glid_with_mismatched_name_warns(session):
    res = SupplierRepository(session).resolve(glid=1001, business_name="Beta Exports")
    assert res.supplier.glid == 1001 and res.warnings


def test_ambiguous_name_never_silently_selected(session):
    with pytest.raises(AmbiguousSupplier) as exc:
        SupplierRepository(session).resolve(business_name="Common Name Co")
    assert sorted(c.glid for c in exc.value.candidates) == [1005, 1006]


def test_not_found_returns_fuzzy_suggestions_only(session):
    with pytest.raises(SupplierNotFound) as exc:
        SupplierRepository(session).resolve(business_name="Alpha Tradrs")
    assert [c.glid for c in exc.value.suggestions] == [1001]
    with pytest.raises(SupplierNotFound):
        SupplierRepository(session).resolve(glid=424242)


def test_history_order_tiebreak_and_undated(session):
    sup = SupplierRepository(session).by_glid(1001)
    h = ConversationRepository(session).history(sup.id, limit=10)
    assert [c.source_record_id for c in h.dated] == ["3", "2", "1"]  # same timestamp: higher id first
    assert [c.source_record_id for c in h.undated] == ["4"]
    assert h.executive["total_calls"] == 2 and h.executive["answered_calls"] == 1
    assert "2" in h.turns and [t.turn_no for t in h.turns["2"]] == [1, 2]  # variant 0 only
    assert any("no reliable timestamp" in w for w in h.warnings)


def test_history_limit_truncates_with_warning(session):
    sup = SupplierRepository(session).by_glid(1001)
    h = ConversationRepository(session).history(sup.id, limit=1)
    assert len(h.dated) == 1 and h.total_dated == 3
    assert any("truncated" in w for w in h.warnings)


def test_indexes_exist(ingested):
    insp = inspect(db.get_engine())
    def cols(table):
        out = [tuple(i["column_names"]) for i in insp.get_indexes(table)]
        out += [tuple(u["column_names"]) for u in insp.get_unique_constraints(table)]
        return out
    assert ("glid",) in cols("suppliers")
    assert ("normalized_name",) in cols("supplier_aliases")
    assert ("supplier_id", "conversation_ts") in cols("supplier_conversations")
    assert ("content_hash",) in cols("supplier_conversations")
    assert ("channel", "source_record_id") in cols("supplier_conversations")


def test_supplier_profile_preserves_source_fields(session):
    sup = session.query(Supplier).filter_by(glid=1004).one()
    assert sup.profile_json["seller_pincode"] == "302001"
    assert sup.profile_json["past_objections"].startswith("Seller Unavailability")


def test_evidence_shape_and_cache(ingested, session):
    ev = get_evidence(session)
    assert ev["available"] and ev["baseline"]["answered_bot_calls"] == 10  # the unlinked call (glid 9999) is excluded
    assert {"by_bot_version", "seller_level", "signal_response"} <= set(ev)
    assert ingested["settings"].evidence_cache_path.exists()

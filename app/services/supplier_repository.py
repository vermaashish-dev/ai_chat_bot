"""Supplier lookup. Order: GLID -> exact business name -> normalized name. Fuzzy only suggests."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Supplier, SupplierAlias

_SUFFIXES = [
    (r"\bprivate limited\b", "pvt ltd"),
    (r"\bpvt\.?\s*ltd\.?\b", "pvt ltd"),
    (r"\blimited\b", "ltd"),
    (r"\bco\.?\b", "co"),
]


def normalize_name(name: str) -> str:
    n = name.lower().replace("&", " and ")
    n = re.sub(r"^\s*m\s*/\s*s\.?\s+", "", n)  # "M/s ABC Traders"
    for pat, rep in _SUFFIXES:
        n = re.sub(pat, rep, n)
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


@dataclass
class Candidate:
    glid: int
    business_name: str
    city: str | None
    state: str | None


class SupplierNotFound(Exception):
    def __init__(self, query: str, suggestions: list[Candidate]):
        super().__init__(query)
        self.query = query
        self.suggestions = suggestions


class AmbiguousSupplier(Exception):
    def __init__(self, query: str, candidates: list[Candidate]):
        super().__init__(query)
        self.query = query
        self.candidates = candidates


@dataclass
class Resolution:
    supplier: Supplier
    match_method: str  # glid | exact_name | normalized_name
    names: list[SupplierAlias]
    warnings: list[str]


class SupplierRepository:
    def __init__(self, session: Session):
        self.s = session

    def by_glid(self, glid: int) -> Supplier | None:
        return self.s.scalar(select(Supplier).where(Supplier.glid == glid))

    def aliases(self, supplier_id: int) -> list[SupplierAlias]:
        return list(
            self.s.scalars(
                select(SupplierAlias).where(SupplierAlias.supplier_id == supplier_id).order_by(SupplierAlias.occurrences.desc(), SupplierAlias.id)
            )
        )

    def _suppliers_for(self, condition) -> list[Supplier]:
        ids = select(SupplierAlias.supplier_id).where(condition).distinct()
        return list(self.s.scalars(select(Supplier).where(Supplier.id.in_(ids)).order_by(Supplier.glid)))

    def find_by_name(self, name: str) -> list[Supplier]:
        exact = self._suppliers_for(SupplierAlias.business_name == name.strip())
        return exact or self._suppliers_for(SupplierAlias.normalized_name == normalize_name(name))

    def _candidates(self, suppliers: list[Supplier]) -> list[Candidate]:
        out = []
        for sup in suppliers:
            names = self.aliases(sup.id)
            out.append(Candidate(sup.glid, names[0].business_name if names else "", sup.city, sup.state))
        return out

    def suggest(self, name: str, limit: int = 5) -> list[Candidate]:
        norm = normalize_name(name)
        if not norm:
            return []
        first = norm.split(" ")[0]
        # Prefix range on the indexed column keeps this cheap as the table grows.
        rows = self.s.execute(
            select(SupplierAlias.supplier_id, SupplierAlias.normalized_name)
            .where(SupplierAlias.normalized_name >= first, SupplierAlias.normalized_name < first + "￿")
            .limit(5000)
        ).all()
        scored: dict[int, float] = {}
        for sid, cand in rows:
            score = difflib.SequenceMatcher(None, norm, cand).ratio()
            if score >= 0.6 and score > scored.get(sid, 0):
                scored[sid] = score
        top = sorted(scored, key=lambda k: -scored[k])[:limit]
        sups = {s.id: s for s in self.s.scalars(select(Supplier).where(Supplier.id.in_(top)))}
        return self._candidates([sups[i] for i in top if i in sups])

    def resolve(self, business_name: str | None = None, glid: int | None = None) -> Resolution:
        warnings: list[str] = []
        if glid is not None:
            sup = self.by_glid(glid)
            if sup is None:
                raise SupplierNotFound(str(glid), [])
            names = self.aliases(sup.id)
            if business_name and normalize_name(business_name) not in {a.normalized_name for a in names}:
                warnings.append("business_name does not match any name on record for this GLID; GLID was used")
            return Resolution(sup, "glid", names, warnings)
        assert business_name
        for method, cond in (
            ("exact_name", SupplierAlias.business_name == business_name.strip()),
            ("normalized_name", SupplierAlias.normalized_name == normalize_name(business_name)),
        ):
            sups = self._suppliers_for(cond)
            if len(sups) == 1:
                return Resolution(sups[0], method, self.aliases(sups[0].id), warnings)
            if len(sups) > 1:
                raise AmbiguousSupplier(business_name, self._candidates(sups))
        raise SupplierNotFound(business_name, self.suggest(business_name))

    def counts(self) -> dict[str, int]:
        return {
            "suppliers": self.s.scalar(select(func.count(Supplier.id))) or 0,
            "suppliers_with_names": self.s.scalar(select(func.count(func.distinct(SupplierAlias.supplier_id)))) or 0,
        }

"""Bilance účtu a ochrana heslem."""
import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sorare_mlb import ledger  # noqa: E402
from sorare_mlb.features import build_features  # noqa: E402
from sorare_mlb.schema import Schema  # noqa: E402

SDL = (Path(__file__).parent / "fixtures" / "mini_schema.graphql").read_text()


@pytest.fixture
def store(monkeypatch, tmp_path):
    import sorare_mlb.archive as archive_mod
    import sorare_mlb.store as store_mod

    monkeypatch.setattr(store_mod, "LOCAL_FILE", tmp_path / "s.json")
    fake = store_mod.LocalStore()
    for mod in (archive_mod, ledger):
        monkeypatch.setattr(mod, "get_store", lambda: fake)
    return fake


def test_ledger_queries_validate():
    graphql = pytest.importorskip("graphql")
    feats = build_features(Schema(SDL))["ledger"]
    assert feats["available"] and feats["sources"][0]["field"] == "accountEntries"
    gql = graphql.build_schema(SDL)
    for src in feats["sources"]:
        assert not graphql.validate(gql, graphql.parse(src["query"]))
    assert not graphql.validate(gql, graphql.parse(feats["balance_query"]))


@pytest.mark.parametrize("entry_type,expected", [
    ("DEPOSIT", "deposit"), ("WITHDRAWAL", "withdrawal"), ("AUCTION_BID", "purchase"),
    ("SALE", "sale"), ("FEE", "fee"), ("SOMETHING", "other"),
])
def test_classification(entry_type, expected):
    row = ledger.normalize(
        {"id": "1", "entryType": entry_type, "createdAt": "2024-05-01T10:00:00Z",
         "amounts": {"eurCents": -1250, "usdCents": None}},
        "accountEntries",
    )
    assert row["category"] == expected
    assert row["eur"] == 12.5 and row["date"].startswith("2024-05-01")


def test_summary_and_manual(store):
    rows = [
        ledger.normalize({"id": str(i), "entryType": t, "createdAt": d, "amounts": {"eurCents": c}}, "a")
        for i, (t, d, c) in enumerate([
            ("DEPOSIT", "2023-01-02", 10000), ("AUCTION_BID", "2023-01-03", 6000),
            ("SALE", "2024-02-01", 2500), ("WITHDRAWAL", "2024-03-01", 3000),
            ("FEE", "2024-02-01", 100),
        ])
    ]
    entry = ledger.add_manual("deposit", 50, "2024-06-01", "kartou")
    s = ledger.summarize(rows + ledger.manual_entries(), reward_money_eur=12)
    h = s["headline"]
    assert h["deposited"] == 150 and h["withdrawn"] == 30
    assert h["cash_result"] == -120
    assert h["market_result"] == -36          # 25 − 60 − 1
    assert [y["year"] for y in s["years"]] == ["2023", "2024"]
    assert ledger.delete_manual(entry["id"]) and not ledger.manual_entries()
    with pytest.raises(ValueError):
        ledger.add_manual("nesmysl", 1, "2024-01-01")


def test_failed_entries_are_ignored():
    row = ledger.normalize({"id": "1", "entryType": "DEPOSIT", "status": "FAILED",
                            "amounts": {"eurCents": 999}}, "a")
    assert ledger.summarize([row])["headline"]["deposited"] == 0


def test_password_gate(monkeypatch):
    from fastapi.testclient import TestClient

    import api.index as app_mod

    monkeypatch.setenv("APP_PASSWORD", "tajne")
    client = TestClient(app_mod.app)
    assert client.get("/nastaveni").status_code == 401
    good = base64.b64encode(b"kb:tajne").decode()
    assert client.get("/nastaveni", headers={"Authorization": f"Basic {good}"}).status_code == 200
    # cron má vlastní secret, heslo ho neblokuje
    assert "Přihlášení vyžadováno" not in client.get("/api/cron").text


def test_roles_and_duplicate_withdrawals():
    general = [
        ledger.normalize({"id": "1", "entryType": "DEPOSIT", "createdAt": "2024-01-01", "amounts": {"eurCents": 10000}}, "accountEntries"),
        ledger.normalize({"id": "2", "entryType": "WITHDRAWAL", "createdAt": "2024-02-01", "amounts": {"eurCents": 4000}}, "accountEntries"),
    ]
    extra = [
        ledger.normalize({"id": "3", "createdAt": "2024-02-01", "amount": {"eurCents": 4000}}, "bankWithdrawals", "withdrawal"),
        ledger.normalize({"id": "4", "createdAt": "2024-03-01", "amount": {"eurCents": 2500}, "status": "succeeded"},
                         "spentFiatPaymentIntents", "card_payment"),
        ledger.normalize({"id": "5", "createdAt": "2024-03-02", "quantity": 120, "rarity": "limited"},
                         "cardShardsHistoryTransactions", "essence"),
        ledger.normalize({"id": "6", "createdAt": "2024-03-03", "quantity": 50, "type": "SPENT"},
                         "cardShardsHistoryTransactions", "essence"),
    ]
    s = ledger.summarize(general + extra)
    h = s["headline"]
    assert h["withdrawn"] == 40 and h["skipped_duplicates"] == 1
    assert h["card_payments"] == 25 and h["invested"] == 125
    assert h["cash_result"] == -85
    assert s["quantities"]["essence"] == {"gained": 120, "spent": 50, "count": 2}
    assert extra[2]["eur"] is None


def test_withdrawal_source_counts_when_general_has_none():
    rows = [
        ledger.normalize({"id": "1", "entryType": "DEPOSIT", "amounts": {"eurCents": 1000}}, "accountEntries"),
        ledger.normalize({"id": "3", "amount": {"eurCents": 400}}, "withdrawals", "withdrawal"),
    ]
    assert ledger.summarize(rows)["headline"]["withdrawn"] == 4

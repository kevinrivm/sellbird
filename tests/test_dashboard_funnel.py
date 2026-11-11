"""Dashboard funnel per industry. Fictional data only."""

import asyncio
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import sellbird.dashboard as dash
from sellbird.models import Company
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "sellbird.db"
        monkeypatch.setattr(dash, "DB_PATH", db)
        monkeypatch.setattr(dash, "WEB_DIR", Path(__file__).resolve().parent.parent / "sellbird" / "web")
        dash.token_store.configure(Path(tmp) / "data" / "dashboard-token")
        token = dash.token_store.get()
        with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                        headers={"Authorization": f"Bearer {token}"}) as c:
            c.db_path = db
            yield c


async def _seed(db_path):
    sm = StateManager(str(db_path))
    await sm.init_db()
    dent = await sm.add_company(Company(name="Dental Uno", industry="Consultorio dental"))
    await sm.add_company(Company(name="Dental Dos", industry="Consultorio dental"))
    vet = await sm.add_company(Company(name="Vet Uno", industry="Veterinaria"))
    for i, (cid, ind, status) in enumerate([(dent, "Consultorio dental", "verified"),
                                            (vet, "Veterinaria", "guess")]):
        await sm.add_prospect(Prospect(email=f"hola{i}@example.com", email_status=status,
                                       email_verified=status == "verified", company_id=cid,
                                       company="x", industry=ind, title="General inbox",
                                       source="company_inbox"))


def test_funnel_groups_by_industry(client):
    asyncio.run(_seed(client.db_path))
    data = client.get("/api/funnel/segments").json()
    by = {s["segment"]: s for s in data["segments"]}
    assert by["Consultorio dental"]["found"] == 2
    assert by["Consultorio dental"]["emails"] == 1 and by["Consultorio dental"]["verified"] == 1
    assert by["Veterinaria"]["unverified"] == 1
    assert data["total"]["found"] == 3 and data["total"]["emails"] == 2


def test_funnel_on_an_empty_database(client):
    data = client.get("/api/funnel/segments").json()
    assert data["segments"] == [] and data["total"]["found"] == 0

"""DENUE (INEGI) discovery source and the `verify` queue. Fictional data only."""

import os
import tempfile

import httpx
import pytest
import pytest_asyncio

from sellbird.collectors import discover as D
from sellbird.config import load_config
from sellbird.paths import PROJECT_ROOT
from sellbird.signals import SIGNAL_CATALOG, seed_signal_catalog
from sellbird.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "test.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        for sig in SIGNAL_CATALOG:
            await sm.set_signal_status(sig["code"], "confirmed")
        yield sm


@pytest.fixture
def config():
    return load_config(str(PROJECT_ROOT / "sellbird.yaml"))


def record(**kw):
    base = {
        "Id": "1001", "Nombre": "CONSULTORIO DENTAL EJEMPLO", "Razon_social": "",
        "Clase_actividad": "Consultorios dentales del sector privado",
        "Estrato": "0 a 5 personas", "Tipo_vialidad": "CALLE", "Calle": "UNO",
        "Num_Exterior": "10", "Colonia": "CENTRO", "CP": "00000",
        "Ubicacion": "CIUDAD EJEMPLO, Ciudad Ejemplo, JALISCO",
        "Telefono": "5550000000", "Correo_e": "hola@example.com", "Sitio_internet": "",
    }
    base.update(kw)
    return base


# ── Places ──


@pytest.mark.parametrize("location, expected", [
    ("Guadalajara, Jalisco, México", ("14", "guadalajara")),
    ("Jalisco, México", ("14", "")),
    ("León, Guanajuato", ("11", "leon")),
    ("Querétaro, Querétaro, México", ("22", "queretaro")),
    ("Ciudad de México", ("09", "")),
    ("Denver, CO", ("", "")),
    ("", ("", "")),
])
def test_mx_place(location, expected):
    assert D.mx_place(location) == expected


# ── Records ──


def test_denue_is_a_free_listed_provider():
    p = D.PROVIDERS["denue"]
    assert p.env_keys == ("inegi_denue_token",)
    assert p.estimate([D.DiscoveryQuery(term="dentista", location="Jalisco", limit=500)]) == 0.0


def test_a_record_becomes_a_business_with_its_published_email():
    q = D.DiscoveryQuery(term="dentista", location="Ciudad Ejemplo, Jalisco")
    biz = D.Denue().to_business(record(), q)
    assert biz.name == "Consultorio Dental Ejemplo"
    assert biz.email == "hola@example.com"
    assert biz.external_id == "denue:1001"
    assert biz.has_website is False and biz.domain == ""
    assert biz.employees_min == 0
    assert biz.category.startswith("Consultorios dentales")
    assert "idee=1001" in biz.source_url


def test_a_malformed_email_is_dropped_and_a_facebook_site_is_not_a_website():
    biz = D.Denue().to_business(record(Correo_e="no tiene", Sitio_internet="www.facebook.com/ejemplo"),
                                D.DiscoveryQuery(term="x"))
    assert biz.email == ""
    assert biz.website == "" and biz.facebook_url.endswith("/ejemplo")


def test_a_real_website_is_kept():
    biz = D.Denue().to_business(record(Sitio_internet="www.example.com"), D.DiscoveryQuery(term="x"))
    assert biz.domain == "example.com" and biz.has_website is True


def test_class_and_city_filters(config):
    config.icp.denue_classes = {"dentista": ["consultorios dentales"]}
    config.icp.keyword_variants = {"dentista": ["dental"]}
    p = D.Denue()
    p.configure(config)
    assert p.keeps(record(), "dentista", "ciudad ejemplo")
    assert p.keeps(record(), "dental", "")
    assert not p.keeps(record(Clase_actividad="Comercio al por mayor de material dental"), "dental", "")
    assert not p.keeps(record(), "dentista", "otra ciudad")
    assert p.keeps(record(Clase_actividad="Lo que sea"), "sin clases", "")


def test_headcount_over_the_limit_is_too_big(config):
    config.icp.max_employees = 30
    q = D.DiscoveryQuery(term="x")
    assert D.too_big(D.Denue().to_business(record(Estrato="51 a 100 personas"), q), config)
    assert not D.too_big(D.Denue().to_business(record(Estrato="11 a 30 personas"), q), config)


# ── Fetch ──


@pytest.mark.asyncio
async def test_fetch_pages_through_the_state_and_keeps_the_city():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        start = int(request.url.path.split("/")[-3])
        if start == 1:
            rows = [record(Id=str(i), Ubicacion="OTRA, Otra, JALISCO") for i in range(D.Denue.PAGE)]
            return httpx.Response(200, json=rows)
        return httpx.Response(200, json=[record(Id="9")])

    p = D.Denue()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        found = await p.fetch(client, {"inegi_denue_token": "tok"},
                              D.DiscoveryQuery(term="Dentistá", location="Ciudad Ejemplo, Jalisco", limit=10))
    assert [b.external_id for b in found] == ["denue:9"]
    assert len(calls) == 2
    assert "/BuscarEntidad/dentista/14/1/1000/tok" in calls[0]


@pytest.mark.asyncio
async def test_fetch_needs_a_state():
    p = D.Denue()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))) as c:
        with pytest.raises(RuntimeError):
            await p.fetch(c, {"inegi_denue_token": "t"}, D.DiscoveryQuery(term="x", location="Denver, CO"))


@pytest.mark.asyncio
async def test_a_bad_token_answer_is_an_error_not_an_empty_result():
    p = D.Denue()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text="Token no valido"))) as c:
        with pytest.raises(RuntimeError):
            await p.fetch(c, {"inegi_denue_token": "t"}, D.DiscoveryQuery(term="x", location="Jalisco"))


# ── Discovery stores the email unverified ──


def fake_denue(records):
    class Fake(D.DiscoveryProvider):
        key, label = "fake_denue", "Fake DENUE"

        async def fetch(self, client, env, query):
            return [D.Denue().to_business(r, query) for r in records]
    return Fake()


@pytest.mark.asyncio
async def test_a_directory_email_becomes_an_unverified_inbox(state, config, monkeypatch):
    monkeypatch.setitem(D.PROVIDERS, "fake_denue", fake_denue([record()]))
    report = await D.run_discovery(state, config, "fake_denue",
                                   [D.DiscoveryQuery(term="dentista", location="Jalisco")])
    assert report.new_companies == 1 and report.prospects_added == 1
    prospect = await state.get_prospect_by_email("hola@example.com")
    assert prospect.email_status == "guess" and not prospect.email_verified
    assert prospect.source == "company_inbox"
    waiting = await state.unverified_inboxes()
    assert [w["email"] for w in waiting] == ["hola@example.com"]


@pytest.mark.asyncio
async def test_no_inbox_without_the_signal_confirmed(state, config, monkeypatch):
    await state.set_signal_status("EMAIL_FROM_DIRECTORY", "rejected")
    monkeypatch.setitem(D.PROVIDERS, "fake_denue", fake_denue([record()]))
    report = await D.run_discovery(state, config, "fake_denue",
                                   [D.DiscoveryQuery(term="dentista", location="Jalisco")])
    assert report.new_companies == 1 and report.prospects_added == 0
    assert await state.get_prospect_by_email("hola@example.com") is None


@pytest.mark.asyncio
async def test_do_not_contact_and_duplicates_are_skipped(state, config, monkeypatch):
    import sellbird.config as C
    monkeypatch.setattr(C, "live_do_not_contact", lambda cfg: ["bloqueado@example.org"])
    rows = [record(), record(Id="2", Nombre="OTRO", Correo_e="hola@example.com"),
            record(Id="3", Nombre="TERCERO", Correo_e="bloqueado@example.org")]
    monkeypatch.setitem(D.PROVIDERS, "fake_denue", fake_denue(rows))
    report = await D.run_discovery(state, config, "fake_denue",
                                   [D.DiscoveryQuery(term="dentista", location="Jalisco")])
    assert report.new_companies == 3 and report.prospects_added == 1


@pytest.mark.asyncio
async def test_unverified_inboxes_filters_by_industry(state, config, monkeypatch):
    rows = [record(), record(Id="2", Nombre="VET", Correo_e="vet@example.com",
                             Clase_actividad="Servicios veterinarios para mascotas")]
    monkeypatch.setitem(D.PROVIDERS, "fake_denue", fake_denue(rows))
    await D.run_discovery(state, config, "fake_denue",
                          [D.DiscoveryQuery(term="x", location="Jalisco")])
    assert [w["email"] for w in await state.unverified_inboxes(industry="veterinar")] == ["vet@example.com"]
    await state.update_prospect_email(
        (await state.get_prospect_by_email("vet@example.com")).id, "vet@example.com", "verified")
    assert await state.unverified_inboxes(industry="veterinar") == []


# ── WhatsApp emoji per industry ──


def test_emoji_by_industry_picks_the_matching_emoji_and_falls_back(config):
    from sellbird.config import WhatsAppConfig, whatsapp_link, all_footer_lines
    wa = WhatsAppConfig(number="52 55 0000 0000", emoji="*", message="Hola",
                        emoji_by_industry={"dent": "🦷", "Veterinaria": "🐾"})
    assert wa.emoji_for("Consultorios dentales del sector privado") == "🦷"
    assert wa.emoji_for("Servicios VETERINARIOS para mascotas") == "*"
    assert wa.emoji_for("Clínica veterinaria") == "🐾"
    assert wa.emoji_for("") == "*"
    cfg = config
    cfg.whatsapp = wa
    assert "%F0%9F%A6%B7" in whatsapp_link(cfg, "Consultorio dental")
    assert len([l for l in all_footer_lines(cfg) if "wa.me" in l]) == 3


def test_emoji_by_industry_rejects_multiline_entries():
    from sellbird.config import WhatsAppConfig
    with pytest.raises(Exception):
        WhatsAppConfig(emoji_by_industry={"dent": "a\nb"})

"""Businesses with no website: Google Maps via Apify, public social pages,
and a WhatsApp link under cold email.

All data is fictional (example.* domains, made-up page names, 555 numbers).
No test reaches the network.
"""

import json
import os
import tempfile
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest
import pytest_asyncio

from sellbird.collectors import discover as D
from sellbird.collectors import social as S
from sellbird.config import (
    ComplianceConfig, EnvConfig, SellbirdConfig, WhatsAppConfig,
    compliance_footer_lines, load_config, whatsapp_link,
)
from sellbird.gate import pre_send_check
from sellbird.models import Company
from sellbird.paths import PROJECT_ROOT
from sellbird.signals import SIGNAL_CATALOG, seed_signal_catalog
from sellbird.state import StateManager

NEW_CODES = ("HAS_FACEBOOK_PAGE", "HAS_INSTAGRAM", "EMAIL_FROM_SOCIAL", "SOCIAL_LAST_POST")


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


# ── Signals ──


def test_new_signals_are_in_the_catalog_and_proposed_not_confirmed():
    codes = {s["code"]: s for s in SIGNAL_CATALOG}
    for code in NEW_CODES:
        assert code in codes
    # The paid ones must not be swept in by `--confirm free`.
    for code in ("EMAIL_FROM_SOCIAL", "SOCIAL_LAST_POST"):
        assert not codes[code]["cost_note"].lower().startswith(("free", "included"))


@pytest.mark.asyncio
async def test_seeded_social_signals_start_proposed():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "t.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        rows = {r["code"]: r for r in await sm.get_signal_codes()}
        assert all(rows[c]["status"] == "proposed" for c in NEW_CODES)


# ── Social page links ──


@pytest.mark.parametrize("url, page", [
    ("https://www.facebook.com/gymejemplo/", "https://www.facebook.com/gymejemplo"),
    ("http://m.facebook.com/gymejemplo?ref=page_internal", "https://www.facebook.com/gymejemplo"),
    ("facebook.com/profile.php?id=1000123&sk=about", "https://www.facebook.com/profile.php?id=1000123"),
    ("https://instagram.com/gym.ejemplo/?hl=es", "https://www.instagram.com/gym.ejemplo"),
    ("https://www.facebook.com/sharer/sharer.php?u=x", ""),
    ("https://www.instagram.com/p/AbC123/", ""),
    ("https://www.facebook.com/", ""),
    ("https://gym.example.com/", ""),
])
def test_social_page_url(url, page):
    assert D.social_page_url(url) == page


# ── Apify Google Maps provider ──


def maps_item(**kw):
    item = {"title": "Gimnasio Ejemplo", "placeId": "ChIJexample1",
            "website": "https://gym.example.com/", "phone": "+52 33 5550 0101",
            "address": "Calle Ficticia 1, Guadalajara", "city": "Guadalajara",
            "state": "Jalisco", "categoryName": "Gimnasio", "totalScore": 4.6,
            "reviewsCount": 87, "claimThisBusiness": False,
            "url": "https://www.google.com/maps/place/?q=place_id:ChIJexample1"}
    item.update(kw)
    return item


def test_apify_maps_is_a_listed_provider():
    assert "apify_maps" in D.PROVIDERS
    row = next(m for m in D.provider_menu({}) if m["key"] == "apify_maps")
    assert row["env_keys"] == ["APIFY_TOKEN"]
    assert row["configured"] is False


def test_apify_maps_estimate_is_per_place():
    p = D.ApifyMaps()
    queries = [D.DiscoveryQuery(term="gimnasio", location="Guadalajara", limit=10)] * 2
    assert p.estimate(queries) == pytest.approx(2 * (p.START_COST + 10 * 0.006))
    assert D.ApifyMaps(contact_details=False).estimate(queries[:1]) == pytest.approx(
        p.START_COST + 10 * 0.004)


def test_a_facebook_website_means_no_website_and_keeps_the_page():
    q = D.DiscoveryQuery(term="gimnasio", location="Guadalajara")
    b = D.ApifyMaps().to_business(
        maps_item(website="https://www.facebook.com/gymejemplo/"), q)
    assert b.domain == "" and b.website == "" and b.has_website is False
    assert b.facebook_url == "https://www.facebook.com/gymejemplo"
    assert b.external_id == "gmaps:ChIJexample1"
    assert b.identity() == "gmaps:ChIJexample1"
    assert b.is_claimed is True and b.review_count == 87
    assert b.location == "Guadalajara, Jalisco"


def test_social_links_from_the_site_are_kept_with_the_website():
    q = D.DiscoveryQuery(term="gimnasio")
    b = D.ApifyMaps().to_business(maps_item(
        facebooks=["https://www.facebook.com/sharer.php?u=1", "https://facebook.com/gymejemplo"],
        instagrams=["https://www.instagram.com/gym.ejemplo/"]), q)
    assert b.domain == "gym.example.com" and b.has_website is True
    assert b.facebook_url == "https://www.facebook.com/gymejemplo"
    assert b.instagram_url == "https://www.instagram.com/gym.ejemplo"


def test_closed_and_nameless_places_are_dropped():
    q = D.DiscoveryQuery(term="gimnasio")
    p = D.ApifyMaps()
    assert p.to_business(maps_item(permanentlyClosed=True), q) is None
    assert p.to_business(maps_item(title=""), q) is None


@pytest.mark.asyncio
async def test_fetch_sends_the_token_in_a_header_and_caps_the_charge():
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers.get("authorization")
        seen["query"] = parse_qs(urlparse(str(request.url)).query)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=[maps_item(), maps_item(placeId="ChIJexample2",
                                                                title="Otro Gimnasio")])

    p = D.ApifyMaps()
    p.remaining_budget = 0.05
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await p.fetch(client, {"apify_token": "tok-test"},
                            D.DiscoveryQuery(term="gimnasio", location="Guadalajara", limit=20))
    assert seen["auth"] == "Bearer tok-test"
    assert "token" not in seen["query"]
    # $0.05 buys 8 places at $0.006: never ask for more than the budget pays for.
    assert seen["query"]["maxItems"] == ["8"]
    # Apify's own per-run minimum; maxItems is what keeps it to the budget.
    assert float(seen["query"]["maxTotalChargeUsd"][0]) == pytest.approx(p.MIN_RUN_CHARGE)
    assert seen["body"]["maxCrawledPlacesPerSearch"] == 8
    assert seen["body"]["locationQuery"] == "Guadalajara"
    assert seen["body"]["language"] == "es"
    assert len(out) == 2
    assert p.last_cost == pytest.approx(p.START_COST + 2 * 0.006)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 402, 403, 429])
async def test_refused_or_out_of_credit_stops_the_run(status):
    def handler(request):
        return httpx.Response(status, json={"error": {"message": "token tok-test"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(D.ProviderExhausted) as e:
            await D.ApifyMaps().fetch(client, {"apify_token": "tok-test"},
                                      D.DiscoveryQuery(term="gimnasio", limit=5))
    assert "tok-test" not in str(e.value)


@pytest.mark.asyncio
async def test_no_budget_left_spends_nothing():
    def handler(request):
        raise AssertionError("must not be called")

    p = D.ApifyMaps()
    p.remaining_budget = 0.001
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(D.ProviderExhausted):
            await p.fetch(client, {"apify_token": "t"}, D.DiscoveryQuery(term="x", limit=5))


@pytest.mark.asyncio
async def test_discovery_keeps_a_facebook_only_business_and_records_its_page(
        state, config, monkeypatch):
    class Fake(D.DiscoveryProvider):
        key, label = "fake_maps", "Fake Maps"

        def configured(self, env):
            return True

        async def fetch(self, client, env, query):
            return [D.ApifyMaps().to_business(
                maps_item(website="https://www.facebook.com/gymejemplo"), query)]

    monkeypatch.setitem(D.PROVIDERS, "fake_maps", Fake())
    report = await D.run_discovery(state, config, "fake_maps",
                                   [D.DiscoveryQuery(term="gimnasio", location="Guadalajara")])
    assert report.junk == 0 and report.new_companies == 1
    company = await state.get_company_by_external_id("gmaps:ChIJexample1")
    assert company is not None and company.domain == ""
    obs = await state.get_observations(company_id=company.id, latest_only=True)
    by_code = {o["signal_code"]: o for o in obs}
    assert by_code["HAS_FACEBOOK_PAGE"]["value_text"] == "https://www.facebook.com/gymejemplo"
    assert "NO_WEBSITE" in by_code


# ── Public emails: read, never guessed ──


def test_public_emails_only_takes_what_is_written():
    assert S.public_emails("Escríbenos: Hola@GymEjemplo.example o al 33 5550 0101") == [
        "hola@gymejemplo.example"]
    assert S.public_emails("tu@correo.com", "logo@2x.png", "nombre@dominio.com") == []
    assert S.public_emails("sin correo aquí") == []
    assert S.public_emails(["a@uno.example", "a@uno.example"], "b@dos.example") == [
        "a@uno.example", "b@dos.example"]


def test_facebook_and_instagram_email_fields():
    assert S.facebook_email({"email": "contacto@gym.example"}) == "contacto@gym.example"
    assert S.facebook_email({"info": ["Horario 6-22", "📧 citas@gym.example"]}) == "citas@gym.example"
    assert S.facebook_email({"intro": "Gimnasio en Guadalajara"}) == ""
    assert S.instagram_email({"biography": "Clases 💪 informes: info@gym.example"}) == "info@gym.example"


def test_last_post_is_the_newest_timestamp():
    when = S.last_post_at({"latestPosts": [{"timestamp": "2026-10-01T10:00:00.000Z"},
                                           {"timestamp": "2026-11-01T10:00:00.000Z"},
                                           {"timestamp": "no es fecha"}]})
    assert when.date().isoformat() == "2026-11-01"
    assert S.last_post_at({}) is None


# ── The social stage ──


async def add_social_company(state, name, facebook="", instagram="", phone=""):
    cid = await state.add_company(Company(name=name, domain="", industry="gimnasio",
                                          location="Guadalajara", phone=phone,
                                          external_id=f"gmaps:{name}", source="discover:apify_maps"))
    rows = []
    if facebook:
        rows.append({"company_id": cid, "signal_code": "HAS_FACEBOOK_PAGE", "collector": "discover",
                     "value_num": 1.0, "value_text": facebook})
    if instagram:
        rows.append({"company_id": cid, "signal_code": "HAS_INSTAGRAM", "collector": "discover",
                     "value_num": 1.0, "value_text": instagram})
    await state.add_observations(rows)
    return cid


def apify_handler(calls, facebook_items, instagram_items, status=200):
    def handler(request: httpx.Request):
        path = urlparse(str(request.url)).path
        calls.append((path, json.loads(request.content),
                      parse_qs(urlparse(str(request.url)).query),
                      request.headers.get("authorization")))
        if status != 200:
            return httpx.Response(status)
        if S.FACEBOOK_ACTOR in path:
            return httpx.Response(200, json=facebook_items)
        if S.INSTAGRAM_ACTOR in path:
            return httpx.Response(200, json=instagram_items)
        return httpx.Response(404)
    return handler


@pytest.fixture
def verified(monkeypatch):
    import sellbird.integrations.email_finder as ef
    checked = []

    async def fake_verify(email, env=None):
        checked.append(email)
        return "verified"

    monkeypatch.setattr(ef, "verify_published_address", fake_verify)
    return checked


@pytest.mark.asyncio
async def test_a_published_facebook_email_becomes_a_verified_inbox_prospect(
        state, config, verified):
    cid = await add_social_company(state, "Gym Uno", facebook="https://www.facebook.com/gymuno",
                                   phone="+52 33 5550 0102")
    calls = []
    handler = apify_handler(calls, [{"facebookUrl": "https://www.facebook.com/gymuno/",
                                     "email": "Hola@GymUno.example"}], [])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await S.run_social_profiling(state, config, max_spend=1.0,
                                              env=EnvConfig(apify_token="tok"), client=client)
    assert report.emails_found == 1 and report.prospects_added == 1
    assert verified == ["hola@gymuno.example"]
    assert calls[0][3] == "Bearer tok"
    assert calls[0][1] == {"startUrls": [{"url": "https://www.facebook.com/gymuno"}]}
    prospect = await state.get_prospect_by_email("hola@gymuno.example")
    assert prospect.is_company_inbox and prospect.email_status == "verified"
    assert prospect.source_url == "https://www.facebook.com/gymuno"
    assert prospect.company_id == cid and prospect.phone == "+52 33 5550 0102"
    obs = await state.get_observations(company_id=cid, signal_code="EMAIL_FROM_SOCIAL")
    assert obs[0]["value_text"] == "hola@gymuno.example"
    assert obs[0]["evidence_url"] == "https://www.facebook.com/gymuno"
    # Read once: the next run has nothing to pay for.
    assert await state.companies_needing_social() == []


@pytest.mark.asyncio
async def test_no_published_email_means_no_prospect_and_no_guess(state, config, verified):
    cid = await add_social_company(state, "Gym Dos", facebook="https://www.facebook.com/gymdos")
    handler = apify_handler([], [{"facebookUrl": "https://www.facebook.com/gymdos",
                                  "intro": "El mejor gym de la zona"}], [])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await S.run_social_profiling(state, config, max_spend=1.0,
                                              env=EnvConfig(apify_token="tok"), client=client)
    assert report.prospects_added == 0 and verified == []
    assert await state.get_contacts_for_company(cid) == []
    obs = await state.get_observations(company_id=cid, signal_code="EMAIL_FROM_SOCIAL")
    assert obs[0]["value_num"] == 0.0 and obs[0]["value_text"] == ""


@pytest.mark.asyncio
async def test_instagram_is_the_fallback_and_records_the_last_post(state, config, verified):
    cid = await add_social_company(state, "Gym Tres", facebook="https://www.facebook.com/gymtres",
                                   instagram="https://www.instagram.com/gym.tres/")
    calls = []
    handler = apify_handler(
        calls, [{"facebookUrl": "https://www.facebook.com/gymtres"}],
        [{"username": "gym.tres", "biography": "Informes: tres@gym.example",
          "latestPosts": [{"timestamp": "2026-11-01T10:00:00Z"}]}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await S.run_social_profiling(state, config, max_spend=1.0,
                                              env=EnvConfig(apify_token="tok"), client=client)
    assert [c[1] for c in calls][1] == {"usernames": ["gym.tres"]}
    assert report.prospects_added == 1
    prospect = await state.get_prospect_by_email("tres@gym.example")
    assert prospect.source_url == "https://www.instagram.com/gym.tres"
    last = await state.get_observations(company_id=cid, signal_code="SOCIAL_LAST_POST")
    assert last[0]["value_text"] == "2026-11-01"


@pytest.mark.asyncio
async def test_nothing_is_read_until_email_from_social_is_confirmed(state, config):
    await state.set_signal_status("EMAIL_FROM_SOCIAL", "proposed")
    await add_social_company(state, "Gym Cuatro", facebook="https://www.facebook.com/gymcuatro")

    def handler(request):
        raise AssertionError("no Apify call before the signal is confirmed")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await S.run_social_profiling(state, config, env=EnvConfig(apify_token="tok"),
                                              client=client)
    assert report.stopped == "EMAIL_FROM_SOCIAL is not confirmed"


@pytest.mark.asyncio
async def test_over_the_cap_spends_nothing(state, config):
    for i in range(5):
        await add_social_company(state, f"Gym {i}", facebook=f"https://www.facebook.com/gym{i}")

    def handler(request):
        raise AssertionError("over budget: must not be called")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await S.run_social_profiling(state, config, max_spend=0.01,
                                              env=EnvConfig(apify_token="tok"), client=client)
    assert report.stopped == "over budget"
    assert report.estimated_cost == pytest.approx(S.START_COST + 5 * S.FACEBOOK_PAGE_COST)


@pytest.mark.asyncio
async def test_a_failed_read_is_retried_later_not_marked_empty(state, config):
    await add_social_company(state, "Gym Cinco", facebook="https://www.facebook.com/gymcinco")
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            apify_handler([], [], [], status=402))) as client:
        report = await S.run_social_profiling(state, config, max_spend=1.0,
                                              env=EnvConfig(apify_token="tok"), client=client)
    assert any("402" in e for e in report.errors)
    assert len(await state.companies_needing_social()) == 1


@pytest.mark.asyncio
async def test_do_not_contact_holds_for_social_emails(state, verified, tmp_path):
    cfg = load_config(str(PROJECT_ROOT / "sellbird.yaml"))
    cfg.compliance = ComplianceConfig(do_not_contact=["gymseis.example"])
    await add_social_company(state, "Gym Seis", facebook="https://www.facebook.com/gymseis")
    handler = apify_handler([], [{"facebookUrl": "https://www.facebook.com/gymseis",
                                  "email": "hola@gymseis.example"}], [])
    import sellbird.config as C
    original = C.live_do_not_contact
    C.live_do_not_contact = lambda config: list(config.compliance.do_not_contact)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            report = await S.run_social_profiling(state, cfg, max_spend=1.0,
                                                  env=EnvConfig(apify_token="tok"), client=client)
    finally:
        C.live_do_not_contact = original
    assert report.emails_found == 1 and report.prospects_added == 0 and verified == []


# ── WhatsApp link and the Spanish opt-out ──


OPT_OUT_ES = "Si no te interesa, responde BAJA y no te vuelvo a escribir."


def mx_config(**wa):
    base = load_config(str(PROJECT_ROOT / "sellbird.yaml")).model_dump()
    base["compliance"]["opt_out_line"] = OPT_OUT_ES
    base["whatsapp"] = {"number": "+52 33 5550 0199", "emoji": "⚡",
                        "message": "Hola, vi tu correo y me interesa saber más",
                        "label": "¿Prefieres WhatsApp? Escríbeme aquí:", **wa}
    return SellbirdConfig(**base)


def test_whatsapp_link_is_digits_and_an_encoded_message():
    link = whatsapp_link(mx_config())
    assert link.startswith("https://wa.me/523355500199?text=")
    text = unquote(urlparse(link).query.removeprefix("text="))
    assert text == "⚡ Hola, vi tu correo y me interesa saber más"
    assert " " not in link


def test_no_number_means_no_link():
    assert whatsapp_link(load_config(str(PROJECT_ROOT / "sellbird.yaml"))) == ""
    assert all("wa.me" not in line for line in compliance_footer_lines(load_config(str(PROJECT_ROOT / "sellbird.yaml"))))


@pytest.mark.parametrize("bad", ["123", "52 33 5550 0199 000 000 0"])
def test_a_number_without_country_code_is_refused(bad):
    with pytest.raises(ValueError):
        WhatsAppConfig(number=bad)


def test_footer_ends_with_opt_out_then_whatsapp():
    lines = compliance_footer_lines(mx_config())
    assert lines[-2] == OPT_OUT_ES
    assert lines[-1].startswith("¿Prefieres WhatsApp? Escríbeme aquí: https://wa.me/523355500199")


def test_the_whatsapp_line_fits_the_one_link_budget():
    cfg = mx_config()
    body = "Hola, vi que su gimnasio atiende por Facebook.\n\n" + "\n".join(
        compliance_footer_lines(cfg))
    gate = pre_send_check("hola@gym.example", "Una pregunta", body, kind="reply")
    assert not any("links" in r for r in gate.reasons), gate.reasons


def test_the_spanish_opt_out_line_is_accepted():
    assert ComplianceConfig(opt_out_line=OPT_OUT_ES).opt_out_line == OPT_OUT_ES


@pytest.mark.parametrize("reply", ["BAJA", "baja", "Baja, gracias", "Por favor dame de baja"])
def test_replying_baja_is_an_opt_out(reply):
    from sellbird.agents.handler import _opt_out_reason
    assert _opt_out_reason(reply, reply) is not None

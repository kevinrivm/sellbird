"""Reach businesses that have no website through their public social pages.

Many local businesses have a Google Maps listing and a Facebook page or
Instagram profile, and nothing else. The website profile can't read them, so
Scout never finds an address and they never get an email. This stage reads
only what those pages show to anyone — no login, no cookies, no account of
yours — through Apify actors, and takes the contact email the business
itself publishes there. An address is never guessed: if the page shows none,
the business stays unreachable by email.

Every address found is verified like an address printed on a company's own
site (``verify_published_address``) and carries its origin: the prospect's
``source_url`` is the page, and an ``EMAIL_FROM_SOCIAL`` observation records
the address with the page as evidence.

Reading pages costs Apify credit, so like discovery this stage estimates
first, caps spend, and only runs on request (``sellbird social``).
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from sellbird.collectors.discover import social_kind, social_page_url

logger = logging.getLogger("sellbird.collectors.social")

API = "https://api.apify.com/v2"
FACEBOOK_ACTOR = "apify~facebook-pages-scraper"
INSTAGRAM_ACTOR = "apify~instagram-profile-scraper"
START_COST = 0.00005
FACEBOOK_PAGE_COST = 0.012
INSTAGRAM_PROFILE_COST = 0.0026
MIN_RUN_CHARGE = 0.50
RUN_TIMEOUT = httpx.Timeout(connect=10.0, read=330.0, write=10.0, pool=10.0)

# What a page names when it shows an address it doesn't own: a template,
# a platform's own address, or an image file name that looks like one.
_EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@"
                       r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24})(?![\w-])")
_NOT_THEIRS = re.compile(
    r"(?i)@(?:example\.|test\.|domain\.|email\.com$|correo\.com$|tudominio|yourdomain|"
    r"facebook\.com$|fb\.com$|instagram\.com$|meta\.com$|sentry|wixpress\.com$)"
    r"|^(?:noreply|no-reply|donotreply|nombre|name|tu|your|usuario|user)@")
_FILE_END = re.compile(r"(?i)\.(?:png|jpe?g|gif|webp|svg|js|css)$")


def public_emails(*texts) -> list[str]:
    """Addresses written out in the given text, in order, lowercased.

    Only what is literally there: nothing is completed, repaired or guessed.
    """
    out: list[str] = []
    for text in texts:
        if isinstance(text, (list, tuple)):
            for found in public_emails(*text):
                if found not in out:
                    out.append(found)
            continue
        if not isinstance(text, str) or "@" not in text:
            continue
        for m in _EMAIL_RE.finditer(text):
            email = m.group(1).strip(".").lower()
            if _NOT_THEIRS.search(email) or _FILE_END.search(email):
                continue
            if email not in out:
                out.append(email)
    return out


def facebook_email(item: dict) -> str:
    """The contact email a Facebook page shows, or ""."""
    fields = [item.get(k) for k in ("email", "emails", "contactEmail")]
    fields += [item.get(k) for k in ("info", "intro", "about", "description")]
    found = public_emails(*fields)
    return found[0] if found else ""


def instagram_email(item: dict) -> str:
    """The contact email an Instagram profile shows (its contact button or
    its bio), or ""."""
    fields = [item.get(k) for k in ("businessEmail", "publicEmail", "email")]
    fields.append(item.get("biography"))
    found = public_emails(*fields)
    return found[0] if found else ""


def last_post_at(item: dict) -> datetime | None:
    """When the profile last posted, from its latest posts."""
    newest = None
    for post in item.get("latestPosts") or []:
        if not isinstance(post, dict):
            continue
        raw = post.get("timestamp")
        if not isinstance(raw, str):
            continue
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if newest is None or when > newest:
            newest = when
    return newest


def instagram_username(url: str) -> str:
    page = social_page_url(url)
    if social_kind(page) != "instagram":
        return ""
    return page.rstrip("/").rsplit("/", 1)[-1].lower()


def _page_key(url: str) -> str:
    """The same page however a link spells it."""
    page = social_page_url(url or "")
    return page.lower().rstrip("/") if page else ""


# Words that say what a business is, not which one it is: a page that only
# shares these with the name ("Gym", "Fitness Center") could be anyone's.
_GENERIC_NAME_WORDS = frozenset("""
    gym gimnasio gimnasios fitness center centro club studio estudio sport sports
    deportivo deportiva the el la los las de del y and en sucursal branch box crossfit
    clinica clínica dental consultorio spa salon salón estetica estética restaurante
    cafe café bar tienda shop store taller escuela academia instituto grupo company
    sa cv rl mx com oficial official
""".split())


def _fold(text: str) -> str:
    import unicodedata
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def name_tokens(name: str) -> list[str]:
    """The distinctive words of a business name, accent- and case-folded."""
    words = re.findall(r"[a-z0-9]+", _fold(name))
    return [w for w in words if len(w) >= 3 and w not in _GENERIC_NAME_WORDS
            and not w.isdigit()]


def page_matches_business(url: str, snippet: str, name: str, city: str = "") -> bool:
    """Is this search hit the business's own page?

    A distinctive word of its name must be in the page address (where a
    page names itself) or, with its city, in the result text. A page that
    only shares generic words ("gym", "fitness") is never taken: a wrong
    page would mean writing to the wrong business.
    """
    tokens = name_tokens(name)
    if not tokens:
        return False
    slug = re.sub(r"[^a-z0-9]", "", _fold(urlparse(url).path))
    text = _fold(snippet)
    city_word = (name_tokens(city.split(",")[0]) or [""])[0] if city else ""
    city_seen = bool(city_word) and (city_word in text or city_word in slug)
    in_slug = [t for t in tokens if len(t) >= 4 and t in slug]
    # Two distinctive words in the address are specific enough on their own.
    # One ("Community Gym") is shared by businesses in every city, so it
    # also needs the city in the address or the result text.
    if len(set(in_slug)) >= 2 or (len(tokens) == 1 and in_slug and city_seen):
        return True
    hits = sum(1 for t in tokens if re.search(rf"\b{re.escape(t)}\b", text))
    return hits >= min(2, len(tokens)) and (not city_word or city_word in text)


async def find_social_pages(state, config, limit: int = 50, env=None, search=None) -> dict:
    """Look up the Facebook/Instagram page of businesses that have no website
    and no page on their listing, with Sellbird's free web search.

    Free (no paid API), slow on purpose (polite search delays), and strict
    about matching: see page_matches_business. Found pages are recorded as
    HAS_FACEBOOK_PAGE / HAS_INSTAGRAM with collector ``social_search`` and a
    lower confidence than a link the listing itself gave.
    """
    from sellbird.config import load_env
    from sellbird.signals import seed_signal_catalog

    await seed_signal_catalog(state)
    confirmed = await state.confirmed_signal_codes()
    wanted = {"HAS_FACEBOOK_PAGE", "HAS_INSTAGRAM"} & confirmed
    result = {"searched": 0, "facebook": 0, "instagram": 0}
    if not wanted:
        return result
    companies = await state.companies_needing_social_search(limit=limit)
    if not companies:
        return result
    if search is None:
        from sellbird.agents.scout import Scout
        scout = Scout(None, state, config, env or load_env())

        async def search(query: str):
            scout._queries_this_cycle = 0   # our own budget: `limit` businesses
            return await scout._web_search(query)

    for company in companies:
        name, city = company.get("name") or "", company.get("location") or ""
        if not name_tokens(name):
            await state.add_observations([_searched(company["id"])])
            continue
        result["searched"] += 1
        place = city.split(",")[0].strip()
        found: dict[str, str] = {}
        answered = False
        for query in (f'"{name}" {place} facebook', f'"{name}" {place} instagram'):
            try:
                hits = await search(query)
            except Exception as e:
                logger.debug(f"social search failed for {name}: {e}")
                hits = []
            answered = answered or bool(hits)
            for url, snippet in hits or []:
                page = social_page_url(url)
                kind = social_kind(page)
                if page and kind not in found and page_matches_business(url, snippet, name, city):
                    found[kind] = page
            if "facebook" in found:
                break   # Facebook is the one that carries contact emails
        if not answered:
            # No results at all is a blocked or rate-limited search, not "no
            # page": leave the business to be looked up on a later run.
            result["searched"] -= 1
            result["unanswered"] = result.get("unanswered", 0) + 1
            continue
        rows = [_searched(company["id"])]
        for kind, code in (("facebook", "HAS_FACEBOOK_PAGE"), ("instagram", "HAS_INSTAGRAM")):
            if kind in found and code in wanted:
                rows.append({"company_id": company["id"], "signal_code": code,
                             "collector": "social_search", "value_num": 1.0,
                             "value_text": found[kind], "confidence": 0.7,
                             "evidence_url": found[kind]})
                result[kind] += 1
        await state.add_observations(rows)
    return result


def _searched(company_id: str) -> dict:
    """The marker that a business's pages were looked for (found or not)."""
    return {"company_id": company_id, "signal_code": "HAS_FACEBOOK_PAGE",
            "collector": "social_search", "value_num": 0.0, "value_text": "",
            "confidence": 1.0, "evidence_url": ""}


@dataclass
class SocialReport:
    companies: int = 0
    facebook_pages: int = 0
    instagram_profiles: int = 0
    estimated_cost: float = 0.0
    actual_cost: float = 0.0
    emails_found: int = 0
    prospects_added: int = 0
    by_status: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    stopped: str = ""


def plan(companies: list[dict]) -> tuple[list[str], list[str]]:
    """(Facebook pages, Instagram profiles) a run may read: the spending
    ceiling the estimate is built from. Instagram is read up front only when
    it is a business's one page or SOCIAL_LAST_POST is wanted; otherwise it
    is the fallback for a Facebook page that shows no email."""
    facebook, instagram = [], []
    for c in companies:
        fb = social_page_url(c.get("facebook_url") or "")
        ig = social_page_url(c.get("instagram_url") or "")
        if fb and social_kind(fb) == "facebook" and fb not in facebook:
            facebook.append(fb)
        if ig and social_kind(ig) == "instagram" and ig not in instagram:
            instagram.append(ig)
    return facebook, instagram


def estimate(facebook: list[str], instagram: list[str]) -> float:
    cost = 0.0
    if facebook:
        cost += START_COST + FACEBOOK_PAGE_COST * len(facebook)
    if instagram:
        cost += START_COST + INSTAGRAM_PROFILE_COST * len(instagram)
    return cost


class ApifyError(RuntimeError):
    pass


async def run_actor(client: httpx.AsyncClient, token: str, actor: str, body: dict,
                    max_items: int, max_charge: float) -> list[dict]:
    """Run an Apify actor and return its dataset items.

    ``maxTotalChargeUsd`` makes Apify itself stop charging at the cap, so a
    run can't spend past it even if its pricing changed since the estimate.
    """
    params = {"maxItems": max(1, int(max_items))}
    if math.isfinite(max_charge):
        # Apify refuses a per-run cap under an actor's minimum; maxItems (one
        # per page asked for) is what bounds the spend to the estimate.
        params["maxTotalChargeUsd"] = f"{max(max_charge, MIN_RUN_CHARGE):.4f}"
    try:
        r = await client.post(f"{API}/acts/{actor}/run-sync-get-dataset-items",
                              params=params, json=body, timeout=RUN_TIMEOUT,
                              headers={"Authorization": f"Bearer {token}",
                                       "Content-Type": "application/json"})
    except httpx.HTTPError:
        raise ApifyError("Apify: request failed; try again later") from None
    # Never log response bodies: an upstream error may echo the token.
    if r.status_code in (401, 402, 403, 429):
        reasons = {401: "invalid API token", 402: "out of credit",
                   403: "access refused", 429: "rate limit reached"}
        raise ApifyError(f"Apify {r.status_code}: {reasons[r.status_code]}")
    if r.status_code >= 400:
        raise ApifyError(f"Apify {r.status_code}: request failed")
    try:
        items = r.json()
    except ValueError:
        raise ApifyError("Apify: invalid JSON response") from None
    if not isinstance(items, list):
        raise ApifyError("Apify: invalid response")
    return [i for i in items if isinstance(i, dict)]


def _facebook_item_page(item: dict) -> str:
    for key in ("facebookUrl", "pageUrl", "url", "inputUrl"):
        page = _page_key(item.get(key) or "")
        if page:
            return page
    return ""


async def run_social_profiling(state, config, max_spend: float = 0.50, limit: int = 25,
                               dry_run: bool = False, env=None,
                               client: httpx.AsyncClient | None = None) -> SocialReport:
    """Read the public pages of businesses with no website, record what they
    publish, and add their published inbox as a prospect.
    """
    from sellbird.config import live_do_not_contact, load_env
    from sellbird.gate import do_not_contact_match
    from sellbird.integrations.email_finder import verify_published_address
    from sellbird.models.prospect import COMPANY_INBOX_SOURCE, Prospect
    from sellbird.signals import seed_signal_catalog

    report = SocialReport()
    await seed_signal_catalog(state)
    confirmed = await state.confirmed_signal_codes()
    companies = await state.companies_needing_social(limit=limit)
    report.companies = len(companies)
    facebook, instagram = plan(companies)
    want_last_post = "SOCIAL_LAST_POST" in confirmed
    report.facebook_pages, report.instagram_profiles = len(facebook), len(instagram)
    report.estimated_cost = estimate(facebook, instagram)
    if dry_run or not companies:
        return report

    if "EMAIL_FROM_SOCIAL" not in confirmed:
        report.stopped = "EMAIL_FROM_SOCIAL is not confirmed"
        report.errors.append("confirm EMAIL_FROM_SOCIAL first (dashboard → Signals, or "
                             "`sellbird signals --confirm EMAIL_FROM_SOCIAL`): reading "
                             "pages costs credit, so nothing runs until you choose it")
        return report
    env = env or load_env()
    token = getattr(env, "apify_token", "") or ""
    if not token:
        report.stopped = "not configured"
        report.errors.append("APIFY_TOKEN is empty in .env")
        return report
    if report.estimated_cost > max_spend:
        report.stopped = "over budget"
        report.errors.append(f"estimated ${report.estimated_cost:.2f} exceeds the "
                             f"${max_spend:.2f} cap — raise the cap or lower --limit")
        return report

    run_id = await state.start_run("social", provider="apify",
                                   params={"companies": len(companies),
                                           "estimate": report.estimated_cost})
    fb_items: dict[str, dict] = {}
    ig_items: dict[str, dict] = {}
    read_ok = {"facebook": False, "instagram": False}
    own_client = client is None
    client = client or httpx.AsyncClient(follow_redirects=True)
    try:
        if facebook:
            try:
                items = await run_actor(
                    client, token, FACEBOOK_ACTOR,
                    {"startUrls": [{"url": u} for u in facebook]},
                    max_items=len(facebook), max_charge=max_spend - report.actual_cost)
                report.actual_cost += START_COST + FACEBOOK_PAGE_COST * min(len(items), len(facebook))
                for item in items:
                    page = _facebook_item_page(item)
                    if page:
                        fb_items[page] = item
                read_ok["facebook"] = True
            except ApifyError as e:
                report.errors.append(f"Facebook: {e}")
        names = []
        for c in companies:
            name = instagram_username(c.get("instagram_url") or "")
            if not name or name in names:
                continue
            fb_page = _page_key(c.get("facebook_url") or "")
            fb_item = fb_items.get(fb_page) if fb_page else None
            if want_last_post or not fb_page or not read_ok["facebook"] \
                    or fb_item is None or not facebook_email(fb_item):
                names.append(name)
        if names and report.actual_cost < max_spend:
            try:
                items = await run_actor(
                    client, token, INSTAGRAM_ACTOR, {"usernames": names},
                    max_items=len(names), max_charge=max_spend - report.actual_cost)
                report.actual_cost += START_COST + INSTAGRAM_PROFILE_COST * min(len(items), len(names))
                for item in items:
                    name = str(item.get("username") or "").lower()
                    if name:
                        ig_items[name] = item
                read_ok["instagram"] = True
            except ApifyError as e:
                report.errors.append(f"Instagram: {e}")
    finally:
        if own_client:
            await client.aclose()

    blocked_list = live_do_not_contact(config)
    now = datetime.now(timezone.utc)
    for company in companies:
        fb_page = _page_key(company.get("facebook_url") or "")
        ig_name = instagram_username(company.get("instagram_url") or "")
        fb = fb_items.get(fb_page) if fb_page else None
        ig = ig_items.get(ig_name) if ig_name else None
        # A page whose read failed is retried next run, not marked "no email".
        attempted = (fb_page and read_ok["facebook"]) or (ig_name and read_ok["instagram"])
        if not attempted:
            continue

        email, page = "", ""
        if fb is not None:
            email, page = facebook_email(fb), social_page_url(company["facebook_url"])
        if not email and ig is not None:
            email, page = instagram_email(ig), social_page_url(company["instagram_url"])
        page = page or social_page_url(company.get("facebook_url") or company.get("instagram_url") or "")

        rows = [{
            "company_id": company["id"], "signal_code": "EMAIL_FROM_SOCIAL",
            "collector": "social", "value_num": 1.0 if email else 0.0,
            "value_text": email, "confidence": 1.0, "evidence_url": page,
            "run_id": run_id,
        }]
        posted = last_post_at(ig) if ig is not None else None
        if posted is not None and "SOCIAL_LAST_POST" in confirmed:
            rows.append({
                "company_id": company["id"], "signal_code": "SOCIAL_LAST_POST",
                "collector": "social", "value_num": float(max(0, (now - posted).days)),
                "value_text": posted.date().isoformat(), "confidence": 1.0,
                "evidence_url": social_page_url(company["instagram_url"]), "run_id": run_id,
            })
        await state.add_observations(rows, run_id=run_id)
        if not email:
            continue
        report.emails_found += 1
        if do_not_contact_match(email, blocked_list):
            continue
        if await state.get_prospect_by_email(email):
            continue
        try:
            status = await verify_published_address(email, env=env)
        except Exception as e:
            logger.debug(f"social: verification failed for {email}: {e}")
            status = "guess"
        report.by_status[status] = report.by_status.get(status, 0) + 1
        where = "Facebook" if social_kind(page) == "facebook" else "Instagram"
        prospect = Prospect(
            email=email, email_status=status, email_verified=(status == "verified"),
            company=company.get("name") or "", company_id=company["id"],
            title="General inbox", industry=company.get("industry") or "",
            phone=company.get("phone") or "",
            source=COMPANY_INBOX_SOURCE, source_url=page,
            personalization_notes=(f"No website. Contact email published on their "
                                   f"{where} page."),
        )
        if prospect.is_valid():
            await state.add_prospect(prospect)
            report.prospects_added += 1

    await state.finish_run(run_id, status="completed", records=report.emails_found,
                           cost_usd=report.actual_cost)
    return report

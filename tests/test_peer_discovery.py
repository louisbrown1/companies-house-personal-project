import json

import pytest

from agent_ui import peer_discovery as discovery
from agent_ui.peer_discovery_models import (
    BusinessFacet,
    PageSource,
    ProfileDraft,
    ProfileSelection,
)


def page(source_id="p1", number="12345678", kind="page_content"):
    return PageSource(
        source_id=source_id,
        url="https://examplebank.co.uk/products",
        kind=kind,
        retrieved_at="2026-11-13",
        text=f"Example bank company number {number}. We offer buy-to-let mortgages for landlords across the UK. We provide personal savings accounts for individuals.",
    )


def draft(source_id="p1"):
    quote = "We offer buy-to-let mortgages for landlords across the UK."
    return ProfileDraft(
        facets=[
            BusinessFacet(source_id=source_id, quote=quote, code=code)
            for code in ["buy_to_let", "landlords", "uk"]
        ]
    )


@pytest.mark.asyncio
async def test_complete_profile_targets_missing_subject_customer_overlap_and_preserves_evidence(
    monkeypatch,
):
    from types import SimpleNamespace

    p = page()
    p.text += " We provide business loans to SMEs in the UK."

    def profile(codes):
        options = discovery.profile_options([p], "bank")
        facets = []
        for code in codes:
            facets.append(next(f for f in options.values() if f.code == code))
        return discovery.validated_profile(
            ProfileDraft(facets=facets), "Example bank", "12345678", [p]
        )

    subject = profile(["savings", "individuals", "uk"])
    responses = iter([profile(["savings", "smes", "uk"]), profile(["individuals"])])

    async def model(*args):
        return next(responses)

    monkeypatch.setattr(discovery, "profile_from_pages", model)
    queries = []

    async def search(query, **kwargs):
        queries.append(query)
        return [p]

    async def extract(sources):
        return sources

    web = SimpleNamespace(search=search, extract_pages=extract)
    result = await discovery.complete_profile(
        web, "Example bank", "12345678", [p], "", "bank", subject
    )
    assert "personal savings" in queries[0]
    assert {f.code for f in result.facets} == {"savings", "smes", "uk", "individuals"}


def test_profile_requires_exact_page_quotes_and_registry_binding():
    profile = discovery.validated_profile(draft(), "Example bank", "12345678", [page()])
    assert len(profile.facets) == 3
    assert "12345678" in profile.identity_quotes[0].quote
    with pytest.raises(ValueError):
        discovery.validated_profile(draft(), "Example bank", "87654321", [page()])
    with pytest.raises(ValueError):
        discovery.validated_profile(
            draft(), "Example bank", "12345678", [page(kind="search_snippet")]
        )
    bad = draft()
    bad.facets[0].quote = "We are the best bank in the world."
    assert len(discovery.validated_profile(bad, "Example bank", "12345678", [page()]).facets) == 2


def test_brand_list_cannot_become_product_evidence():
    p = page()
    p.text += " Alpha and Beta are trading names of Example Bank."
    raw = ProfileDraft(
        facets=[
            BusinessFacet(
                source_id="p1",
                quote="Alpha and Beta are trading names of Example Bank.",
                code="consumer_credit",
            )
        ]
    )
    profile = discovery.validated_profile(raw, "Example bank", "12345678", [p])
    assert profile.facets == []


def test_phone_number_and_registered_office_are_not_business_evidence():
    assert discovery.identity_quote("Call us on 12345678 for assistance.", "12345678") is None
    assert discovery.identity_quote("Registered in England No. 388466", "00388466")
    p = page()
    p.text += " Our registered office is in the UK."
    raw = ProfileDraft(
        facets=[
            BusinessFacet(source_id="p1", quote="Our registered office is in the UK.", code="uk")
        ]
    )
    assert discovery.validated_profile(raw, "Example bank", "12345678", [p]).facets == []


def test_all_three_business_dimensions_must_overlap():
    a = discovery.validated_profile(draft(), "Subject", "12345678", [page()])
    b = a.model_copy(deep=True)
    b.facets = [f for f in b.facets if f.code != "landlords"]
    shared, missing = discovery.matching_facets(a, b)
    assert "customers" in missing and "buy_to_let" in shared


def test_bank_options_exclude_biographies_indirect_lending_and_brand_lists():
    p = page()
    p.text += (
        " Our distribution partners offer support."
        " Alex has 20 years of experience in consumer credit."
        " Home Loans is a trading name of Another Company."
        " Our warehouse financing supports SME lenders."
        " Mortgage Lenders Association: an industry body for mortgage lenders."
    )
    options = discovery.profile_options([p], "bank")
    assert {v.code for v in options.values()} <= {
        "buy_to_let",
        "mortgages",
        "landlords",
        "uk",
        "savings",
        "individuals",
    }
    assert all("industry body" not in v.quote for v in options.values())


def test_candidate_names_and_quotes_are_copied_and_subject_aliases_excluded():
    p = page()
    p.text = "OurBrand and OtherBrand are trading names of Subject Limited."
    aliases = discovery.subject_aliases("Subject Limited", [p])
    p.text += " Peerbank offers mortgages. OurBrand offers savings."
    assert "our brand" in aliases and "other brand" in aliases
    proposal = discovery.grounded_candidate("Peerbank", "p1", [p])
    assert proposal.name in proposal.quote and proposal.quote in p.text
    with pytest.raises(ValueError):
        discovery.grounded_candidate("Invented Bank", "p1", [p])


def test_negative_activity_is_not_a_supported_facet():
    p = page()
    p.text += " We no longer offer personal loans."
    raw = ProfileDraft(
        facets=[
            BusinessFacet(
                source_id="p1", quote="We no longer offer personal loans.", code="consumer_credit"
            )
        ]
    )
    assert not discovery.validated_profile(raw, "Example", "12345678", [p]).facets


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:secret@example.com",
        "https://127.0.0.1",
        "https://localhost",
        "https://10.0.0.1",
        "https://example.com:8080",
    ],
)
def test_unsafe_page_urls_rejected(url):
    assert not discovery.public_url(url)


def test_page_aliases_preserve_host_path_and_query_boundaries():
    assert discovery.page_key("https://www.example.com/") == discovery.page_key(
        "https://example.com"
    )
    assert discovery.page_key("https://example.com/legal") != discovery.page_key(
        "https://evil.example.com/legal"
    )
    assert discovery.page_key("https://example.com/legal?a=1") != discovery.page_key(
        "https://example.com/legal?a=2"
    )
    assert discovery.page_key("http://example.com/legal") is None


@pytest.mark.asyncio
async def test_no_key_has_no_sic_fallback(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    async def registry(*args):
        pytest.fail("No fallback registry discovery")

    result = await discovery.discover_web_peers({}, registry, "")
    assert result.status == "unavailable" and "No SIC-only" in result.gaps[0]


@pytest.mark.asyncio
async def test_issuer_search_retries_when_results_are_only_directories():
    audit = discovery.DiscoveryAudit()

    class Research(discovery.TavilyResearch):
        async def search(self, query, *, pages=False):
            audit.searches.append(query)
            if "official company registration" in query:
                return [page().model_copy(update={"url": "https://directory.example.org/bank"})]
            return [page()]

        async def extract_pages(self, sources):
            return sources

    result = await Research(None, audit).issuer_pages("Example Bank", "12345678")
    assert any("terms conditions" in q for q in audit.searches)
    assert any(discovery.identity_quote(s.text, "12345678") for s in result)
    assert all(discovery.source_host(s.url) == "examplebank.co.uk" for s in result)


def test_quotes_from_unbound_hosts_cannot_supply_business_facets():
    foreign = page("foreign").model_copy(update={"url": "https://anotherbank.co.uk/"})
    foreign.text = "We offer buy-to-let mortgages for landlords across the UK."
    result = discovery.validated_profile(draft("foreign"), "Example", "12345678", [page(), foreign])
    assert result.facets == []


@pytest.mark.asyncio
async def test_unpadded_issuer_number_allows_product_page_discovery():
    audit = discovery.DiscoveryAudit()

    class Research(discovery.TavilyResearch):
        async def search(self, query, *, pages=False):
            audit.searches.append(query)
            return [page(number="388466")]

        async def extract_pages(self, sources):
            return sources

    await Research(None, audit).issuer_pages("Example Bank")
    assert any(q.startswith("site:") for q in audit.searches)


def test_grounded_candidate_preserves_explicit_bank_suffix():
    source = page()
    source.text = "Example Bank offers mortgages and savings."
    assert discovery.grounded_candidate("Example", "p1", [source]).name == "Example Bank"


def test_exact_registry_hint_never_selects_a_parent_or_first_result():
    candidates = [
        {"title": "Example Banking Group PLC", "company_number": "11111111"},
        {"title": "Example Bank PLC", "company_number": "22222222"},
    ]
    assert discovery.exact_registry_candidate("Example Bank", candidates) == "22222222"
    assert discovery.exact_registry_candidate("Example", candidates) is None


def test_long_issuer_page_preserves_target_registration_binding():
    research = discovery.TavilyResearch(None, discovery.DiscoveryAudit())
    research.identity_number = "12345678"
    text = (
        "Product description. " * 250
        + "Example Bank company number 12345678. "
        + "Other information. " * 350
    )
    source = research.source("https://examplebank.co.uk/legal", text, "page_content")
    assert len(source.text) <= 6000
    assert discovery.identity_quote(source.text, "12345678")


@pytest.mark.asyncio
@pytest.mark.parametrize("first_batch", ["empty", "failed"])
async def test_candidate_batches_recover_after_empty_batch_and_reject_invented_names(
    monkeypatch, first_batch
):
    sources = [page(f"p{i}") for i in range(4)]
    sources[2].text = "Peerbank offers mortgages and savings."
    calls = 0

    async def model(schema, prompt):
        nonlocal calls
        calls += 1
        if calls == 1:
            if first_batch == "failed":
                raise ValueError("Invalid model JSON")
            return schema(candidates=[])
        return schema(
            candidates=[
                {"name": "Invented Bank", "source_id": "p2"},
                {"name": "Peerbank", "source_id": "p2"},
            ]
        )

    monkeypatch.setattr(discovery, "ask_model", model)
    audit = discovery.DiscoveryAudit()
    results = await discovery.propose_candidates(
        "Subject", {"subject"}, {"savings"}, sources, audit
    )
    assert [r.name for r in results] == ["Peerbank"]
    assert audit.candidates[0].status == "excluded"


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_brand_resolution_uses_page_crn_not_registry_top_hit(monkeypatch, ambiguous):
    monkeypatch.setenv("TAVILY_API_KEY", "test")
    subject = page("subject", "12345678")
    peer = page("peer", "87654321")
    if ambiguous:
        peer.text += " Parent company number 11111111."
    lead = PageSource(
        source_id="lead",
        url="https://news.example.com/peers",
        kind="search_snippet",
        text="Peerbank offers competing buy-to-let mortgages.",
        retrieved_at="2026-11-13",
    )

    class Research:
        def __init__(self, *args):
            pass

        async def issuer_pages(self, name, number=""):
            return [subject] if number else [peer]

        async def search(self, *args, **kwargs):
            return [lead]

    async def model(schema, prompt):
        if schema.__name__ == "PeerCandidateSelection":
            return schema(candidates=[{"name": "Peerbank", "source_id": "lead"}])
        data = json.loads(prompt[prompt.index('{"company_name"') :])
        wanted = {"buy_to_let", "landlords", "uk"}
        ids = []
        for item in data["statements"]:
            if item["code"] in wanted:
                ids.append(item["candidate_id"])
                wanted.remove(item["code"])
        return ProfileSelection(selected_ids=ids)

    async def registry(name, args):
        if name == "resolve_company":
            return {
                "selected_company_number": "11111111",
                "candidates": [{"company_number": "11111111"}, {"company_number": "87654321"}],
            }
        assert args["company_number"] == "87654321"
        return {
            "company_number": "87654321",
            "company_name": "Peerbank Limited",
            "company_status": "active",
        }

    monkeypatch.setattr(discovery, "TavilyResearch", Research)
    monkeypatch.setattr(discovery, "ask_model", model)
    result = await discovery.discover_web_peers(
        {"company_number": "12345678", "profile": {"company_name": "Subject"}}, registry, ""
    )
    decision = result.candidates[0]
    if ambiguous:
        assert decision.status == "excluded" and "ambiguous" in decision.reasons[0]
    else:
        assert decision.company_number == "87654321" and decision.status == "shortlisted"
        assert decision.shared_facets == ["buy_to_let", "landlords", "uk"]
        rendered = "\n".join(discovery.render_discovery(result.model_dump(mode="json")))
        assert (
            "Legal-entity binding" in rendered and "https://examplebank.co.uk/products" in rendered
        )


@pytest.mark.asyncio
async def test_model_request_has_typed_schema_and_no_tool_execution(monkeypatch):
    from types import SimpleNamespace

    class Model:
        async def generate_content_async(self, request, stream):
            assert request.config.response_schema is ProfileSelection
            yield SimpleNamespace(
                error_code=None,
                content=SimpleNamespace(
                    parts=[SimpleNamespace(text=json.dumps({"selected_ids": []}))]
                ),
            )

    monkeypatch.setattr(discovery, "agent_model", lambda: Model())
    assert (await discovery.ask_model(ProfileSelection, "Select statements")).selected_ids == []

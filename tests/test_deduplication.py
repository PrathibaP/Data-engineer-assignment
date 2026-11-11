"""Cross-source dedup: exact dupes, title variants, same title other city, other company, false positives."""
from pipeline.resolve.dedup import AUTO_MERGE, REVIEW, Candidate, best_match, score_pair, title_similarity


def cand(key, source="greenhouse", board="stripe", jid="1", company="stripe", title="Senior Data Engineer",
         seniority="senior", city="San Francisco", remote=None, skills=("Python", "Spark"), url="", links=()):
    return Candidate(key, source, board, jid, company, title, seniority, city, "US", remote, list(skills), url, list(links))


GH = cand("greenhouse:stripe:1", title="Senior Data Engineer, Platform", url="https://boards.greenhouse.io/stripe/jobs/4401234")


def test_exact_duplicate_same_source_key():
    m = best_match(cand("greenhouse:stripe:1"), {"p1": [GH]})
    assert m.method == "source_key" and m.posting_id == "p1"


def test_exact_duplicate_across_sources_merges():
    hn = cand("hn:t:9:x", source="hackernews", board="t", jid="9", title="Senior Data Engineer, Platform")
    s, _ = score_pair(hn, GH)
    assert s >= AUTO_MERGE


def test_different_title_formats_same_job_merge():
    for t in ["Sr. Data Engineer", "Senior Data Engineer", "Senior Data Engineer – Platform", "Sr Data Eng, Platform"]:
        hn = cand("hn:t:9:x", source="hackernews", board="t", jid="9", title=t)
        s, why = score_pair(hn, GH)
        assert s >= AUTO_MERGE, (t, s, why)


def test_same_title_different_location_not_merged():
    hn = cand("hn:t:9:x", source="hackernews", board="t", jid="9", title="Senior Data Engineer", city="Seattle")
    s, why = score_pair(hn, GH)
    assert s < REVIEW and "city conflict" in why


def test_same_board_different_ids_never_merge():
    other = cand("greenhouse:stripe:2", jid="2", title="Senior Data Engineer, Platform", city="Seattle")
    assert score_pair(other, GH)[0] == 0.0
    same_city = cand("greenhouse:stripe:3", jid="3", title="Senior Data Engineer, Platform")
    assert best_match(same_city, {"p1": [GH]}).posting_id is None  # two reqs, even in the same city


def test_similar_titles_different_companies_not_merged():
    hn = cand("hn:t:9:x", source="hackernews", board="t", jid="9", company="plaid", title="Senior Data Engineer, Platform")
    assert score_pair(hn, GH)[0] == 0.0
    assert best_match(hn, {"p1": [GH]}).method == "new"


def test_false_positive_seniority_conflict():
    hn = cand("hn:t:9:x", source="hackernews", board="t", jid="9", title="Staff Data Engineer", seniority="staff_plus")
    s, why = score_pair(hn, GH)
    assert s < AUTO_MERGE and "seniority conflict" in why


def test_false_positive_different_role_family():
    hn = cand("hn:t:9:x", source="hackernews", board="t", jid="9", title="Senior Machine Learning Engineer")
    assert score_pair(hn, GH)[0] < AUTO_MERGE


def test_false_positive_different_team_qualifier():
    assert title_similarity("Data Engineer, Ads", "Data Engineer, Payments") < 0.8
    a = cand("hn:t:9:x", source="hackernews", board="t", jid="9", title="Senior Data Engineer, Ads", skills=())
    b = cand("greenhouse:stripe:7", jid="7", title="Senior Data Engineer, Payments", skills=())
    assert score_pair(a, b)[0] < AUTO_MERGE


def test_outbound_link_to_ats_is_definitive():
    hn = cand("hn:t:9:x", source="hackernews", board="t", jid="9", title="Data person (see link)", seniority=None,
              city=None, links=["https://boards.greenhouse.io/stripe/jobs/4401234?gh_src=hn"])
    m = best_match(hn, {"p1": [GH]})
    assert m.method == "url_link" and m.posting_id == "p1"


def test_uncertain_match_goes_to_review_band():
    hn = cand("hn:t:9:x", source="hackernews", board="t", jid="9", title="Senior Data Engineer", city=None,
              remote="remote", skills=("Go",))
    s, _ = score_pair(hn, GH)
    assert REVIEW <= s < AUTO_MERGE

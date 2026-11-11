"""Generate SYNTHETIC HTTP fixtures that mimic the real Greenhouse / Lever / HN response formats.

These are NOT real postings. They exist so tests and the offline demo run are reproducible without
network access, and they deliberately contain the edge cases from the assignment:
duplicates across sources, same title in two cities, multi-role HN comments, non-US roles, replies,
deleted comments, pagination, an empty board (substitution), HTML-instead-of-JSON, 503s.

live_v1 = first snapshot; live_v2 = a later snapshot with a salary change, a location change,
a closed posting, a Lever board answering HTML, and an HN page failing with 503.

    python tests/fixtures/make_fixtures.py
"""
from __future__ import annotations

import copy
import html
import json
from pathlib import Path

ROOT = Path(__file__).parent
GH = "https://boards-api.greenhouse.io/v1/boards/{}/jobs?content=true"
LV = "https://api.lever.co/v0/postings/{}?mode=json"
HN = "https://news.ycombinator.com/"


def gh_job(jid, token, title, loc, body_html, first_published="2026-10-20T12:00:00-04:00"):
    return {"id": jid, "title": title, "location": {"name": loc}, "absolute_url": f"https://boards.greenhouse.io/{token}/jobs/{jid}",
            "updated_at": "2026-11-01T09:00:00-04:00", "first_published": first_published, "requisition_id": f"R{jid}",
            "company_name": token.title(), "offices": [], "metadata": None,
            "content": html.escape(body_html)}  # Greenhouse entity-escapes the HTML


DE_BODY = ("<p>We are looking for a {title} to build batch and streaming pipelines.</p>"
           "<h3>What you'll do</h3><ul><li>Own pipelines in Spark and Airflow</li><li>Model data in dbt and Snowflake</li></ul>"
           "<h3>Requirements</h3><ul><li>5+ years of Python and SQL</li><li>Experience with Kafka</li></ul>{extra}")

greenhouse_v1 = {
    "stripe": [
        gh_job(1001, "stripe", "Senior Data Engineer, Platform", "San Francisco, CA",
               DE_BODY.format(title="Senior Data Engineer", extra="<p>The annual base salary range for this role in San Francisco is "
                              "$180,000 - $250,000 USD per year. This role is full-time.</p>")),
        gh_job(1002, "stripe", "Senior Data Engineer, Platform", "Seattle, WA",
               DE_BODY.format(title="Senior Data Engineer", extra="<p>The annual base salary range for this role in Seattle is "
                              "$170,000 - $235,000 USD per year.</p>")),
        gh_job(1003, "stripe", "Account Executive, Enterprise", "New York, NY", "<p>Sell payments.</p>"),
        gh_job(1004, "stripe", "Machine Learning Engineer, Fraud", "Remote - US",
               "<p>Build fraud models with PyTorch and Python. This role is remote within the US.</p>"),
        gh_job(1005, "stripe", "Data Engineer", "Toronto, Canada", DE_BODY.format(title="Data Engineer", extra="")),
    ],
    "airbnb": [
        gh_job(2001, "airbnb", "Staff Machine Learning Engineer - Search", "San Francisco, CA",
               "<p>Lead ranking models for search using TensorFlow, Spark and Python. This is a hybrid role, "
               "3 days in office.</p><p>Pay range: $220,000—$300,000 USD annually.</p>"),
        gh_job(2002, "airbnb", "Software Engineer, Frontend", "San Francisco, CA", "<p>React and TypeScript.</p>"),
        gh_job(2003, "airbnb", "Analytics Engineer", "Austin, TX",
               "<p>Own dbt models and SQL metrics layers in our lakehouse. Hybrid in Austin.</p>"),
    ],
    "databricks": [
        gh_job(3001, "databricks", "Sr. Platform Engineer - Data Infrastructure", "Mountain View, CA",
               "<p>Run the Spark and Kafka platform on Kubernetes and AWS. Onsite in Mountain View. "
               "Python, Scala, Terraform.</p>"),
        gh_job(3002, "databricks", "Platform Engineer", "San Francisco, CA",
               "<p>Own our web CI/CD, build tooling and developer experience. React and TypeScript a plus.</p>"),
        gh_job(3003, "databricks", "Solutions Architect", "Chicago, IL", "<p>Customer-facing.</p>"),
    ],
    "figma": [],  # empty board -> substitution
    "dropbox": [
        gh_job(4001, "dropbox", "Data Engineer II", "Remote (US)",
               "<p>Remote in the US. Build data pipelines with Python, Airflow and BigQuery. "
               "Salary: $150,000 - $185,000 per year. We are unable to sponsor visas for this role.</p>"),
    ],
}

PLAID_DE = "8f1c2a7e-1111-4c3b-9a55-0e5c0b5d2222"
PALANTIR_DE = "3b9d0c21-aaaa-4f2e-8a77-1c2d3e4f5a6b"
lever_v1 = {
    "palantir": [
        {"id": PALANTIR_DE, "text": "Data Engineer", "hostedUrl": f"https://jobs.lever.co/palantir/{PALANTIR_DE}",
         "categories": {"location": "New York, NY", "commitment": "Full-time", "team": "Engineering",
                        "allLocations": ["New York, NY"]}, "workplaceType": "onsite", "createdAt": 1761000000000,
         "descriptionPlain": "Build data integrations in Python, Spark and SQL for government and commercial customers.",
         "lists": [{"text": "Requirements", "content": "<li>Python</li><li>Spark</li><li>Java</li>"}],
         "additionalPlain": "", "country": "US",
         "salaryRange": {"min": 130000, "max": 200000, "currency": "USD", "interval": "per-year-salary"}},
        {"id": "5e6f7a8b-bbbb-4c1d-9e2f-334455667788", "text": "Machine Learning Engineer",
         "hostedUrl": "https://jobs.lever.co/palantir/5e6f7a8b-bbbb-4c1d-9e2f-334455667788",
         "categories": {"location": "Palo Alto, CA", "commitment": "Full-time", "allLocations": ["Palo Alto, CA"]},
         "workplaceType": "hybrid", "createdAt": 1761500000000,
         "descriptionPlain": "Train and deploy models with PyTorch on Kubernetes.", "lists": [], "additionalPlain": "",
         "country": "US"},
        {"id": "9a9a9a9a-cccc-4000-8000-000000000001", "text": "Deployment Strategist",
         "hostedUrl": "https://jobs.lever.co/palantir/9a9a9a9a-cccc-4000-8000-000000000001",
         "categories": {"location": "Washington, DC"}, "workplaceType": "onsite", "createdAt": 1761500000000,
         "descriptionPlain": "Customer-facing.", "lists": []},
        {"id": "9a9a9a9a-dddd-4000-8000-000000000002", "text": "Data Engineer",
         "hostedUrl": "https://jobs.lever.co/palantir/9a9a9a9a-dddd-4000-8000-000000000002",
         "categories": {"location": "London, United Kingdom", "allLocations": ["London, United Kingdom"]},
         "workplaceType": "onsite", "createdAt": 1761500000000, "descriptionPlain": "Python and Spark.", "lists": [],
         "country": "GB"},
    ],
    "plaid": [
        {"id": "1c1c1c1c-eeee-4000-8000-000000000003", "text": "Senior Analytics Engineer",
         "hostedUrl": "https://jobs.lever.co/plaid/1c1c1c1c-eeee-4000-8000-000000000003",
         "categories": {"location": "San Francisco", "commitment": "Full-Time", "allLocations": ["San Francisco"]},
         "workplaceType": "hybrid", "createdAt": 1761800000000,
         "descriptionPlain": "Own dbt and SQL models on Snowflake.", "lists": [], "country": "US",
         "salaryRange": {"min": 170000, "max": 210000, "currency": "USD", "interval": "per-year-salary"}},
        {"id": PLAID_DE, "text": "Senior Data Engineer", "hostedUrl": f"https://jobs.lever.co/plaid/{PLAID_DE}",
         "categories": {"location": "San Francisco", "commitment": "Full-Time", "allLocations": ["San Francisco"]},
         "workplaceType": "hybrid", "createdAt": 1761800000000,
         "descriptionPlain": "Build streaming pipelines with Kafka, Flink and Python.", "lists": [], "country": "US"},
    ],
}

SUBMITTED = """<html><body><table id="hnmain"><tr><td><table>
<tr class="athing submission" id="46000000"><td class="title"><span class="titleline"><a href="item?id=46000000">Ask HN: Who is hiring? (November 2026)</a></span></td></tr>
<tr class="athing submission" id="46000001"><td class="title"><span class="titleline"><a href="item?id=46000001">Ask HN: Who wants to be hired? (November 2026)</a></span></td></tr>
<tr class="athing submission" id="46000002"><td class="title"><span class="titleline"><a href="item?id=46000002">Ask HN: Freelancer? Seeking freelancer? (November 2026)</a></span></td></tr>
<tr class="athing submission" id="45700000"><td class="title"><span class="titleline"><a href="item?id=45700000">Ask HN: Who is hiring? (October 2026)</a></span></td></tr>
</table><a href="submitted?id=whoishiring&amp;next=45700000" class="morelink" rel="next">More</a></td></tr></table></body></html>"""


def comment(cid, indent, user, body_html, ts="2026-11-02T16:01:23 1793635283"):
    text = f'<div class="commtext c00">{body_html}</div>' if body_html is not None else '<div class="comment"><span class="commtext">[deleted]</span></div>'
    if body_html is None:
        text = '<div class="comment">[deleted]</div>'
    return f"""<tr class="athing comtr" id="{cid}"><td><table border="0"><tr>
<td class="ind" indent="{indent}"><img src="s.gif" height="1" width="{indent * 40}"></td>
<td valign="top" class="votelinks"></td><td class="default"><div style="margin-top:2px"><span class="comhead">
<a href="user?id={user}" class="hnuser">{user}</a> <span class="age" title="{ts}"><a href="item?id={cid}">9 days ago</a></span>
</span></div><br><div class="comment">{text}<div class="reply"><p><font size="1"><u><a href="reply?id={cid}">reply</a></u></font></div></div></td>
</tr></table></td></tr>"""


def item_page(rows, more):
    more_html = f'<a href="item?id=46000000&amp;p={more}" class="morelink" rel="next">More</a>' if more else ""
    return f"""<html><body><table id="hnmain"><tr><td><table class="fatitem"><tr class="athing" id="46000000">
<td class="title"><span class="titleline"><a href="item?id=46000000">Ask HN: Who is hiring? (November 2026)</a></span></td></tr></table>
<table class="comment-tree">{''.join(rows)}</table>{more_html}</td></tr></table></body></html>"""


HN_P1 = [
    comment(46000101, 0, "stripe_hiring", "Stripe | Sr. Data Engineer | San Francisco, CA | ONSITE | Full-time | $180k-$250k"
            "<p>Our data platform team builds pipelines on Spark, Airflow and Kafka in Python.</p>"
            "<p>Apply: https://stripe.com/jobs</p>"),
    comment(46000102, 0, "plaidjobs", f"Plaid | Senior Data Engineer | San Francisco | Hybrid"
            f"<p>Streaming with Kafka and Flink. Apply at <a href=\"https://jobs.lever.co/plaid/{PLAID_DE}\" rel=\"nofollow\">"
            f"https://jobs.lever.co/plaid/{PLAID_DE}</a></p>"),
    comment(46000103, 0, "acme_cto", "Acme Analytics | Multiple roles | REMOTE (US) | Full-time"
            "<p>We're hiring:</p><p>- Senior Data Engineer ($170k-$200k): Python, dbt, Snowflake</p>"
            "<p>- Machine Learning Engineer ($160k-$190k): PyTorch, Ray</p><p>- Frontend Engineer: React</p>"
            "<p>We sponsor visas.</p>"),
    comment(46000104, 0, "berlin_co", "Berliner Daten GmbH | Data Engineer | Berlin, Germany | Onsite<p>Spark and Python.</p>"),
    comment(46000105, 0, "foocorp", "FooCorp | Backend Engineer | NYC | Onsite<p>Go and Postgres.</p>"),
    comment(46000106, 1, "replier", "Is the data engineer role open to juniors?"),
    comment(46000107, 0, "ghost", None),
    comment(46000108, 0, "dbx_recruit", "Databricks | Senior Platform Engineer, Data Infrastructure | Mountain View, CA | Onsite"
            "<p>Spark, Kafka, Kubernetes, AWS and Terraform.</p>"),
    comment(46000109, 0, "northwind", "Northwind Robotics | ML Engineer | Boston, MA | ONSITE | Full-time"
            "<p>Perception models in PyTorch. No visa sponsorship available.</p>"),
]
HN_P2 = [
    comment(46000110, 0, "airbnb_eng", "Airbnb | Staff ML Engineer, Search | San Francisco | Hybrid<p>Ranking with TensorFlow and Spark.</p>"),
    comment(46000111, 0, "stripe_hiring2", "Stripe | Staff Data Engineer | Seattle, WA | Onsite<p>Spark and Python.</p>"),
    comment(46000112, 0, "globex", "Globex | Data Engineer | Austin, TX | Onsite | $150,000 - $180,000 per year"
            "<p>Visa sponsorship available. Python, Airflow, Redshift.</p>"),
    comment(46000113, 0, "initech", "Initech | Analytics Engineer | Remote (US only) | Contract | $90/hr<p>dbt and SQL.</p>"),
    comment(46000114, 0, "hooli", "Hooli | Data Platform Engineer | Toronto or Remote (Canada)<p>Kafka.</p>"),
]
ROBOTS_HN = "User-agent: *\nDisallow: /x?\nDisallow: /vote?\nDisallow: /reply?\nCrawl-delay: 1\n"


def write(dirname, greenhouse, lever, hn_pages, overrides=None):
    d = ROOT / dirname
    d.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for tok, jobs in greenhouse.items():
        (d / f"gh_{tok}.json").write_text(json.dumps({"jobs": jobs, "meta": {"total": len(jobs)}}, indent=1))
        manifest[GH.format(tok)] = {"file": f"gh_{tok}.json", "content_type": "application/json; charset=utf-8"}
    for site, posts in lever.items():
        (d / f"lever_{site}.json").write_text(json.dumps(posts, indent=1))
        manifest[LV.format(site)] = {"file": f"lever_{site}.json", "content_type": "application/json; charset=utf-8"}
    (d / "hn_submitted.html").write_text(SUBMITTED)
    manifest[HN + "submitted?id=whoishiring"] = {"file": "hn_submitted.html", "content_type": "text/html; charset=utf-8"}
    (d / "hn_robots.txt").write_text(ROBOTS_HN)
    manifest[HN + "robots.txt"] = {"file": "hn_robots.txt", "content_type": "text/plain"}
    for i, (rows, more) in enumerate(hn_pages, 1):
        (d / f"hn_item_p{i}.html").write_text(item_page(rows, more))
        url = HN + "item?id=46000000" + (f"&p={i}" if i > 1 else "")
        manifest[url] = {"file": f"hn_item_p{i}.html", "content_type": "text/html; charset=utf-8"}
    manifest.update(overrides or {})
    (d / "manifest.json").write_text(json.dumps(manifest, indent=1))
    (d / "README.md").write_text("SYNTHETIC fixtures generated by make_fixtures.py. Not real job postings.\n")


if __name__ == "__main__":
    write("live_v1", greenhouse_v1, lever_v1, [(HN_P1, 2), (HN_P2, None)])

    gh2 = copy.deepcopy(greenhouse_v1)
    s = gh2["stripe"]
    s[0]["content"] = s[0]["content"].replace("$180,000 - $250,000", "$190,000 - $260,000")  # salary change
    gh2["stripe"] = [j for j in s if j["id"] != 1004]  # 1004 closed
    gh2["airbnb"][2]["location"]["name"] = "New York, NY"  # location change
    gh2["airbnb"][2]["content"] = html.escape("<p>Own dbt models and SQL metrics layers in our lakehouse. Hybrid in New York.</p>")
    (ROOT / "live_v2").mkdir(exist_ok=True)
    (ROOT / "live_v2" / "plaid_maintenance.html").write_text("<html><body>Down for maintenance</body></html>")
    write("live_v2", gh2, lever_v1, [(HN_P1, 2), (HN_P2, None)], overrides={
        LV.format("plaid"): {"file": "plaid_maintenance.html", "content_type": "text/html"},  # HTML instead of JSON
        HN + "item?id=46000000&p=2": {"status": 503, "content_type": "text/html"},  # partial HN thread
    })
    print("fixtures written")

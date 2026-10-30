# mgnrega

Scrapers for the MGNREGA MIS (NREGASoft, Ministry of Rural Development) in the s1d-labs scrape pool. The sites answer only from
Indian IPs, so both projects are `geo="IN"`: the hub's India pool servers run them, GitHub runners never do.

| Project | Task | What | One queue item | Records |
|---|---|---|---|---|
| `mgnrega-vendors` (G06) | `vendors.py` | MIS report "Vendor Wise Expenditure" (mnregaweb2.dord.gov.in) | district x financial year | one per material line of a bill: vendor (name, registration/TIN), work (code, name, category), voucher, bill no/date, material, amount, payment date, amount paid |
| `mgnrega-socialaudit` (A07) | `socialaudit.py` | "Social Audit Report" formats 1, 2A, 2B (mnregaweb4.nic.in/NregaArch/SocialAudit) | block x financial year | `type=panchayat` (registration, job cards, works, wage/material totals per audit period), `type=work` (sanctioned, spent, spent in the audit period), `type=material` (vendor, bill, material, price, quantity) |

Raw pages go to the bucket through the hub (`vendors/<year>/<state>/<district>/<block>/<panchayat>.html.gz`,
`socialaudit/<year>/<state>/<district>/<block>/<panchayat>/<period>/format*.html.gz`). Muster rolls, payments to named workers
(format 3) and SC/ST household lists (format 4) are never fetched.

How the site works: report links carry an encrypted `payload=` bound to the session that made it, so each item walks from a public
start page in one session; years and dropdowns are ASP.NET postbacks. The national "MIS Report" index has an arithmetic captcha;
these tasks don't use it (the state pages link the same reports without one).

- `geo.json`: states, districts and blocks (with codes) the queues are built from; `python make_geo.py > geo.json` from an Indian IP.
- `python -m pytest tests`: parsing tests on saved pages.
- One item without the hub (Indian IP): see the `--local` examples at the top of `vendors.py` and `socialaudit.py`.
- `scrape-check --items 2 --env MAX_BLOCKS=1 --env MAX_PANCHAYATS=1` (and `--project mgnrega-socialaudit`) from an Indian IP.
- The **setup** workflow registers the projects, pool entries (`geo="IN"`, 4 workers each) and queues (5 financial years).

The full contract with scrape-hub is in [CLAUDE.md](CLAUDE.md).

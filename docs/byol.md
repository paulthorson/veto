# Bring your own listing (BYOL)

The primary way to get a job posting into Veto (legal-hardening commit 7,
spec §5.2). Replaces the deleted board scrapers: instead of Veto fetching
job boards, **you** paste a job description, paste a posting URL, or drop a
saved HTML file, and Veto parses it locally. Your browser did the
fetching; Veto does the thinking.

## The three inputs

| Input | Command | What happens |
|---|---|---|
| Pasted text | `cli.py add-listing "Senior SWE @ Acme …"` | Parsed locally, zero network |
| Posting URL | `cli.py add-listing --url https://…` | Fetched **once** with the honest Veto User-Agent, robots.txt honored, polite delay; then parsed locally |
| Saved HTML file | `cli.py add-listing --html-file posting.html` | Parsed locally, zero network |

Optional `--title`, `--company`, `--location` override the auto-detected
values (the first line of pasted text is tried as `"Title @ Company"`).

## Where it lives

- **CLI**: `add-listing` (FIND group). Prints the job id and next steps.
- **Dashboard**: menu `0` — "Add a job listing", first item under FIND,
  with an immediate offer to score or decode the new listing.
- **MCP tools**: `add_listing(text=… / url=… / html_file=…)` and
  `list_listings()`.
- **Web UI**: FIND → "Bring your own listing" → `add` / `list`.

## Downstream: one code path

The parsed listing is a standard job dict (same keys the providers
produce) saved to `listings/<key>.json`, with id `user:<base64url-key>`.
The `UserListingsBoard` pseudo-provider (board name `"user"`) is
registered in `server.PROVIDERS`, so `show`, `briefs.prep_interview`,
`tailor`, the apply queue, and the fill-only apply flow consume it
exactly like a provider-sourced listing — no duplicate path.

`cli.py search --board user` (or the dashboard board choice `user`)
searches your saved listings locally; `"all"` never queries them.

Compliance tier `"user"` means *user-supplied*: neither official-API nor
scraping. Its notices say so plainly.

Listings are user data: `listings/` belongs in `.gitignore` next to
`applications.json` and is never committed.

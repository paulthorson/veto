# Where to list an MCP server

Goal: get the server in front of people who run MCP-capable agents
(Claude, Cursor, etc.). List in this order — highest leverage first.

## Confident — list here

- **Official MCP servers repo** (`modelcontextprotocol/servers`) — the
  canonical community registry. Needs: repo URL, server description,
  install instructions, confirmation it follows the MCP spec. This is the
  listing other directories scrape from; do it first.
- **Smithery** (`smithery.ai`) — the largest MCP server marketplace with
  one-click installs. Needs: GitHub repo connection, a `smithery.yaml`
  config, verified build. This is where non-technical users will find it.
- **Awesome MCP Servers** (`punkpeye/awesome-mcp-servers`) — the most-starred
  community awesome list. Needs: a PR adding one line under the right
  category, with repo link + one-line description. Low effort, high
  discovery via GitHub search.
- **Glama** (`glama.ai/mcp`) — MCP directory with quality scoring. Needs:
  repo URL; it inspects the repo automatically. Good for credibility — a
  strong code-quality score reinforces the "governance" story.

## Likely — verify before submitting

- **Official MCP Registry** (`registry.modelcontextprotocol.io`) — the
  newer official package registry. Verify: current submission process and
  whether community servers are being accepted yet.
- **PulseMCP** (`pulsemcp.com`) — curated MCP directory. Verify: whether
  they accept direct submissions or curate editorially.
- **mcp.so** — community directory. Verify: submission form still active.

## Also worth doing (not directories)

- **Show HN** — draft in `show-hn-post.md`. The single highest-leverage
  distribution channel for this audience.
- **r/mcp and r/ClaudeAI** — short "I built this" posts once the repo is
  public. Read each sub's self-promo rules first.
- **The demo video** (script in `demo-script.md`) — directories get you
  listed; the video gets you remembered. Post natively to X and LinkedIn,
  not as a link.

## Prerequisites before any listing

1. Repo is public.
2. README opens with the hook (demo GIF or the refusal screenshot),
   one-command install, and the fill-only / governance policy up top —
   not buried.
3. Pick the product name (`name.md`) — listings are much harder to rename
   than a repo.

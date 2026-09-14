# MCP snippet for Role Radar Digest — see integration_notes.md.
# (server.py owns tool registration; this file documents the surface.)

# @mcp.tool()
# def role_radar_digest_run(action: str, params: dict | None = None) -> dict:
#     """Run a Role Radar Digest action through the extension host broker."""
#     host = get_extension_host()  # integration_notes.md
#     return host.run_extension_action("role-radar-digest", action, **(params or {}))

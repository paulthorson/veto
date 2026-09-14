# CLI snippet for Role Radar Digest — see integration_notes.md.
# (cli.py owns the parser; this file documents the intended surface.)

def cmd_role_radar_digest(args):
    """`veto extension run role-radar-digest <action>` — brokered, audited."""
    from initiatives.i11.sandbox.host import Host
    host = Host()
    host.load_extension("extensions/role-radar-digest")
    print(host.run_extension_action("role-radar-digest", args.action))

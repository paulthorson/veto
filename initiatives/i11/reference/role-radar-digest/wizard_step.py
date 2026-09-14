"""Wizard step snippet for Role Radar Digest (onboarding).

Append to the setup wizard per initiatives/i11/integration_notes.md.
"""


def wizard_step_role_radar_digest(wizard):
    """One guided step: show declared capabilities, ask to enable."""
    capabilities = ['watches:read', 'applications:read', 'jobs:read']
    wizard.show_markdown(f"""### Enable Role Radar Digest?

This extension declares access to: {", ".join(capabilities) or "nothing"}.
It cannot access anything else, and confirm-required actions always
ask you first.
""")
    return wizard.confirm("Enable this extension?", default=False)

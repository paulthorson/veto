# Apply Pilot (policy-kit fixture)

Non-vacuous fixture for `initiatives/i11/policy_kit` and
`tests/test_ext_adversarial.py`. Declares:

- a **confirm-required** action (`send-application`),
- a **network destination** (`api.example.com`),
- **profile:read** with `pii: "redact"`,
- a non-default `calls_per_minute: 25` so the rate-limit check is
  sensitive to the manifest value (a hardcoded default would fail it).

Test fixture only. Not for distribution.

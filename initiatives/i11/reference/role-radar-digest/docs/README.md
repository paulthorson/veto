# Role Radar Digest

A read-only daily digest of watched roles and application stages. Drafts a local summary and queues a notification proposal — it never sends, submits, or messages anyone.

## What this extension can access

- data scope `watches:read`
- data scope `applications:read`
- data scope `jobs:read`

### Network destinations

- none (no network access)

### Actions

- `role-radar-digest-read` (read)
- `role-radar-digest-draft` (draft)
- `role-radar-digest-notify` (notify)

> A new extension can prove what it accesses: everything above comes
> straight from its signed manifest, and the host refuses anything else.
> `confirm-required` actions always re-prompt you at execution time —
> the extension can never confirm on your behalf.

# Phone access: open the Veto dashboard on your iPhone

The web dashboard (`python3 cli.py serve`) can be opened on your phone
so you can review applications, grills, and matches away from your
desk. There are two ways to connect; pick whichever fits your network.

The quick path: `python3 wizard.py --phone` walks you through both and
prints the exact command for your machine.

## Prerequisites

- The project checked out on a computer that's on, with the server
  running (`python3 cli.py serve --host <ip>` — see below).
- The **auth token** printed in the server terminal at startup. The
  phone's login screen asks for it; tokens are case-sensitive.
- The dashboard default port is **8765**; the phone opens
  `http://<ip>:8765`.

## Mode 1: Home Wi-Fi (same network)

Fast, no extra software. Your phone must be on the **same Wi-Fi
network** as the computer.

1. On the computer, in the project directory, run:
   ```
   python3 cli.py serve --host lan
   ```
   (`lan` auto-resolves your computer's LAN IP, e.g. `192.168.1.42` —
   the terminal shows it.)
2. Note the **auth token** printed in the server terminal.
3. On your iPhone (same Wi-Fi), open Safari and go to
   `http://<lan-ip>:8765`, e.g. `http://192.168.1.42:8765`.
4. Type the token into the sign-in screen.

### Security notes (Home Wi-Fi mode)

- There is **no HTTPS** in Home Wi-Fi mode: the token travels in
  **cleartext on your LAN**. Only use this mode on a network you trust
  (your home Wi-Fi) — never on cafe, hotel, or other public Wi-Fi.
- Every `/api/*` route requires the token. Losing the token to someone
  on your LAN means they can drive the dashboard (same as losing the
  token anywhere else — see revocation below).
- The token is stored on the computer at `~/.veto_webui_token` with
  permissions `0600` (readable only by your user). Don't copy it to
  shared machines, screenshots, or chat history.

## Mode 2: Tailscale (encrypted, works off-network)

[Tailscale](https://tailscale.com/download) builds a private mesh
network between your devices (WireGuard encryption end-to-end). It
works from a coffee shop, on cellular, anywhere — and the token is
never exposed to a LAN you don't control. Tailscale is strictly
**optional**; nothing else in Veto needs it.

1. Install Tailscale on the computer from
   https://tailscale.com/download, then bring it onto your tailnet:
   ```
   tailscale up
   ```
2. Install the **Tailscale app** on your iPhone (App Store) and sign
   in with the **same account** as the computer.
3. On the computer, get the tailnet IPv4 address:
   ```
   tailscale ip -4
   ```
   (something like `100.64.0.7`).
4. Start the dashboard bound to that IP:
   ```
   python3 cli.py serve --host <tailnet-ip>
   ```
   e.g. `python3 cli.py serve --host 100.64.0.7`
5. Note the auth token printed in the server terminal.
6. On your iPhone (Tailscale app connected, same account), open Safari
   to `http://<tailnet-ip>:8765` and enter the token on the sign-in
   screen.

A visual step-by-step is also available in the dashboard itself —
open it and visit the Tailscale guide page.

## Token management

- The token is printed in the server terminal every time the server
  starts.
- **Regenerate** (invalidates the old token immediately):
  ```
  python3 cli.py serve --regenerate-token
  ```
- Alternatively, delete the token file and restart the server; a new
  token is issued:
  ```
  rm ~/.veto_webui_token
  ```
  (After deleting, restart the server and copy the new token to your
  phone — the old one stops working.)

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| Phone browser can't reach the page | Phone and computer must be on the same Wi-Fi (Mode 1), or the iPhone's Tailscale app must be connected and signed in with the same account (Mode 2). Also check the computer's firewall allows incoming connections on port 8765. |
| `tailscale ip -4` prints nothing | `tailscale up` isn't connected. Run `tailscale up` and sign in, then retry. |
| Token rejected on the phone | Tokens are case-sensitive — copy it exactly from the server terminal. If it still fails, regenerate with `--regenerate-token` and try the fresh one. |
| Server works on the computer but not the phone | The server binds to `127.0.0.1` by default, which is reachable only from the computer itself. Restart with `--host lan` (Home Wi-Fi) or `--host <tailnet-ip>` (Tailscale). |
| Old token still works after `--regenerate-token` | Only the newest token is valid; restart the server after regenerating and use the newly printed token. |

# jiji client

A vanilla-JS single-page app for using a jiji node: create/import a keypair,
read the feed, post, reply, endorse, and send transfers — all without the CLI.

No build step. Just serve the directory.

## Prerequisites

- A reachable jiji node. Start one with LAN mode so the browser can reach it:

  ```
  jiji node --lan --mine --keyfile ~/.jiji/key
  ```

  Note the bearer token printed on startup (also saved to
  `<data-dir>/rpc_token`). The RPC is at `http://<lan-ip>:9332`.

- A modern browser: Chrome 113+, Firefox 130+, or Safari 17+ (WebCrypto Ed25519
  is required).

## Run

**Same machine only** (simplest):

```
python3 -m http.server 8080
```

Open http://127.0.0.1:8080.

**Multiple devices on the LAN**: you need HTTPS. The web client signs
transactions with WebCrypto, which browsers only expose in secure contexts
(HTTPS, or `http://localhost`). From a second device over plain HTTP on a LAN
IP, `crypto.subtle` is `undefined` and signing fails.

Use the bundled HTTPS server:

```
python3 serve.py                    # https on 0.0.0.0:8443
python3 serve.py --port 8443 \
    --host my-laptop.local \
    --host 192.168.1.42             # add extra SANs if needed
```

On first run it generates a cert in `.certs/` (gitignore'd). If `mkcert` is
installed (`brew install mkcert && mkcert -install` once), the cert is
locally-trusted and browsers won't warn. Otherwise it falls back to a
self-signed cert via `openssl` — every browser shows a warning on first
visit; click **Advanced → Proceed** once per device and you're done.

The server prints something like:

```
  jiji client served at:
    https://localhost:8443
    https://10.30.11.161:8443   (other devices on your LAN)
```

## First-time setup

1. **Create a wallet.** The app generates an Ed25519 keypair in-browser and
   encrypts the private key with your passphrase (PBKDF2 → AES-GCM). The
   encrypted blob lives in `localStorage`. The private key is never sent to
   the node.
2. **Connect to a node.** Enter the RPC URL and bearer token from the
   `jiji node --lan` startup output.
3. **Fund the wallet.** A brand-new key has zero balance. Either mine to it
   (`--keyfile` on the node points at this key) or receive a transfer from
   another account before posting.

## Features

- **Feed**: scans the last 50 blocks on first load, then incremental. Posts
  and endorsements are cached in IndexedDB. Reorgs trigger a rescan.
- **Compose**: post (≤ 300 chars) or reply. Signs locally.
- **Wallet**: balance, nonce, and transfers.
- **Settings**: copy public key, switch nodes, export private key (passphrase
  required), lock, or wipe the wallet from this device.

## Safety bounds

The feed scanner caps itself so a large chain or a misbehaving node can't
freeze the browser:

- first-run / post-reorg scan: last 50 blocks
- per-refresh walk: at most 100 blocks
- cached posts: at most 500 (LRU by block height)

## What's stored where

| Thing                | Where                      |
| -------------------- | -------------------------- |
| Encrypted key blob   | `localStorage["jiji.wallet"]` |
| Node URL + token     | `localStorage["jiji.node"]`   |
| Posts / endorsements | IndexedDB `jiji-cache`        |
| Private key in-mem   | cleared on lock / tab close   |

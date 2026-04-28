# Slide 8 — Web Frontend (Speaker Script)

**Slide title on screen:** *Client Application — Web Frontend*
**Estimated runtime:** ~2 min 30 sec
**Bracketed lines** are stage directions, not spoken.

---

## Opening — frame the slide (≈ 15s)

> "So far we've talked about the protocol — blocks, consensus, the P2P
> mesh. But protocols don't post tweets, people do. This slide is about the
> reference web client: a pure-browser app that lets a user generate a
> wallet, sign transactions, and read the feed without ever installing the
> CLI."

[Gesture to the screen, then click into the first quadrant.]

---

## 1. In-browser key management (≈ 35s)

> "Top-left: key management lives entirely in the browser. We use the
> WebCrypto API's native Ed25519 support — that's Chrome 113, Firefox 130,
> Safari 17 and up. No JavaScript crypto library, no hand-rolled scalar
> math; the browser does it.
>
> The private key never leaves the device. To survive a page reload we
> encrypt it with the user's passphrase: PBKDF2 with 200,000 iterations of
> SHA-256 to derive a key, then AES-GCM-256 to seal the seed. The encrypted
> blob sits in localStorage; the plaintext key only exists in memory between
> 'unlock' and 'lock'. The node literally cannot exfiltrate it because it
> never gets it in the first place."

[If asked: 200k iterations is OWASP's current PBKDF2-SHA256 floor.]

---

## 2. Reorg-safe feed (≈ 40s)

> "Top-right: the feed cache. Posts and endorsements are indexed into
> IndexedDB the first time you see them, so reopening the tab is instant
> instead of re-walking the chain.
>
> The interesting problem is *reorgs* — proof-of-work means the tip can
> change retroactively. So on every refresh we walk backwards from the
> current tip, block by block, until we hit a hash we already have in the
> cache. Same hash = our cache is consistent, stop. Different hash at the
> same height = the chain reorged under us, drop the suspect range and do a
> bounded rescan.
>
> Three safety caps make sure a misbehaving node or a giant chain can't
> freeze your tab: 50 blocks on first load, 100 blocks per refresh, and 500
> cached posts with LRU eviction by height. So worst case you do a fixed
> amount of work, never an unbounded scan."

---

## 3. Client-side signing (≈ 25s)

> "Bottom-left: client-side signing. The interesting bit here is the
> canonical-JSON encoder — it produces the exact same bytes as the Python
> node's serializer. Sorted keys, no whitespace, integers not floats.
>
> Why does that matter? Because the transaction hash is `SHA-256` over
> those bytes, and the signature is over that hash. If the browser
> serializes a tx differently from the node by even one byte, the signature
> verifies in the browser and fails on every full node in the network. So
> we kept both encoders byte-for-byte identical and have tests that pin
> them together."

---

## 4. User-facing views (≈ 30s)

> "Bottom-right: the actual UI surface. Four tabs.
>
> *Feed* renders posts threaded by `reply_to`. *Compose* opens with a reply
> banner if you tapped 'reply' from the feed, otherwise it's a top-level
> post. *Wallet* shows your balance and nonce and lets you transfer. And
> *Settings* exposes the things people occasionally need: copy your
> pubkey, switch nodes, export the encrypted key, lock the wallet, or wipe
> it from this device.
>
> Endorsements are a one-click action on every post in the feed — there's
> an optional tip amount and a 200-character message that travels with the
> endorsement on chain."

---

## Footer — how it's served (≈ 20s)

[Point to the footer line of the slide.]

> "And to make this work over a LAN — which we need because WebCrypto only
> exposes itself in HTTPS contexts — the project ships a tiny dev server,
> `frontend/serve.py`. On first run it tries `mkcert` for a locally-trusted
> cert, falls back to a self-signed `openssl` cert. So you can pull up the
> wallet from your phone on the same Wi-Fi without webpack, npm, or a
> build step at all."

---

## Closing transition (≈ 10s)

> "That's the client. The whole 'separate protocol from presentation'
> story we set up at the start of the talk — this is what it looks like in
> practice. Anyone can build a different one with different moderation,
> different ranking, different UI; the chain doesn't care."

[Click to next slide.]

---

## Likely Q&A — keep these answers ready

**"Why not a JS Ed25519 library like tweetnacl?"**
> Because native WebCrypto runs in optimized C, gets the browser's RNG,
> and doesn't ship 80KB of audited-but-still-JS crypto over the wire. The
> browsers we target all have it.

**"What if someone steals my localStorage?"**
> They get the encrypted blob, not the key. Brute-forcing PBKDF2 at 200k
> iters takes ~150ms per guess on modern hardware — fine for a passphrase
> with real entropy, hopeless against a 4-digit PIN. We surface a strength
> hint in the UI but ultimately that's the user's problem.

**"How do you know the canonical-JSON encoders match?"**
> Round-trip tests. Generate a tx in Python, serialize and hash; do the
> same in JS; assert the hashes match. Run on every CI build. If anyone
> ever changes one encoder, the other test breaks loudly.

**"What happens during a deep reorg?"**
> The walk-back hits the 100-block budget, gives up, and falls back to a
> 50-block fresh rescan from the new tip. We lose visibility into anything
> older than that until the user scrolls back, at which point we paginate.

**"Can I use it without the bundled dev server?"**
> Yes — any HTTPS host works, or `http://localhost` (browsers grant
> secure-context to localhost). The dev server just removes the cert
> fiddling for the LAN-demo case.

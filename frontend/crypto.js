// Ed25519 keypair, signing, passphrase-encrypted storage, and the
// canonical-JSON encoder that matches the Python node byte-for-byte.
//
// WebCrypto exposes Ed25519 natively in Chrome 113+, Firefox 130+, Safari 17+.
// Older browsers will throw on generateKey/importKey — caller should surface
// a clear "update your browser" message.

// -- hex + base64url helpers --------------------------------------------------

export function bytesToHex(bytes) {
    return Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
}

export function hexToBytes(hex) {
    hex = hex.trim();
    if (hex.length % 2 !== 0) throw new Error("hex length must be even");
    const out = new Uint8Array(hex.length / 2);
    for (let i = 0; i < out.length; i++) {
        const byte = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
        if (Number.isNaN(byte)) throw new Error("invalid hex");
        out[i] = byte;
    }
    return out;
}

function bytesToBase64url(bytes) {
    let s = "";
    for (const b of bytes) s += String.fromCharCode(b);
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function base64urlToBytes(s) {
    s = s.replace(/-/g, "+").replace(/_/g, "/");
    while (s.length % 4) s += "=";
    const bin = atob(s);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
}

// -- canonical JSON (matches Python: sort_keys=True, separators=(',',':')) ---
//
// Integers, strings, nulls, nested objects and arrays. Matches JSON.stringify
// on scalars, but enforces sorted object keys and drops all whitespace.

export function canonicalize(value) {
    if (value === null || value === undefined) return "null";
    if (typeof value === "number") {
        if (!Number.isFinite(value)) throw new Error("non-finite number");
        if (!Number.isInteger(value)) return JSON.stringify(value);
        return value.toString();
    }
    if (typeof value === "boolean") return value ? "true" : "false";
    if (typeof value === "string") return JSON.stringify(value);
    if (Array.isArray(value)) {
        return "[" + value.map(canonicalize).join(",") + "]";
    }
    if (typeof value === "object") {
        const keys = Object.keys(value).sort();
        const parts = keys.map(k => JSON.stringify(k) + ":" + canonicalize(value[k]));
        return "{" + parts.join(",") + "}";
    }
    throw new Error("unsupported JSON value: " + typeof value);
}

export function canonicalBytes(value) {
    return new TextEncoder().encode(canonicalize(value));
}

// -- Ed25519 PKCS8 wrapping --------------------------------------------------
//
// WebCrypto won't take a bare 32-byte Ed25519 seed. It accepts PKCS8, which
// for Ed25519 has a fixed 16-byte prefix followed by the seed.
// See RFC 8410 §7.

const PKCS8_PREFIX = new Uint8Array([
    0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06,
    0x03, 0x2b, 0x65, 0x70, 0x04, 0x22, 0x04, 0x20,
]);

function seedToPkcs8(seed) {
    if (seed.length !== 32) throw new Error("Ed25519 seed must be 32 bytes");
    const out = new Uint8Array(PKCS8_PREFIX.length + 32);
    out.set(PKCS8_PREFIX, 0);
    out.set(seed, PKCS8_PREFIX.length);
    return out;
}

// -- Keypair operations ------------------------------------------------------

export async function generateKeypair() {
    const kp = await crypto.subtle.generateKey(
        { name: "Ed25519" }, true, ["sign", "verify"],
    );
    return await exportKeypair(kp.privateKey);
}

export async function importFromPrivateHex(hex) {
    const seed = hexToBytes(hex);
    if (seed.length !== 32) throw new Error("private key must be 32 bytes (64 hex chars)");
    const pkcs8 = seedToPkcs8(seed);
    const privateKey = await crypto.subtle.importKey(
        "pkcs8", pkcs8, { name: "Ed25519" }, true, ["sign"],
    );
    return await exportKeypair(privateKey);
}

// Derive the public key by exporting the private CryptoKey as JWK —
// WebCrypto fills in `x` (the derived public key) for us.
async function exportKeypair(privateKey) {
    const jwk = await crypto.subtle.exportKey("jwk", privateKey);
    const privBytes = base64urlToBytes(jwk.d);
    const pubBytes = base64urlToBytes(jwk.x);
    // Also import the public side so we can call verify() if we want.
    const publicKey = await crypto.subtle.importKey(
        "jwk", { kty: "OKP", crv: "Ed25519", x: jwk.x },
        { name: "Ed25519" }, true, ["verify"],
    );
    return {
        privateKey,           // CryptoKey (for sign)
        publicKey,            // CryptoKey (for verify)
        privateHex: bytesToHex(privBytes),
        publicHex: bytesToHex(pubBytes),
    };
}

export async function signCanonical(privateKey, value) {
    const msg = canonicalBytes(value);
    const sig = await crypto.subtle.sign({ name: "Ed25519" }, privateKey, msg);
    return bytesToHex(new Uint8Array(sig));
}

// SHA-256 of canonical JSON (matches node's `tx_hash`).
export async function sha256Hex(bytes) {
    const hash = await crypto.subtle.digest("SHA-256", bytes);
    return bytesToHex(new Uint8Array(hash));
}

// -- Passphrase encryption (PBKDF2 → AES-GCM) --------------------------------

const PBKDF2_ITERS = 200_000;

async function deriveWrapKey(passphrase, salt) {
    const base = await crypto.subtle.importKey(
        "raw", new TextEncoder().encode(passphrase),
        { name: "PBKDF2" }, false, ["deriveKey"],
    );
    return await crypto.subtle.deriveKey(
        { name: "PBKDF2", salt, iterations: PBKDF2_ITERS, hash: "SHA-256" },
        base,
        { name: "AES-GCM", length: 256 },
        false,
        ["encrypt", "decrypt"],
    );
}

export async function encryptWithPassphrase(plaintext, passphrase) {
    const salt = crypto.getRandomValues(new Uint8Array(16));
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const wrapKey = await deriveWrapKey(passphrase, salt);
    const ct = await crypto.subtle.encrypt(
        { name: "AES-GCM", iv },
        wrapKey,
        new TextEncoder().encode(plaintext),
    );
    return {
        salt: bytesToBase64url(salt),
        iv: bytesToBase64url(iv),
        ct: bytesToBase64url(new Uint8Array(ct)),
        iters: PBKDF2_ITERS,
    };
}

export async function decryptWithPassphrase(blob, passphrase) {
    const salt = base64urlToBytes(blob.salt);
    const iv = base64urlToBytes(blob.iv);
    const ct = base64urlToBytes(blob.ct);
    const wrapKey = await deriveWrapKey(passphrase, salt);
    const pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv }, wrapKey, ct);
    return new TextDecoder().decode(pt);
}

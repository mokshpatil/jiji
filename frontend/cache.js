// IndexedDB-backed post/endorsement cache with reorg-safe incremental scan.
//
// Stores:
//   posts         { tx_hash, author, body, reply_to, timestamp, gas_fee,
//                   height, block_hash }
//   endorsements  { tx_hash, target, author, amount, message, height }
//   meta          { key, value }        // key = "tip" → {height, hash}
//
// Safety bounds (so a bad node / long chain can't freeze the browser):
//   MAX_FEED_DEPTH        50  // first-run scan and post-reorg fallback
//   MAX_BLOCKS_PER_SCAN  100  // hard cap per refresh call
//   MAX_CACHED_POSTS     500  // LRU eviction by height

import { canonicalBytes, sha256Hex } from "./crypto.js";

const DB_NAME = "jiji-cache";
const DB_VERSION = 1;

export const MAX_FEED_DEPTH = 50;
export const MAX_BLOCKS_PER_SCAN = 100;
export const MAX_CACHED_POSTS = 500;

function openDB() {
    return new Promise((resolve, reject) => {
        const req = indexedDB.open(DB_NAME, DB_VERSION);
        req.onupgradeneeded = () => {
            const db = req.result;
            if (!db.objectStoreNames.contains("posts")) {
                const s = db.createObjectStore("posts", { keyPath: "tx_hash" });
                s.createIndex("height", "height");
            }
            if (!db.objectStoreNames.contains("endorsements")) {
                const s = db.createObjectStore("endorsements", { keyPath: "tx_hash" });
                s.createIndex("target", "target");
                s.createIndex("height", "height");
            }
            if (!db.objectStoreNames.contains("meta")) {
                db.createObjectStore("meta", { keyPath: "key" });
            }
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
    });
}

function promisify(req) {
    return new Promise((resolve, reject) => {
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
    });
}

export class PostCache {
    constructor() { this._db = null; }

    async _open() {
        if (!this._db) this._db = await openDB();
        return this._db;
    }

    async getMeta(key) {
        const db = await this._open();
        const t = db.transaction(["meta"], "readonly");
        const row = await promisify(t.objectStore("meta").get(key));
        return row ? row.value : null;
    }

    async putMeta(key, value) {
        const db = await this._open();
        const t = db.transaction(["meta"], "readwrite");
        await promisify(t.objectStore("meta").put({ key, value }));
    }

    async putPost(post) {
        const db = await this._open();
        const t = db.transaction(["posts"], "readwrite");
        await promisify(t.objectStore("posts").put(post));
    }

    async putEndorsement(e) {
        const db = await this._open();
        const t = db.transaction(["endorsements"], "readwrite");
        await promisify(t.objectStore("endorsements").put(e));
    }

    async getPost(txHash) {
        const db = await this._open();
        const t = db.transaction(["posts"], "readonly");
        return await promisify(t.objectStore("posts").get(txHash));
    }

    async getAllPosts() {
        const db = await this._open();
        const t = db.transaction(["posts"], "readonly");
        return await promisify(t.objectStore("posts").getAll());
    }

    async getEndorsementsFor(targetHash) {
        const db = await this._open();
        const t = db.transaction(["endorsements"], "readonly");
        return await promisify(t.objectStore("endorsements").index("target").getAll(targetHash));
    }

    async getAllEndorsements() {
        const db = await this._open();
        const t = db.transaction(["endorsements"], "readonly");
        return await promisify(t.objectStore("endorsements").getAll());
    }

    async wipe() {
        const db = await this._open();
        const t = db.transaction(["posts", "endorsements", "meta"], "readwrite");
        await Promise.all([
            promisify(t.objectStore("posts").clear()),
            promisify(t.objectStore("endorsements").clear()),
            promisify(t.objectStore("meta").clear()),
        ]);
    }

    // LRU eviction: when over cap, drop the oldest rows by height.
    async enforceCap() {
        const db = await this._open();
        const countTx = db.transaction(["posts"], "readonly");
        const count = await promisify(countTx.objectStore("posts").count());
        if (count <= MAX_CACHED_POSTS) return;
        const toEvict = count - MAX_CACHED_POSTS;
        const t = db.transaction(["posts"], "readwrite");
        const idx = t.objectStore("posts").index("height");
        await new Promise((resolve, reject) => {
            let removed = 0;
            const req = idx.openCursor();
            req.onsuccess = () => {
                const cursor = req.result;
                if (!cursor || removed >= toEvict) return resolve();
                cursor.delete();
                removed++;
                cursor.continue();
            };
            req.onerror = () => reject(req.error);
        });
    }
}

// -- Hash helpers ------------------------------------------------------------

export async function blockHashOf(block) {
    return await sha256Hex(canonicalBytes(block.header));
}

export async function txHashOf(txDict) {
    // Match the node: SHA-256 over canonical JSON with the signature stripped.
    // Coinbase txs don't have a signature field, which is fine — destructuring
    // an absent key is a no-op.
    const { signature, ...rest } = txDict;
    return await sha256Hex(canonicalBytes(rest));
}

// -- Incremental scan --------------------------------------------------------

export async function syncFeed(rpc, cache, { maxDepth = MAX_FEED_DEPTH } = {}) {
    const info = await rpc.getNodeInfo();
    const tipHeight = info.height;
    if (tipHeight < 0) return { scanned: 0, tipHeight };

    const savedTip = await cache.getMeta("tip");
    const tipBlock = await rpc.getBlockByHeight(tipHeight);
    const tipHash = await blockHashOf(tipBlock);

    if (savedTip && savedTip.hash === tipHash) {
        return { scanned: 0, tipHeight };
    }

    // Walk backwards from the new tip collecting fresh blocks until we hit
    // a cached ancestor (common case: tip advanced by a few blocks) or
    // exhaust the budget / detect a reorg (fallback: rescan depth).
    const toScan = [tipBlock];
    let cursor = tipBlock;
    let cursorHash = tipHash;
    let steps = 0;
    let reorg = false;

    while (savedTip && steps < MAX_BLOCKS_PER_SCAN) {
        if (cursor.header.height === 0) break;
        if (cursor.header.height <= savedTip.height) {
            if (cursorHash === savedTip.hash) {
                toScan.pop();  // savedTip is already cached
                break;
            }
            reorg = true;
            break;
        }
        const parent = await rpc.getBlockByHash(cursor.header.prev_hash);
        if (!parent) break;
        toScan.push(parent);
        cursor = parent;
        cursorHash = await blockHashOf(parent);
        steps++;
    }

    let blocks;
    if (!savedTip || reorg || steps >= MAX_BLOCKS_PER_SCAN) {
        // First run, or reorg detected, or walked too far — scan fresh.
        if (reorg) await cache.wipe();
        const start = Math.max(0, tipHeight - maxDepth + 1);
        blocks = [];
        for (let h = start; h <= tipHeight; h++) {
            blocks.push(await rpc.getBlockByHeight(h));
        }
    } else {
        blocks = toScan.reverse();  // oldest → newest for reply_to resolution
    }

    for (const block of blocks) {
        const height = block.header.height;
        const bhash = await blockHashOf(block);
        for (const txn of block.transactions) {
            if (txn.tx_type === "post") {
                await cache.putPost({
                    tx_hash: await txHashOf(txn),
                    author: txn.author,
                    body: txn.body,
                    reply_to: txn.reply_to || null,
                    timestamp: txn.timestamp,
                    gas_fee: txn.gas_fee,
                    height,
                    block_hash: bhash,
                });
            } else if (txn.tx_type === "endorse") {
                await cache.putEndorsement({
                    tx_hash: await txHashOf(txn),
                    target: txn.target,
                    author: txn.author,
                    amount: txn.amount,
                    message: txn.message || "",
                    height,
                });
            }
        }
    }

    await cache.enforceCap();
    await cache.putMeta("tip", { height: tipHeight, hash: tipHash });

    return { scanned: blocks.length, tipHeight, reorg };
}

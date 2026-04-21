// jiji client — UI orchestration.
//
// State machine: lock → node → app. The wallet blob lives in localStorage;
// keypair material is held in memory only and cleared on lock.

import {
    generateKeypair,
    importFromPrivateHex,
    signCanonical,
    encryptWithPassphrase,
    decryptWithPassphrase,
} from "./crypto.js";
import { RpcClient, RpcError } from "./api.js";
import { PostCache, syncFeed, MAX_FEED_DEPTH } from "./cache.js";

// -- Persistence keys --------------------------------------------------------

const LS_WALLET = "jiji.wallet";      // encrypted keypair blob + publicHex
const LS_NODE   = "jiji.node";        // {url, token}

// -- State -------------------------------------------------------------------

const state = {
    keypair: null,    // unlocked: {privateKey, publicKey, privateHex, publicHex}
    rpc: null,        // RpcClient once node is connected
    cache: new PostCache(),
    view: "feed",
    replyTo: null,    // {tx_hash, author_hex} when composing a reply
    syncing: false,
    refreshTimer: null,
};

// -- DOM helpers -------------------------------------------------------------

const $ = (id) => document.getElementById(id);
const show = (el) => el.classList.remove("hidden");
const hide = (el) => el.classList.add("hidden");

function showOnly(panelId, ...allIds) {
    for (const id of allIds) hide($(id));
    show($(panelId));
}

function setErr(elId, msg) {
    const el = $(elId);
    el.textContent = msg || "";
}

function setOk(elId, msg) {
    const el = $(elId);
    el.textContent = msg || "";
}

let toastTimer = null;
function toast(msg, kind = "info") {
    const el = $("toast");
    el.textContent = msg;
    el.classList.toggle("err", kind === "err");
    el.classList.remove("hidden");
    requestAnimationFrame(() => el.classList.add("show"));
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
        el.classList.remove("show");
        setTimeout(() => el.classList.add("hidden"), 250);
    }, 2200);
}

function shortHex(hex, n = 8) {
    if (!hex) return "—";
    return hex.slice(0, n) + "…" + hex.slice(-4);
}

function fmtTime(tsSec) {
    if (!tsSec) return "";
    const d = new Date(tsSec * 1000);
    const now = Date.now();
    const dSec = (now - d.getTime()) / 1000;
    if (dSec < 60) return "just now";
    if (dSec < 3600) return `${Math.floor(dSec / 60)}m ago`;
    if (dSec < 86400) return `${Math.floor(dSec / 3600)}h ago`;
    if (dSec < 86400 * 7) return `${Math.floor(dSec / 86400)}d ago`;
    return d.toLocaleDateString();
}

// -- Lock screen flows -------------------------------------------------------

function showLockScreen() {
    hide($("node-screen"));
    hide($("app"));
    show($("lock-screen"));
    setErr("lock-error", "");
    const wallet = localStorage.getItem(LS_WALLET);
    if (wallet) {
        showOnly("unlock-panel", "unlock-panel", "create-panel", "import-panel");
        $("unlock-pass").focus();
    } else {
        showOnly("create-panel", "unlock-panel", "create-panel", "import-panel");
        $("create-pass").focus();
    }
}

async function handleUnlock() {
    setErr("lock-error", "");
    const pass = $("unlock-pass").value;
    const raw = localStorage.getItem(LS_WALLET);
    if (!raw) { setErr("lock-error", "no wallet stored"); return; }
    let blob;
    try { blob = JSON.parse(raw); }
    catch { setErr("lock-error", "wallet blob corrupted"); return; }
    try {
        const privHex = await decryptWithPassphrase(blob.enc, pass);
        const kp = await importFromPrivateHex(privHex);
        state.keypair = kp;
        $("unlock-pass").value = "";
        await postUnlock();
    } catch (e) {
        setErr("lock-error", "wrong passphrase");
    }
}

async function handleCreate() {
    setErr("lock-error", "");
    const p1 = $("create-pass").value;
    const p2 = $("create-pass2").value;
    if (p1.length < 6) { setErr("lock-error", "passphrase must be at least 6 chars"); return; }
    if (p1 !== p2)     { setErr("lock-error", "passphrases don't match"); return; }
    try {
        const kp = await generateKeypair();
        const enc = await encryptWithPassphrase(kp.privateHex, p1);
        localStorage.setItem(LS_WALLET, JSON.stringify({ enc, publicHex: kp.publicHex }));
        state.keypair = kp;
        $("create-pass").value = "";
        $("create-pass2").value = "";
        await postUnlock();
    } catch (e) {
        setErr("lock-error", "could not create wallet: " + e.message);
    }
}

async function handleImport() {
    setErr("lock-error", "");
    const hex = $("import-hex").value.trim();
    const p1 = $("import-pass").value;
    const p2 = $("import-pass2").value;
    if (!/^[0-9a-fA-F]{64}$/.test(hex)) {
        setErr("lock-error", "private key must be 64 hex characters"); return;
    }
    if (p1.length < 6)   { setErr("lock-error", "passphrase must be at least 6 chars"); return; }
    if (p1 !== p2)       { setErr("lock-error", "passphrases don't match"); return; }
    try {
        const kp = await importFromPrivateHex(hex);
        const enc = await encryptWithPassphrase(kp.privateHex, p1);
        localStorage.setItem(LS_WALLET, JSON.stringify({ enc, publicHex: kp.publicHex }));
        state.keypair = kp;
        $("import-hex").value = "";
        $("import-pass").value = "";
        $("import-pass2").value = "";
        await postUnlock();
    } catch (e) {
        setErr("lock-error", "import failed: " + e.message);
    }
}

function handleWipe() {
    if (!confirm("Delete wallet from this device? This cannot be undone " +
                 "unless you have the private key backed up.")) return;
    localStorage.removeItem(LS_WALLET);
    showLockScreen();
}

// -- Node screen flow --------------------------------------------------------

async function postUnlock() {
    hide($("lock-screen"));
    const nodeCfg = localStorage.getItem(LS_NODE);
    if (nodeCfg) {
        try {
            const { url, token } = JSON.parse(nodeCfg);
            const rpc = new RpcClient({ url, token });
            await rpc.getNodeInfo();  // probe
            state.rpc = rpc;
            await enterApp();
            return;
        } catch (e) {
            // fall through to node screen
            console.warn("saved node unreachable:", e.message);
        }
    }
    showNodeScreen();
}

function showNodeScreen() {
    hide($("lock-screen"));
    hide($("app"));
    show($("node-screen"));
    setErr("node-error", "");
    const saved = localStorage.getItem(LS_NODE);
    if (saved) {
        try {
            const { url, token } = JSON.parse(saved);
            $("node-url").value = url || "";
            $("node-token").value = token || "";
        } catch {}
    } else {
        $("node-url").value = $("node-url").value || "http://127.0.0.1:9332";
    }
    $("node-url").focus();
}

async function handleNodeConnect() {
    setErr("node-error", "");
    const url = $("node-url").value.trim();
    const token = $("node-token").value.trim();
    if (!url) { setErr("node-error", "RPC URL required"); return; }
    const rpc = new RpcClient({ url, token });
    try {
        await rpc.getNodeInfo();
    } catch (e) {
        setErr("node-error", "could not reach node: " + e.message);
        return;
    }
    localStorage.setItem(LS_NODE, JSON.stringify({ url, token }));
    state.rpc = rpc;
    await enterApp();
}

// -- Main app ----------------------------------------------------------------

async function enterApp() {
    hide($("lock-screen"));
    hide($("node-screen"));
    show($("app"));
    renderSettings();
    switchView("feed");
    await refresh();
    if (state.refreshTimer) clearInterval(state.refreshTimer);
    state.refreshTimer = setInterval(() => { refresh().catch(() => {}); }, 15000);
}

function lockApp() {
    state.keypair = null;
    if (state.refreshTimer) { clearInterval(state.refreshTimer); state.refreshTimer = null; }
    showLockScreen();
}

function switchView(view) {
    state.view = view;
    for (const v of ["feed", "compose", "wallet", "settings"]) {
        const el = $("view-" + v);
        if (v === view) show(el); else hide(el);
        const tab = document.querySelector(`.tab[data-view="${v}"]`);
        if (tab) tab.classList.toggle("active", v === view);
    }
    if (view === "wallet") renderWallet().catch(e => toast("wallet: " + e.message, "err"));
    if (view === "compose") {
        renderReplyBanner();
        $("post-body").focus();
    }
    if (view === "feed") renderFeed().catch(e => toast("feed: " + e.message, "err"));
}

// -- Refresh / sync ----------------------------------------------------------

async function refresh() {
    if (!state.rpc || state.syncing) return;
    state.syncing = true;
    $("refresh-btn").classList.add("spinning");
    try {
        const info = await state.rpc.getNodeInfo();
        $("status-height").textContent = info.height;
        const res = await syncFeed(state.rpc, state.cache, { maxDepth: MAX_FEED_DEPTH });
        if (res.reorg) toast("reorg detected — refreshed cache");
        if (state.view === "feed") await renderFeed();
        if (state.view === "wallet") await renderWallet();
    } catch (e) {
        console.warn("refresh failed:", e);
        toast("sync failed: " + e.message, "err");
    } finally {
        state.syncing = false;
        $("refresh-btn").classList.remove("spinning");
    }
}

// -- Feed rendering ----------------------------------------------------------

async function renderFeed() {
    const list = $("feed-list");
    const [posts, endorsements] = await Promise.all([
        state.cache.getAllPosts(),
        state.cache.getAllEndorsements(),
    ]);

    const byTarget = new Map();
    for (const e of endorsements) {
        const arr = byTarget.get(e.target) || [];
        arr.push(e);
        byTarget.set(e.target, arr);
    }

    posts.sort((a, b) => (b.height - a.height) || (b.timestamp - a.timestamp));
    const postByHash = new Map(posts.map(p => [p.tx_hash, p]));

    $("feed-info").textContent = posts.length
        ? `${posts.length} posts cached`
        : "";

    if (posts.length === 0) {
        list.innerHTML = `<div class="empty">No posts yet. Be the first — tap Compose.</div>`;
        return;
    }

    const myPub = state.keypair?.publicHex;
    const frag = document.createDocumentFragment();

    for (const p of posts) {
        const endList = byTarget.get(p.tx_hash) || [];
        const tipTotal = endList.reduce((s, e) => s + (e.amount || 0), 0);

        const article = document.createElement("article");
        article.className = "post";

        const head = document.createElement("div");
        head.className = "post-head";

        const author = document.createElement("span");
        author.className = "post-author" + (p.author === myPub ? " mine" : "");
        author.textContent = p.author === myPub ? "you" : shortHex(p.author);
        author.title = p.author;

        const time = document.createElement("span");
        time.className = "post-time";
        time.textContent = fmtTime(p.timestamp);
        time.title = new Date(p.timestamp * 1000).toLocaleString();

        head.append(author, time);
        article.append(head);

        if (p.reply_to) {
            const parent = postByHash.get(p.reply_to);
            const ref = document.createElement("div");
            ref.className = "post-reply-ref";
            if (parent) {
                ref.textContent = `↩ reply to ${parent.author === myPub ? "you" : shortHex(parent.author)}: "${parent.body.slice(0, 60)}${parent.body.length > 60 ? "…" : ""}"`;
            } else {
                ref.textContent = `↩ reply to ${shortHex(p.reply_to, 12)}`;
            }
            article.append(ref);
        }

        const body = document.createElement("div");
        body.className = "post-body";
        body.textContent = p.body;
        article.append(body);

        const actions = document.createElement("div");
        actions.className = "post-actions";

        const endorseBtn = document.createElement("button");
        const endorseCount = endList.length;
        endorseBtn.textContent = endorseCount
            ? `♥ ${endorseCount}${tipTotal ? ` · ${tipTotal} tipped` : ""}`
            : "♥ endorse";
        endorseBtn.disabled = (p.author === myPub);
        endorseBtn.title = endorseBtn.disabled
            ? "can't endorse your own post"
            : "endorse (optionally tip)";
        endorseBtn.addEventListener("click", () => endorsePrompt(p));
        actions.append(endorseBtn);

        const replyBtn = document.createElement("button");
        replyBtn.textContent = "reply";
        replyBtn.addEventListener("click", () => {
            state.replyTo = { tx_hash: p.tx_hash, author: p.author, body: p.body };
            switchView("compose");
        });
        actions.append(replyBtn);

        const hashSpan = document.createElement("span");
        hashSpan.className = "muted";
        hashSpan.style.marginLeft = "auto";
        hashSpan.textContent = shortHex(p.tx_hash, 10);
        hashSpan.title = p.tx_hash;
        actions.append(hashSpan);

        article.append(actions);
        frag.append(article);
    }

    list.replaceChildren(frag);
}

// -- Compose / post ----------------------------------------------------------

function renderReplyBanner() {
    const banner = $("reply-banner");
    const title = $("compose-title");
    if (state.replyTo) {
        title.textContent = "Reply";
        banner.innerHTML = "";
        const text = document.createElement("span");
        text.className = "banner-text";
        text.textContent = `Replying to ${shortHex(state.replyTo.author)}: "${state.replyTo.body.slice(0, 80)}${state.replyTo.body.length > 80 ? "…" : ""}"`;
        const cancel = document.createElement("button");
        cancel.className = "small";
        cancel.textContent = "cancel";
        cancel.addEventListener("click", () => {
            state.replyTo = null;
            renderReplyBanner();
        });
        banner.append(text, cancel);
        show(banner);
    } else {
        title.textContent = "New post";
        hide(banner);
    }
}

async function submitPost() {
    setErr("compose-error", ""); setOk("compose-ok", "");
    const body = $("post-body").value;
    const gasFee = parseInt($("post-fee").value, 10);
    if (!body.trim())               { setErr("compose-error", "post body can't be empty"); return; }
    if (body.length > 300)          { setErr("compose-error", "max 300 characters"); return; }
    if (!Number.isInteger(gasFee) || gasFee < 1) { setErr("compose-error", "gas fee must be ≥ 1"); return; }

    const btn = $("post-submit");
    btn.disabled = true;
    try {
        const pub = state.keypair.publicHex;
        const { nonce: nextNonce } = await state.rpc.getNextNonce(pub);
        const tx = {
            tx_type: "post",
            author: pub,
            nonce: nextNonce,
            timestamp: Math.floor(Date.now() / 1000),
            body,
            reply_to: state.replyTo ? state.replyTo.tx_hash : null,
            gas_fee: gasFee,
        };
        const sig = await signCanonical(state.keypair.privateKey, tx);
        tx.signature = sig;
        const res = await state.rpc.submitTransaction(tx);
        setOk("compose-ok", `posted — tx ${shortHex(res.tx_hash, 10)}`);
        $("post-body").value = "";
        state.replyTo = null;
        renderReplyBanner();
        setTimeout(() => { refresh().catch(() => {}); switchView("feed"); }, 600);
    } catch (e) {
        setErr("compose-error", rpcMsg(e));
    } finally {
        btn.disabled = false;
    }
}

// -- Endorse -----------------------------------------------------------------

async function endorsePrompt(post) {
    const tipStr = prompt(
        `Endorse this post by ${shortHex(post.author)}.\n\n` +
        "Tip amount (0 for plain endorsement):",
        "0");
    if (tipStr === null) return;
    const tip = parseInt(tipStr, 10);
    if (Number.isNaN(tip) || tip < 0) { toast("invalid tip amount", "err"); return; }
    const msg = prompt("Message (optional, max 200 chars):", "") || "";
    if (msg.length > 200) { toast("message too long", "err"); return; }

    try {
        const pub = state.keypair.publicHex;
        const { nonce: nextNonce } = await state.rpc.getNextNonce(pub);
        const tx = {
            tx_type: "endorse",
            author: pub,
            nonce: nextNonce,
            target: post.tx_hash,
            amount: tip,
            message: msg,
            gas_fee: 1,
        };
        const sig = await signCanonical(state.keypair.privateKey, tx);
        tx.signature = sig;
        const res = await state.rpc.submitTransaction(tx);
        toast(`endorsed — ${shortHex(res.tx_hash, 10)}`);
        setTimeout(() => refresh().catch(() => {}), 500);
    } catch (e) {
        toast(rpcMsg(e), "err");
    }
}

// -- Wallet / transfer -------------------------------------------------------

async function renderWallet() {
    if (!state.keypair || !state.rpc) return;
    try {
        const pub = state.keypair.publicHex;
        const [acct, next] = await Promise.all([
            state.rpc.getAccount(pub),
            state.rpc.getNextNonce(pub),
        ]);
        $("w-balance").textContent   = acct.balance;
        $("w-nonce").textContent     = acct.nonce;
        $("w-next-nonce").textContent = next.nonce;
    } catch (e) {
        toast("wallet load failed: " + e.message, "err");
    }
}

async function submitTransfer() {
    setErr("xfer-error", ""); setOk("xfer-ok", "");
    const to = $("xfer-to").value.trim();
    const amount = parseInt($("xfer-amount").value, 10);
    const fee = parseInt($("xfer-fee").value, 10);
    if (!/^[0-9a-fA-F]{64}$/.test(to))             { setErr("xfer-error", "recipient must be 64 hex chars"); return; }
    if (!Number.isInteger(amount) || amount < 1)   { setErr("xfer-error", "amount must be ≥ 1"); return; }
    if (!Number.isInteger(fee) || fee < 1)         { setErr("xfer-error", "gas fee must be ≥ 1"); return; }
    if (to.toLowerCase() === state.keypair.publicHex.toLowerCase()) {
        setErr("xfer-error", "can't send to yourself"); return;
    }

    const btn = $("xfer-submit");
    btn.disabled = true;
    try {
        const pub = state.keypair.publicHex;
        const { nonce: nextNonce } = await state.rpc.getNextNonce(pub);
        const tx = {
            tx_type: "transfer",
            sender: pub,
            recipient: to.toLowerCase(),
            amount,
            nonce: nextNonce,
            gas_fee: fee,
        };
        const sig = await signCanonical(state.keypair.privateKey, tx);
        tx.signature = sig;
        const res = await state.rpc.submitTransaction(tx);
        setOk("xfer-ok", `sent — tx ${shortHex(res.tx_hash, 10)}`);
        $("xfer-to").value = ""; $("xfer-amount").value = "";
        setTimeout(() => renderWallet(), 600);
    } catch (e) {
        setErr("xfer-error", rpcMsg(e));
    } finally {
        btn.disabled = false;
    }
}

// -- Settings ----------------------------------------------------------------

function renderSettings() {
    const pub = state.keypair?.publicHex || "—";
    $("set-pubkey").textContent = pub;
    const nodeCfg = localStorage.getItem(LS_NODE);
    try {
        $("set-node").textContent = JSON.parse(nodeCfg).url || "—";
    } catch { $("set-node").textContent = "—"; }
    hide($("export-output"));
}

async function handleExport() {
    const pass = prompt("Enter your passphrase to reveal the private key:");
    if (!pass) return;
    try {
        const raw = localStorage.getItem(LS_WALLET);
        const blob = JSON.parse(raw);
        const privHex = await decryptWithPassphrase(blob.enc, pass);
        $("export-hex").textContent = privHex;
        show($("export-output"));
    } catch {
        toast("wrong passphrase", "err");
    }
}

async function copyToClipboard(text) {
    try {
        await navigator.clipboard.writeText(text);
        toast("copied");
    } catch {
        toast("copy failed", "err");
    }
}

// -- Helpers -----------------------------------------------------------------

function rpcMsg(e) {
    if (e instanceof RpcError) return e.message;
    return e.message || String(e);
}

// -- Wire up -----------------------------------------------------------------

function init() {
    // Lock screen
    $("unlock-btn").addEventListener("click", handleUnlock);
    $("unlock-pass").addEventListener("keydown", (e) => { if (e.key === "Enter") handleUnlock(); });
    $("create-btn").addEventListener("click", handleCreate);
    $("create-pass2").addEventListener("keydown", (e) => { if (e.key === "Enter") handleCreate(); });
    $("import-btn").addEventListener("click", handleImport);
    $("show-create").addEventListener("click", () => {
        showOnly("create-panel", "unlock-panel", "create-panel", "import-panel");
        $("create-pass").focus();
    });
    $("show-import").addEventListener("click", () => {
        showOnly("import-panel", "unlock-panel", "create-panel", "import-panel");
        $("import-hex").focus();
    });
    $("back-from-create").addEventListener("click", () => {
        const hasWallet = !!localStorage.getItem(LS_WALLET);
        showOnly(hasWallet ? "unlock-panel" : "create-panel",
                 "unlock-panel", "create-panel", "import-panel");
    });
    $("back-from-import").addEventListener("click", () => {
        const hasWallet = !!localStorage.getItem(LS_WALLET);
        showOnly(hasWallet ? "unlock-panel" : "create-panel",
                 "unlock-panel", "create-panel", "import-panel");
    });
    $("wipe-btn").addEventListener("click", handleWipe);

    // Node screen
    $("node-connect").addEventListener("click", handleNodeConnect);
    $("node-url").addEventListener("keydown", (e) => { if (e.key === "Enter") handleNodeConnect(); });
    $("node-token").addEventListener("keydown", (e) => { if (e.key === "Enter") handleNodeConnect(); });

    // Tabs
    for (const tab of document.querySelectorAll(".tab")) {
        tab.addEventListener("click", () => switchView(tab.dataset.view));
    }

    // Refresh
    $("refresh-btn").addEventListener("click", () => refresh().catch(() => {}));

    // Compose
    $("post-submit").addEventListener("click", submitPost);

    // Wallet
    $("xfer-submit").addEventListener("click", submitTransfer);

    // Settings
    $("copy-pubkey").addEventListener("click", () => copyToClipboard(state.keypair.publicHex));
    $("change-node").addEventListener("click", showNodeScreen);
    $("export-key").addEventListener("click", handleExport);
    $("copy-priv").addEventListener("click", () => copyToClipboard($("export-hex").textContent));
    $("lock-wallet").addEventListener("click", lockApp);
    $("delete-wallet").addEventListener("click", handleWipe);

    // Kick off
    showLockScreen();
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
} else {
    init();
}

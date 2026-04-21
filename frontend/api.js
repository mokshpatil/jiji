// JSON-RPC 2.0 client for a jiji node. Bearer-auth optional.

export class RpcError extends Error {
    constructor(code, message) {
        super(`RPC error ${code}: ${message}`);
        this.code = code;
    }
}

export class RpcClient {
    constructor({ url, token }) {
        this.url = url.replace(/\/+$/, "");
        this.token = token || "";
    }

    async call(method, params = {}) {
        const headers = { "Content-Type": "application/json" };
        if (this.token) headers["Authorization"] = `Bearer ${this.token}`;
        const body = JSON.stringify({ jsonrpc: "2.0", id: 1, method, params });
        let resp;
        try {
            resp = await fetch(this.url, { method: "POST", headers, body });
        } catch (e) {
            throw new Error(`network error: ${e.message}`);
        }
        if (resp.status === 401) throw new Error("unauthorized (bad RPC token)");
        if (resp.status === 429) throw new Error("rate limited — slow down");
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        let data;
        try {
            data = await resp.json();
        } catch {
            throw new Error("invalid JSON response");
        }
        if (data.error) throw new RpcError(data.error.code, data.error.message);
        return data.result;
    }

    // -- Convenience wrappers --

    getNodeInfo()              { return this.call("get_node_info"); }
    getLatestBlock()           { return this.call("get_latest_block"); }
    getBlockByHeight(h)        { return this.call("get_block", { height: h }); }
    getBlockByHash(hash)       { return this.call("get_block", { hash }); }
    getAccount(pubkey)         { return this.call("get_account", { pubkey }); }
    getNextNonce(pubkey)       { return this.call("get_next_nonce", { pubkey }); }
    getMempool()               { return this.call("get_mempool"); }
    getTransaction(txHash)     { return this.call("get_transaction", { tx_hash: txHash }); }
    submitTransaction(tx)      { return this.call("submit_transaction", { transaction: tx }); }
}

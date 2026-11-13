'use strict';
// FreeHand WhatsApp bridge: a small local HTTP server between FreeHand and
// WhatsApp (via whatsapp.js).
//
//   WhatsApp ──> bridge ──POST /api/gateway/whatsapp──> FreeHand
//   FreeHand ──POST /reply, /pair──> bridge ──> WhatsApp
//
// Security (Nov 2026):
//  - Listens on 127.0.0.1 only by default. It used to listen on every
//    interface with no authentication, so anyone on the network could send
//    WhatsApp messages from the linked number or request a pairing code.
//  - Every route except /health needs X-Bridge-Secret, and the bridge sends
//    the same secret to FreeHand. BRIDGE_SECRET must equal FreeHand's
//    settings "bridge_secret" (`freehand gateway secret` prints it).
//  - /health no longer reveals the pairing code.

const crypto = require('crypto');
const path = require('path');
const express = require('express');

try {
    process.loadEnvFile(path.join(__dirname, '.env'));
} catch (e) {
    // No .env file: configuration comes from the real environment.
}

const DEFAULT_PORT = 8765; // must match core/gateway.py BRIDGE_PORT
const MIN_SECRET_LENGTH = 16;

function secretMatches(provided, expected) {
    const a = Buffer.from(String(provided || ''));
    const b = Buffer.from(String(expected || ''));
    return b.length > 0 && a.length === b.length && crypto.timingSafeEqual(a, b);
}

function createApp(wa, secret) {
    const app = express();
    app.use(express.json({ limit: '64kb' }));

    // Unauthenticated liveness only — nothing sensitive.
    app.get('/health', (req, res) => {
        res.json({ status: 'ok', whatsapp_connected: wa.status().connected });
    });

    app.use((req, res, next) => {
        if (!secretMatches(req.get('X-Bridge-Secret'), secret)) {
            return res.status(401).json({ error: 'Invalid or missing X-Bridge-Secret' });
        }
        next();
    });

    app.post('/pair', async (req, res) => {
        const phone = req.body && req.body.phone;
        if (!phone) {
            return res.status(400).json({ error: 'phone number required' });
        }
        const result = await wa.requestPairingCode(phone);
        res.status(result.error ? 409 : 200).json(result);
    });

    app.get('/pairing', (req, res) => {
        const s = wa.status();
        res.json({ code: s.pairingCode, phone: s.pairingPhone, connected: s.connected });
    });

    app.post('/reply', async (req, res) => {
        const { jid, text } = req.body || {};
        if (typeof jid !== 'string' || typeof text !== 'string' || !jid || !text) {
            return res.status(400).json({ sent: false, error: 'jid and text (strings) required' });
        }
        if (!wa.isDirectChat(jid)) {
            return res.status(400).json({ sent: false, error: 'only one-to-one chats are supported' });
        }
        if (!wa.status().connected) {
            return res.status(503).json({ sent: false, error: 'WhatsApp not connected' });
        }
        try {
            await wa.sendText(jid, text);
            res.json({ sent: true });
        } catch (e) {
            res.status(500).json({ sent: false, error: e.message });
        }
    });

    return app;
}

function makeForwarder(freehandUrl, secret, fetchImpl = fetch) {
    const url = `${freehandUrl.replace(/\/+$/, '')}/api/gateway/whatsapp`;
    return async ({ jid, text, id, fromSelf = false }) => {
        const resp = await fetchImpl(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Bridge-Secret': secret },
            body: JSON.stringify({ remote_jid: jid, text, message_id: id, from_self: fromSelf === true }),
            signal: AbortSignal.timeout(15000),
        });
        if (!resp.ok) {
            // FreeHand replies through /reply, so the body is not needed;
            // only failures are worth logging.
            throw new Error(`FreeHand returned HTTP ${resp.status}`);
        }
    };
}

function main() {
    const secret = (process.env.BRIDGE_SECRET || '').trim();
    if (secret.length < MIN_SECRET_LENGTH) {
        console.error(
            `BRIDGE_SECRET is missing or shorter than ${MIN_SECRET_LENGTH} characters.\n` +
            "Run `freehand gateway secret` and put the value it prints in this folder's .env as\n" +
            'BRIDGE_SECRET=<value>. The bridge will not start without it.'
        );
        process.exit(1);
    }
    const host = process.env.BRIDGE_HOST || '127.0.0.1';
    const port = Number(process.env.BRIDGE_PORT || DEFAULT_PORT);
    const freehandUrl = process.env.FREEHAND_URL || process.env.PYTHON_API_URL || 'http://127.0.0.1:8000';

    if (!['127.0.0.1', 'localhost', '::1'].includes(host)) {
        console.warn(
            `WARNING: BRIDGE_HOST=${host} exposes the bridge beyond this computer. Anyone who can reach it ` +
            'and knows BRIDGE_SECRET can send WhatsApp messages from the linked number.'
        );
    }

    const wa = require('./whatsapp');
    wa.onMessage(makeForwarder(freehandUrl, secret));
    wa.start().catch((e) => {
        console.error(`Could not start WhatsApp: ${e.message}`);
        process.exit(1);
    });

    createApp(wa, secret).listen(port, host, () => {
        console.log(`WhatsApp bridge listening on http://${host}:${port} (FreeHand at ${freehandUrl})`);
    });
}

if (require.main === module) {
    main();
}

module.exports = { createApp, makeForwarder, secretMatches, DEFAULT_PORT };

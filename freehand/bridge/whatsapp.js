'use strict';
// WhatsApp connection (Baileys). Owned by bridge-server.js, which starts it,
// registers the incoming-message handler, and exposes /pair and /reply.
//
// Nov 2026 rewrite. The old version:
//  - exported `sock` / `isConnected` by value at require time (null/false),
//    so the server always thought WhatsApp was disconnected and /reply
//    could never send;
//  - routed messages through two JSON queue files that were cleared after
//    fire-and-forget requests, losing anything that arrived or failed in
//    between, and queued FreeHand's whole JSON reply as message text;
//  - forwarded group chats, broadcasts and status updates to the agent.

const fs = require('fs');
const path = require('path');
const pino = require('pino');
const {
    makeWASocket,
    useMultiFileAuthState,
    DisconnectReason,
    fetchLatestBaileysVersion,
} = require('@whiskeysockets/baileys');

// The linked-device session. Treat this folder like a password: anyone
// with a copy can read and send your WhatsApp messages.
const AUTH_DIR = process.env.WHATSAPP_AUTH_DIR || path.join(__dirname, 'auth_info_baileys');
const RECONNECT_DELAY_MS = 3000;

const logger = pino({ level: process.env.LOG_LEVEL || 'warn' });

const state = {
    sock: null,
    connected: false,
    pairingCode: null,
    pairingPhone: null,
};
let messageHandler = null;

// Only one-to-one chats reach the agent. Groups (@g.us), broadcast lists,
// status updates and channels (@newsletter) are ignored.
function isDirectChat(jid) {
    return typeof jid === 'string' && (jid.endsWith('@s.whatsapp.net') || jid.endsWith('@lid'));
}

function textOf(message) {
    if (!message) return '';
    return message.conversation || message.extendedTextMessage?.text || '';
}

async function start() {
    const { state: authState, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
    const { version } = await fetchLatestBaileysVersion();

    const sock = makeWASocket({
        version,
        auth: authState,
        printQRInTerminal: false,
        markOnlineOnConnect: false,
        // Skip the optional props/blocklist/privacy fetch on connect. The
        // bridge never uses them, and WhatsApp often times out answering,
        // which Baileys logs as a scary "unexpected error in 'init queries'".
        fireInitQueries: false,
        logger,
    });
    state.sock = sock;

    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('connection.update', (update) => {
        const { connection, lastDisconnect } = update;
        if (connection === 'open') {
            state.connected = true;
            state.pairingCode = null;
            console.log('WhatsApp connected.');
        } else if (connection === 'close') {
            state.connected = false;
            const code = lastDisconnect?.error?.output?.statusCode;
            if (code === DisconnectReason.loggedOut) {
                console.log('WhatsApp logged out; clearing the saved session. Pair again to reconnect.');
                try { fs.rmSync(AUTH_DIR, { recursive: true, force: true }); } catch (e) { /* ignore */ }
            } else {
                console.log(`WhatsApp connection closed (${code}); reconnecting in ${RECONNECT_DELAY_MS / 1000}s.`);
            }
            // Always start a fresh socket: after a logout this yields a new,
            // unregistered session that can be paired again.
            setTimeout(() => start().catch((e) => console.error(`Reconnect failed: ${e.message}`)), RECONNECT_DELAY_MS);
        }
    });

    sock.ev.on('messages.upsert', async ({ messages, type }) => {
        if (type !== 'notify') return; // history sync, not new messages
        for (const msg of messages) {
            if (!msg.message || msg.key.fromMe) continue;
            const jid = msg.key.remoteJid;
            if (!isDirectChat(jid)) continue;
            const text = textOf(msg.message).trim();
            if (!text || !messageHandler) continue;
            try {
                await messageHandler({ jid, text, id: msg.key.id });
            } catch (e) {
                console.error(`Failed to hand message to FreeHand: ${e.message}`);
            }
        }
    });
}

function onMessage(handler) {
    messageHandler = handler;
}

async function sendText(jid, text) {
    if (!state.sock || !state.connected) {
        throw new Error('WhatsApp not connected');
    }
    await state.sock.sendMessage(jid, { text });
}

async function requestPairingCode(phone) {
    if (!state.sock) {
        return { error: 'WhatsApp is still starting. Try again in a few seconds.' };
    }
    if (state.connected) {
        return { error: 'Already linked. Unlink the device in WhatsApp first to pair again.' };
    }
    try {
        const digits = String(phone).replace(/[^0-9]/g, '');
        const code = await state.sock.requestPairingCode(digits);
        state.pairingCode = code;
        state.pairingPhone = digits;
        return { code, phone: digits };
    } catch (e) {
        return { error: e.message };
    }
}

function status() {
    return {
        connected: state.connected,
        pairingPhone: state.pairingPhone,
        pairingCode: state.pairingCode,
    };
}

module.exports = { start, onMessage, sendText, requestPairingCode, status, isDirectChat, AUTH_DIR };

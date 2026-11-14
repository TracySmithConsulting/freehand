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
    downloadMediaMessage,
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

// Disappearing and view-once messages wrap the real message one level down.
function unwrap(message) {
    let m = message;
    for (let i = 0; i < 3 && m; i++) {
        const inner = m.ephemeralMessage?.message || m.viewOnceMessage?.message
            || m.viewOnceMessageV2?.message || m.documentWithCaptionMessage?.message;
        if (!inner) break;
        m = inner;
    }
    return m;
}

function textOf(message) {
    const m = unwrap(message);
    if (!m) return '';
    return m.conversation || m.extendedTextMessage?.text || '';
}

// Voice notes (and other audio), up to about 10 minutes. FreeHand turns
// them into text on its own computer, like Telegram voice notes.
const MAX_AUDIO_SECONDS = 600;
const MAX_AUDIO_BYTES = 20 * 1024 * 1024;
function audioOf(message) {
    const a = unwrap(message)?.audioMessage;
    if (!a) return null;
    const seconds = Number(a.seconds || 0);
    const bytes = Number(a.fileLength || 0);
    return {
        mimetype: a.mimetype || 'audio/ogg; codecs=opus',
        seconds,
        voiceNote: a.ptt === true,
        tooLong: seconds > MAX_AUDIO_SECONDS || bytes > MAX_AUDIO_BYTES,
    };
}

// "27821234567:12@s.whatsapp.net" -> { user: "27821234567", server: "s.whatsapp.net" }
function splitJid(jid) {
    const [left, server = ''] = String(jid || '').split('@');
    return { user: left.split(':')[0], server };
}

// True if `jid` is the linked account's own "Message yourself" chat.
// `me` is sock.user: { id: "<number>:<device>@s.whatsapp.net", lid: "<id>:<device>@lid" }.
function isSelfChat(jid, me) {
    if (!me) return false;
    const chat = splitJid(jid);
    if (!chat.user) return false;
    const own = chat.server === 'lid' ? splitJid(me.lid) : splitJid(me.id);
    return own.user !== '' && own.user === chat.user;
}

// IDs of messages this bridge sent. In the self chat, the bridge's own
// replies come back as messages "from me"; without this they would be
// answered again, forever.
const SENT_ID_LIMIT = 500;
const sentIds = new Set();
function rememberSent(id) {
    if (!id) return;
    sentIds.add(id);
    if (sentIds.size > SENT_ID_LIMIT) sentIds.delete(sentIds.values().next().value);
}

// Decide what to do with one incoming message. Returns null to ignore it,
// or { jid, text, id, fromSelf } to hand to FreeHand.
//  - Other people's one-to-one chats go to FreeHand (FreeHand checks its
//    allow list).
//  - Messages the account owner types in other people's chats are never
//    forwarded: those are their own conversations.
//  - The owner's "Message yourself" chat is forwarded with fromSelf, so
//    someone with only one WhatsApp number can still talk to FreeHand.
function classifyMessage(msg, me, sent = sentIds) {
    if (!msg || !msg.message || !msg.key) return null;
    const jid = msg.key.remoteJid;
    if (!isDirectChat(jid)) return null;
    const text = textOf(msg.message).trim();
    const audio = text ? null : audioOf(msg.message);
    if (!text && !audio) return null;
    const id = msg.key.id;
    const out = { jid, text, id, fromSelf: false };
    if (audio) out.audio = audio;
    if (msg.key.fromMe) {
        if (!isSelfChat(jid, me)) return null;
        if (id && sent.has(id)) return null; // our own reply
        out.fromSelf = true;
    }
    return out;
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
            const incoming = classifyMessage(msg, sock.user);
            if (!incoming || !messageHandler) continue;
            if (incoming.audio && !incoming.audio.tooLong) {
                // Fetch the recording from WhatsApp now; FreeHand can't.
                try {
                    const data = await downloadMediaMessage(msg, 'buffer', {}, {
                        logger, reuploadRequest: sock.updateMediaMessage,
                    });
                    incoming.audio.data = Buffer.from(data).toString('base64');
                } catch (e) {
                    console.error(`Couldn't download a voice note: ${e.message}`);
                    incoming.audio.failed = true;
                }
            }
            try {
                await messageHandler(incoming);
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
    const sent = await state.sock.sendMessage(jid, { text });
    rememberSent(sent?.key?.id);
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

module.exports = {
    start, onMessage, sendText, requestPairingCode, status,
    isDirectChat, isSelfChat, classifyMessage, audioOf, AUTH_DIR,
};

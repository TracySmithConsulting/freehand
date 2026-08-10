require('dotenv').config();
const { makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion } = require('@whiskeysockets/baileys');
const fs = require('fs');
const path = require('path');
const pino = require('pino');

const logger = pino({ timestamp: () => ',"time":"' + new Date().toJSON() + '"' });

const AUTH_DIR = path.join(__dirname, 'auth_info_baileys');
const QUEUE_FILE = path.join(__dirname, 'message_queue.json');
const RESPONSE_QUEUE_FILE = path.join(__dirname, 'response_queue.json');

let sock = null;
let isConnected = false;
let pairingCode = null;
let pairingPhone = null;

function loadQueue(filePath) {
    try {
        if (fs.existsSync(filePath)) {
            return JSON.parse(fs.readFileSync(filePath, 'utf8'));
        }
    } catch (e) {}
    return [];
}

function saveQueue(filePath, data) {
    fs.writeFileSync(filePath, JSON.stringify(data, null, 2));
}

function appendQueue(filePath, item) {
    const queue = loadQueue(filePath);
    queue.push(item);
    saveQueue(filePath, queue);
}

async function connectToWhatsApp() {
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
    const { version, isLatest } = await fetchLatestBaileysVersion();

    sock = makeWASocket({
        version,
        auth: state,
        printQRInTerminal: false,
        markOnlineOnConnect: false,
        generateHighQualityLinkPreview: true,
        logger: logger,
    });

    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('connection.update', async (update) => {
        const { connection, lastDisconnect } = update;
        if (connection === 'close') {
            const shouldReconnect = lastDisconnect?.error?.output?.statusCode !== DisconnectReason.loggedOut;
            console.log(`Connection closed due to ${lastDisconnect?.error?.output?.statusCode}, reconnecting: ${shouldReconnect}`);
            if (shouldReconnect) {
                connectToWhatsApp();
            } else {
                isConnected = false;
                // Clear auth on logout
                try { fs.rmSync(AUTH_DIR, { recursive: true, force: true }); } catch (e) {}
            }
        } else if (connection === 'open') {
            isConnected = true;
            console.log('WhatsApp connected successfully!');
            // Process any queued messages
            processMessageQueue();
        }
    });

    sock.ev.on('messages.upsert', async ({ messages }) => {
        for (const msg of messages) {
            if (!msg.message) continue;
            const text = msg.message.conversation || msg.message.extendedTextMessage?.text || '';
            if (!text) continue;

            const remoteJid = msg.key.remoteJid;
            const fromMe = msg.key.fromMe;

            if (fromMe) continue;

            // Normalize JID
            let cleanJid = remoteJid;
            if (cleanJid.includes('@s.whatsapp.net')) {
                cleanJid = cleanJid.replace('@s.whatsapp.net', '');
            }

            console.log(`Received message from ${cleanJid}: ${text.substring(0, 100)}`);

            // Queue message for Python bridge to process
            appendQueue(QUEUE_FILE, {
                jid: remoteJid,
                clean_jid: cleanJid,
                text: text,
                timestamp: new Date().toISOString(),
            });
        }
    });
}

async function requestPairingCode(phone) {
    if (!sock) {
        return { error: 'WhatsApp not connected. Start the bridge first.' };
    }
    try {
        const code = await sock.requestPairingCode(phone.replace(/[^0-9]/g, ''));
        pairingCode = code;
        pairingPhone = phone;
        console.log(`Pairing code for ${phone}: ${code}`);
        return { code, phone };
    } catch (e) {
        return { error: e.message };
    }
}

async function processMessageQueue() {
    const queue = loadQueue(QUEUE_FILE);
    if (queue.length === 0) return;

    const baseUrl = process.env.PYTHON_API_URL || 'http://127.0.0.1:8000';
    const http = require('http');

    for (const item of queue) {
        const body = JSON.stringify({
            remote_jid: item.jid,
            clean_jid: item.clean_jid,
            text: item.text,
        });

        const options = {
            hostname: '127.0.0.1',
            port: 8766,
            path: '/inbound',
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(body),
            },
        };

        const req = http.request(options, (res) => {
            let data = '';
            res.on('data', (chunk) => { data += chunk; });
            res.on('end', () => {
                try {
                    const resp = JSON.parse(data);
                    if (resp.response) {
                        // Queue response to send back
                        appendQueue(RESPONSE_QUEUE_FILE, {
                            jid: item.jid,
                            text: resp.response,
                        });
                    }
                } catch (e) {}
            });
        });
        req.on('error', (e) => {
            console.error(`Failed to send to Python: ${e.message}`);
        });
        req.write(body);
        req.end();
    }

    // Clear processed queue
    saveQueue(QUEUE_FILE, []);
}

async function processResponseQueue() {
    const queue = loadQueue(RESPONSE_QUEUE_FILE);
    if (queue.length === 0) return;

    for (const item of queue) {
        if (!sock || !isConnected) continue;
        try {
            await sock.sendMessage(item.jid, { text: item.text });
            console.log(`Sent response to ${item.jid}`);
        } catch (e) {
            console.error(`Failed to send: ${e.message}`);
        }
    }

    saveQueue(RESPONSE_QUEUE_FILE, []);
}

// Start connection
connectToWhatsApp();

// Poll queues
setInterval(processMessageQueue, 2000);
setInterval(processResponseQueue, 1000);

// Export for bridge-server.js
module.exports = { sock, requestPairingCode, isConnected, getPairingCode: () => pairingCode };

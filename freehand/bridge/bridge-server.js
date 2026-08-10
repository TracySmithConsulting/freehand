const express = require('express');
const fs = require('fs');
const path = require('path');

const app = express();
app.use(express.json());

const PORT = process.env.BRIDGE_PORT || 8766;
const PYTHON_PORT = process.env.PYTHON_PORT || 8000;

// Load whatsapp module
const whatsappModule = require('./whatsapp');

// Health check
app.get('/health', (req, res) => {
    res.json({
        status: 'ok',
        whatsapp_connected: whatsappModule.isConnected,
        pairing_code: whatsappModule.getPairingCode(),
    });
});

// Request pairing code
app.post('/pair', async (req, res) => {
    const { phone } = req.body;
    if (!phone) {
        return res.status(400).json({ error: 'phone number required' });
    }
    try {
        const result = await whatsappModule.requestPairingCode(phone);
        res.json(result);
    } catch (e) {
        res.status(500).json({ error: e.message });
    }
});

// Get pairing code status
app.get('/pairing', (req, res) => {
    res.json({
        code: whatsappModule.getPairingCode(),
        phone: whatsappModule.pairingPhone,
        connected: whatsappModule.isConnected,
    });
});

// Receive inbound messages from WhatsApp and forward to Python
app.post('/inbound', async (req, res) => {
    const { remote_jid, clean_jid, text } = req.body;
    if (!remote_jid || !text) {
        return res.status(400).json({ error: 'remote_jid and text required' });
    }

    try {
        const response = await forwardToPython(remote_jid, text);
        res.json({ response });
    } catch (e) {
        res.status(500).json({ error: e.message });
    }
});

// Send response back to WhatsApp
app.post('/reply', async (req, res) => {
    const { jid, text } = req.body;
    if (!jid || !text) {
        return res.status(400).json({ error: 'jid and text required' });
    }

    try {
        if (whatsappModule.sock && whatsappModule.isConnected) {
            await whatsappModule.sock.sendMessage(jid, { text });
            res.json({ sent: true });
        } else {
            res.json({ sent: false, error: 'WhatsApp not connected' });
        }
    } catch (e) {
        res.status(500).json({ error: e.message });
    }
});

async function forwardToPython(jid, text) {
    return new Promise((resolve, reject) => {
        const body = JSON.stringify({ remote_jid: jid, text });
        const options = {
            hostname: '127.0.0.1',
            port: PYTHON_PORT,
            path: '/api/gateway/whatsapp',
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(body),
            },
        };

        const req = require('http').request(options, (res) => {
            let data = '';
            res.on('data', (chunk) => { data += chunk; });
            res.on('end', () => {
                try {
                    resolve(JSON.parse(data));
                } catch (e) {
                    resolve({ response: data });
                }
            });
        });
        req.on('error', reject);
        req.write(body);
        req.end();
    });
}

app.listen(PORT, () => {
    console.log(`WhatsApp bridge server listening on port ${PORT}`);
});

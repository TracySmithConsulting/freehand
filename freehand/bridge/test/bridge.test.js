'use strict';
// Run with: npm test   (or: node --test test/)
// Uses a fake WhatsApp module, so no phone or network is needed.

const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');

const { createApp, makeForwarder, secretMatches } = require('../bridge-server');
const { isDirectChat, isSelfChat, classifyMessage } = require('../whatsapp');

const SECRET = 'a-long-test-bridge-secret';

function fakeWa({ connected = true } = {}) {
    const sent = [];
    return {
        sent,
        status: () => ({ connected, pairingCode: 'ABCD-1234', pairingPhone: '27821234567' }),
        isDirectChat,
        sendText: async (jid, text) => { sent.push({ jid, text }); },
        requestPairingCode: async (phone) => ({ code: 'WXYZ-9876', phone }),
    };
}

async function withServer(app, fn) {
    const server = app.listen(0, '127.0.0.1');
    await new Promise((r) => server.once('listening', r));
    const base = `http://127.0.0.1:${server.address().port}`;
    try {
        await fn(base);
    } finally {
        await new Promise((r) => server.close(r));
    }
}

function post(base, path, body, secret) {
    const headers = { 'Content-Type': 'application/json' };
    if (secret !== undefined) headers['X-Bridge-Secret'] = secret;
    return fetch(base + path, { method: 'POST', headers, body: JSON.stringify(body) });
}

test('health is open but does not leak the pairing code', async () => {
    await withServer(createApp(fakeWa(), SECRET), async (base) => {
        const r = await fetch(base + '/health');
        assert.equal(r.status, 200);
        const body = await r.json();
        assert.deepEqual(body, { status: 'ok', whatsapp_connected: true, whatsapp_paused: false });
    });
});

test('reply, pair and pairing require the secret', async () => {
    const wa = fakeWa();
    await withServer(createApp(wa, SECRET), async (base) => {
        for (const secret of [undefined, '', 'wrong-secret', SECRET + 'x']) {
            assert.equal((await post(base, '/reply', { jid: '1@s.whatsapp.net', text: 'hi' }, secret)).status, 401);
            assert.equal((await post(base, '/pair', { phone: '27821234567' }, secret)).status, 401);
        }
        const pairing = await fetch(base + '/pairing');
        assert.equal(pairing.status, 401);
        assert.deepEqual(wa.sent, []);
    });
});

test('reply with the secret sends the message', async () => {
    const wa = fakeWa();
    await withServer(createApp(wa, SECRET), async (base) => {
        const r = await post(base, '/reply', { jid: '27821234567@s.whatsapp.net', text: 'hello' }, SECRET);
        assert.equal(r.status, 200);
        assert.deepEqual(await r.json(), { sent: true });
        assert.deepEqual(wa.sent, [{ jid: '27821234567@s.whatsapp.net', text: 'hello' }]);
    });
});

test('reply refuses groups and bad input, and reports disconnection', async () => {
    const wa = fakeWa();
    await withServer(createApp(wa, SECRET), async (base) => {
        assert.equal((await post(base, '/reply', { jid: '123@g.us', text: 'x' }, SECRET)).status, 400);
        assert.equal((await post(base, '/reply', { jid: '1@s.whatsapp.net', text: { not: 'text' } }, SECRET)).status, 400);
        assert.deepEqual(wa.sent, []);
    });
    await withServer(createApp(fakeWa({ connected: false }), SECRET), async (base) => {
        assert.equal((await post(base, '/reply', { jid: '1@s.whatsapp.net', text: 'x' }, SECRET)).status, 503);
    });
});

test('an empty configured secret never matches', () => {
    assert.equal(secretMatches('', ''), false);
    assert.equal(secretMatches(undefined, ''), false);
    assert.equal(secretMatches(SECRET, SECRET), true);
});

test('only one-to-one chats count as direct', () => {
    assert.equal(isDirectChat('27821234567@s.whatsapp.net'), true);
    assert.equal(isDirectChat('1234567890@lid'), true);
    for (const jid of ['123-456@g.us', 'status@broadcast', '1@newsletter', '', null]) {
        assert.equal(isDirectChat(jid), false, String(jid));
    }
});

test('forwarder sends the secret and message id to FreeHand', async () => {
    const received = [];
    const server = http.createServer((req, res) => {
        let data = '';
        req.on('data', (c) => { data += c; });
        req.on('end', () => {
            received.push({ path: req.url, secret: req.headers['x-bridge-secret'], body: JSON.parse(data) });
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end('{"ok":true}');
        });
    });
    await new Promise((r) => server.listen(0, '127.0.0.1', r));
    try {
        const forward = makeForwarder(`http://127.0.0.1:${server.address().port}/`, SECRET);
        await forward({ jid: '27821234567@s.whatsapp.net', text: 'hi', id: 'MSG1' });
    } finally {
        await new Promise((r) => server.close(r));
    }
    assert.deepEqual(received, [{
        path: '/api/gateway/whatsapp',
        secret: SECRET,
        body: { remote_jid: '27821234567@s.whatsapp.net', text: 'hi', message_id: 'MSG1', from_self: false },
    }]);
});

test('forwarder raises on a FreeHand error so it gets logged', async () => {
    const fakeFetch = async () => ({ ok: false, status: 401 });
    const forward = makeForwarder('http://x', SECRET, fakeFetch);
    await assert.rejects(forward({ jid: '1@s.whatsapp.net', text: 'x', id: '1' }), /HTTP 401/);
});

// ── Self chat ("Message yourself") ─────────────────────────────────────

const ME = { id: '27821234567:12@s.whatsapp.net', lid: '98765432:12@lid' };

function msg(remoteJid, { fromMe = false, id = 'M1', text = 'hello' } = {}) {
    return { key: { remoteJid, fromMe, id }, message: { conversation: text } };
}

test('the linked number\'s own chat is recognised in both id forms', () => {
    assert.equal(isSelfChat('27821234567@s.whatsapp.net', ME), true);
    assert.equal(isSelfChat('98765432@lid', ME), true);
    assert.equal(isSelfChat('27820000000@s.whatsapp.net', ME), false);
    assert.equal(isSelfChat('27821234567@lid', ME), false); // number is not a lid
    assert.equal(isSelfChat('27821234567@s.whatsapp.net', null), false);
});

test('messages from other people are forwarded, not marked as self', () => {
    assert.deepEqual(classifyMessage(msg('27820000000@s.whatsapp.net'), ME, new Set()),
        { jid: '27820000000@s.whatsapp.net', text: 'hello', id: 'M1', fromSelf: false });
});

test('the owner typing in the self chat is forwarded as self', () => {
    const out = classifyMessage(msg('98765432@lid', { fromMe: true }), ME, new Set());
    assert.deepEqual(out, { jid: '98765432@lid', text: 'hello', id: 'M1', fromSelf: true });
});

test('the owner\'s chats with other people are never forwarded', () => {
    assert.equal(classifyMessage(msg('27820000000@s.whatsapp.net', { fromMe: true }), ME, new Set()), null);
});

test('the bridge\'s own replies in the self chat are not answered again', () => {
    const sent = new Set(['REPLY1']);
    assert.equal(classifyMessage(msg('27821234567@s.whatsapp.net', { fromMe: true, id: 'REPLY1' }), ME, sent), null);
});

test('groups, empty and non-text messages are ignored', () => {
    assert.equal(classifyMessage(msg('123-456@g.us'), ME, new Set()), null);
    assert.equal(classifyMessage(msg('27820000000@s.whatsapp.net', { text: '   ' }), ME, new Set()), null);
    assert.equal(classifyMessage({ key: { remoteJid: '1@s.whatsapp.net' }, message: { imageMessage: {} } }, ME, new Set()), null);
});

test('forwarder marks self-chat messages', async () => {
    let body;
    const fakeFetch = async (url, opts) => { body = JSON.parse(opts.body); return { ok: true }; };
    await makeForwarder('http://x', SECRET, fakeFetch)({ jid: '98765432@lid', text: 'hi', id: '9', fromSelf: true });
    assert.equal(body.from_self, true);
});

// ── Voice notes (Nov 2026) ─────────────────────────────────────────────

const VME = { id: '27820000000:3@s.whatsapp.net', lid: '111:3@lid' };

test('a voice note from someone is forwarded as audio', () => {
    const msg = { key: { remoteJid: '27821234567@s.whatsapp.net', id: 'v1', fromMe: false },
        message: { audioMessage: { ptt: true, seconds: 4, mimetype: 'audio/ogg; codecs=opus', fileLength: 9000 } } };
    const out = classifyMessage(msg, VME, new Set());
    assert.equal(out.text, '');
    assert.equal(out.audio.voiceNote, true);
    assert.equal(out.audio.tooLong, false);
    assert.equal(out.audio.mimetype, 'audio/ogg; codecs=opus');
});

test('a disappearing voice note is unwrapped', () => {
    const msg = { key: { remoteJid: '27821234567@s.whatsapp.net', id: 'v2', fromMe: false },
        message: { ephemeralMessage: { message: { audioMessage: { ptt: true, seconds: 2 } } } } };
    assert.ok(classifyMessage(msg, VME, new Set()).audio);
});

test('a very long recording is marked too long, not downloaded', () => {
    const msg = { key: { remoteJid: '27821234567@s.whatsapp.net', id: 'v3', fromMe: false },
        message: { audioMessage: { seconds: 3600 } } };
    assert.equal(classifyMessage(msg, VME, new Set()).audio.tooLong, true);
});

test('voice notes in group chats and other people\'s chats are not forwarded', () => {
    const group = { key: { remoteJid: '123@g.us', id: 'v4', fromMe: false }, message: { audioMessage: { ptt: true } } };
    const mine = { key: { remoteJid: '27821234567@s.whatsapp.net', id: 'v5', fromMe: true }, message: { audioMessage: { ptt: true } } };
    assert.equal(classifyMessage(group, VME, new Set()), null);
    assert.equal(classifyMessage(mine, VME, new Set()), null);
});

test('a voice note in "Message yourself" is forwarded from the owner', () => {
    const msg = { key: { remoteJid: '27820000000@s.whatsapp.net', id: 'v6', fromMe: true }, message: { audioMessage: { ptt: true } } };
    const out = classifyMessage(msg, VME, new Set());
    assert.equal(out.fromSelf, true);
    assert.ok(out.audio);
});

test('the forwarder sends the recording to FreeHand', async () => {
    const calls = [];
    const fakeFetch = async (url, opts) => { calls.push({ url, body: JSON.parse(opts.body) }); return { ok: true }; };
    const forward = makeForwarder('http://freehand:8000', SECRET, fakeFetch);
    await forward({ jid: '27821234567@s.whatsapp.net', text: '', id: 'v7',
        audio: { mimetype: 'audio/ogg', seconds: 3, data: 'T2dnUw==' } });
    assert.equal(calls[0].body.text, '');
    assert.deepEqual(calls[0].body.audio, { mimetype: 'audio/ogg', seconds: 3, data_b64: 'T2dnUw==', too_long: false, failed: false });
});

// ── WhatsApp on/off from FreeHand (Nov 2026) ───────────────────────────

test('pause and resume need the secret and report the state', async () => {
    let paused = false;
    const wa = { ...fakeWa(), pause: async () => { paused = true; return { connected: false, paused }; },
        resume: async () => { paused = false; return { connected: true, paused }; },
        status: () => ({ connected: !paused, paused }) };
    await withServer(createApp(wa, SECRET), async (base) => {
        assert.equal((await post(base, '/pause', {}, 'wrong-secret')).status, 401);
        assert.deepEqual(await (await post(base, '/pause', {}, SECRET)).json(), { connected: false, paused: true });
        assert.deepEqual(await (await fetch(base + '/health')).json(), { status: 'ok', whatsapp_connected: false, whatsapp_paused: true });
        assert.deepEqual(await (await post(base, '/resume', {}, SECRET)).json(), { connected: true, paused: false });
    });
});

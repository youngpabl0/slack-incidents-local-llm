// Read-only Slack export (same-origin /api/<method> on app.slack.com; the team URL fails with CORS) through the user's own logged-in web session in a CDP Chrome.
// Usage:
//   node slack_export.mjs [--port 9222] [--out ./slack-export/<date>] [--pages 60] [--channels alerts,devops] [--since 2022-11-01]
//   node slack_export.mjs --all [--first alert,incident,devops,prod,ops,support,release] [--reuse-index] [--no-dm] [--no-outside]
// Only the read methods listed in READ_ONLY below can be called; anything else stops the script.
// --all dumps channels in priority order: names containing a --first word, then channels where the user wrote most, DMs last.
// --reuse-index: keep threads.jsonl from an interrupted --all run instead of repeating the search (channels with .done are skipped anyway).
// Default mode: every thread where the user wrote at least one message (search "from:<@me>", all channels he can see),
// each thread fetched in full with conversations.replies.
// --channels: also export the full history of the named channels (every top-level message plus every thread),
// limited by --since. Output: threads.jsonl (one thread per line), channels/<name>.jsonl, users.json, summary.json;
// with --all also threads_outside.jsonl (his threads in conversations he has left).
// Nothing is sent anywhere except Slack's own API from the logged-in tab.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
const arg = (k, d) => { const i = process.argv.indexOf(k); return i > 0 ? process.argv[i + 1] : d; };
const PORT = arg('--port', '9222');
const today = new Date().toISOString().slice(0, 10);
const OUT = (arg('--out', `./slack-export/${today}`)).replace(/^~/, os.homedir());
const MAXP = +arg('--pages', process.argv.includes('--all') ? '999' : '60');
const CHANNELS = (arg('--channels', '') || '').split(',').map((s) => s.trim().replace(/^#/, '')).filter(Boolean);
const SINCE = arg('--since', '2022-11-01');
const FIRST = (arg('--first', 'alert,incident,devops,prod,ops,support,release')).split(',').map((s) => s.trim()).filter(Boolean);
// The whole list of Slack methods this script may call. All of them only read; nothing posts, reacts or marks as read.
const READ_ONLY = new Set(['auth.test', 'search.messages', 'users.list', 'users.info', 'bots.info', 'users.conversations', 'conversations.list', 'conversations.history', 'conversations.replies']);
fs.mkdirSync(path.join(OUT, 'channels'), { recursive: true });
// The CDP socket dies when the laptop sleeps: every call has a timeout, and a dead socket is reopened on the same tab.
let ws, n = 0; const p = new Map();
const connect = async () => {
  const tabs = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const t = tabs.find((x) => x.type === 'page' && /app\.slack\.com|\.slack\.com\/client/.test(x.url));
  if (!t) return false;
  ws = new WebSocket(t.webSocketDebuggerUrl);
  await new Promise((r, j) => { ws.addEventListener('open', r, { once: true }); ws.addEventListener('error', j, { once: true }); });
  ws.addEventListener('message', (e) => { const m = JSON.parse(e.data); if (m.id && p.has(m.id)) { p.get(m.id)(m); p.delete(m.id); } });
  ws.addEventListener('close', () => { for (const r of p.values()) r({ closed: true }); p.clear(); });
  return true;
};
if (!(await connect())) { console.log(`no Slack tab on port ${PORT}: open https://app.slack.com/client in that Chrome and log in`); process.exit(1); }
const send = (method, params = {}) => new Promise((r) => {
  if (ws.readyState !== 1) return r({ closed: true });
  const id = ++n; const tm = setTimeout(() => { p.delete(id); r({ timeout: true }); }, 120000);
  p.set(id, (m) => { clearTimeout(tm); r(m); }); ws.send(JSON.stringify({ id, method, params }));
});
const js = async (expr) => { const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true }); if (r.closed || r.timeout) throw new Error(`cdp ${r.closed ? 'closed' : 'timeout'}`); if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0, 300)); return r.result?.result?.value; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const cfg = await js(`(() => { const c = JSON.parse(localStorage.getItem('localConfig_v2') || '{}'); const tid = c.lastActiveTeamId || Object.keys(c.teams || {})[0]; const tm = (c.teams || {})[tid] || {}; return { tid, domain: tm.domain, ok: !!tm.token && !!tm.url }; })()`);
if (!cfg?.ok) { console.log('Slack tab is not logged in: log in at https://app.slack.com/client in that Chrome'); process.exit(2); }
const api = async (method, params) => {
  if (!READ_ONLY.has(method)) throw new Error(`${method} is not in the read-only list`);
  for (let a = 0; a < 30; a++) {
    let r;
    try { r = await js(`(async () => { const c = JSON.parse(localStorage.getItem('localConfig_v2')); const tm = c.teams[${JSON.stringify(cfg.tid)}]; const fd = new FormData(); fd.append('token', tm.token); const prm = ${JSON.stringify(params)}; for (const k in prm) fd.append(k, prm[k]); const res = await fetch('/api/${method}', { method: 'POST', body: fd, credentials: 'include' }); return { status: res.status, retry: res.headers.get('retry-after'), body: await res.json().catch(() => null) }; })()`); }
    catch (e) { // asleep, offline after wake, or the tab reloaded: wait and reconnect, then retry the same call
      if (!/^cdp |Failed to fetch|NetworkError|Execution context|Cannot find context/.test(e.message)) throw e;
      console.log(`${method}: ${e.message.slice(0, 80)}; retry ${a + 1}/30 in 20 s`); await sleep(20000);
      if (ws.readyState !== 1) await connect().catch(() => false);
      continue;
    }
    if (r.status === 429 || r.body?.error === 'ratelimited') { await sleep(((+r.retry) || 20) * 1000); continue; }
    return r.body;
  }
  throw new Error('gave up after 30 tries: ' + method);
};
const me = await api('auth.test', {});
if (!me?.ok) { console.log('auth.test failed', JSON.stringify(me).slice(0, 200)); process.exit(3); }
console.log(`team ${cfg.domain}, user ${me.user} (${me.user_id}) -> ${OUT}`);
const users = new Map(), bots = new Set(['USLACKBOT']);
{ let cur; do { const r = await api('users.list', { limit: 1000, ...(cur ? { cursor: cur } : {}) }); if (!r?.ok) break; for (const u of r.members) { users.set(u.id, u.real_name || u.profile?.real_name || u.name); if (u.is_bot) bots.add(u.id); } cur = r.response_metadata?.next_cursor; await sleep(3000); } while (cur); }
// alert channels are full of threads where only bots answer ("resolved"): those are not fetched, the root message is kept
const humanThread = (m) => (m.reply_count || 0) > 0 && !(m.reply_users?.length && m.reply_users.every((id) => bots.has(id)));
console.log('users', users.size);
const uname = async (id) => { if (!id) return '?'; if (users.has(id)) return users.get(id); const r = /^B/.test(id) ? await api('bots.info', { bot: id }) : await api('users.info', { user: id }); const nm = r?.user?.real_name || r?.user?.name || r?.bot?.name || id; users.set(id, nm); await sleep(300); return nm; };
const msgOut = async (m) => ({ ts: m.ts, user: await uname(m.user || m.bot_id), bot: !!m.bot_id, reply_count: m.reply_count || 0, text: m.text || (m.attachments || []).map((a) => [a.title, a.text || a.fallback].filter(Boolean).join(': ')).join('\n') });
const replies = async (channel, ts) => {
  const msgs = []; let cursor;
  do {
    const r = await api('conversations.replies', { channel, ts, limit: 200, ...(cursor ? { cursor } : {}) });
    if (!r?.ok) { msgs.push({ error: r?.error }); break; }
    for (const m of r.messages) msgs.push(await msgOut(m));
    cursor = r.response_metadata?.next_cursor; await sleep(1300);
  } while (cursor);
  return msgs;
};
// 1. threads with the user's participation
const roots = new Map();
const INDEX = path.join(OUT, 'threads.jsonl');
const REUSE = process.argv.includes('--reuse-index') && process.argv.includes('--all') && fs.existsSync(INDEX) && fs.statSync(INDEX).size > 0;
if (REUSE) { for (const l of fs.readFileSync(INDEX, 'utf8').split('\n').filter(Boolean)) { const th = JSON.parse(l); roots.set(th.channel + ':' + th.thread_ts, th); } console.log(`reused index: ${roots.size} threads`); }
// search.messages serves at most 100 pages (10,000 matches) per query, so it runs month by month, newest first;
// windows overlap by a few days for time zones and matches are deduplicated by channel + ts.
const seenMsg = new Set(); let budget = REUSE ? 0 : MAXP;
const day = (x) => x.toISOString().slice(0, 10);
const months = []; for (let d = new Date(SINCE + 'T00:00:00Z'); d <= new Date(); ) { const a = new Date(d); d.setUTCMonth(d.getUTCMonth() + 1); months.push([a, new Date(d)]); }
for (const [a, b] of months.reverse()) {
  const q = `from:<@${me.user_id}> after:${day(new Date(a - 2 * 864e5))} before:${day(new Date(+b + 864e5))}`;
  for (let page = 1; page <= 100 && budget > 0; page++, budget--) {
    const r = await api('search.messages', { query: q, count: 100, page, sort: 'timestamp', sort_dir: 'desc' });
    if (!r?.ok) { console.log('search failed', r?.error); break; }
    for (const m of r.messages.matches) {
      if (seenMsg.has(m.channel.id + ':' + m.ts)) continue; seenMsg.add(m.channel.id + ':' + m.ts);
      const th = (m.permalink.match(/thread_ts=([\d.]+)/) || [])[1] || m.ts;
      const key = m.channel.id + ':' + th;
      if (!roots.has(key)) roots.set(key, { channel: m.channel.id, channel_name: m.channel.name, is_private: !!m.channel.is_private, thread_ts: th, my_count: 0, permalink: m.permalink });
      roots.get(key).my_count++;
    }
    const pg = r.messages.paging || {};
    if (page === 1 && (pg.pages || 0) > 100) console.log(`WARNING ${day(a)}: ${pg.total} matches, only the newest 10,000 are reachable`);
    console.log(`search ${day(a)} page ${page}/${pg.pages} -> ${seenMsg.size} messages, ${roots.size} threads`);
    await sleep(3200);
    if (page >= (pg.pages || 0)) break;
  }
}
const writeSummary = () => fs.writeFileSync(path.join(OUT, 'summary.json'), JSON.stringify({ team: cfg.domain, user: me.user, user_id: me.user_id, user_name: users.get(me.user_id) || me.user, exported_at: new Date().toISOString(), threads: roots.size, channels: CHANNELS, since: SINCE }, null, 1));
writeSummary(); // early, so ranking can run on a partial export
const out = REUSE ? null : fs.createWriteStream(INDEX);
let i = 0;
const INDEX_ONLY = process.argv.includes('--all'); // --all fetches every thread from channel history; here only mark the user's participation
for (const th of REUSE ? [] : roots.values()) {
  if (INDEX_ONLY) { out.write(JSON.stringify(th) + '\n'); continue; }
  const msgs = await replies(th.channel, th.thread_ts);
  out.write(JSON.stringify({ ...th, n: msgs.length, messages: msgs }) + '\n');
  if (++i % 25 === 0) console.log(`threads fetched ${i}/${roots.size}`);
}
out?.end();
// 2. full history: --all (every conversation the user is a member of, including DMs) or --channels a,b,c
// --no-dm: --all without DMs and group DMs; --no-outside: skip threads in conversations the user has left.
const ALL = process.argv.includes('--all');
const NO_DM = process.argv.includes('--no-dm');
const NO_OUTSIDE = process.argv.includes('--no-outside');
const dumpConv = async (ch, label) => {
  const file = path.join(OUT, 'channels', `${label}.jsonl`), done = file + '.done';
  if (fs.existsSync(done)) { console.log(`${label}: already exported`); return; }
  const f = fs.createWriteStream(file); let cur, k = 0; const oldest = String(Date.parse(SINCE) / 1000);
  do {
    const r = await api('conversations.history', { channel: ch.id, oldest, limit: 200, ...(cur ? { cursor: cur } : {}) });
    if (!r?.ok) {
      if (/invalid_auth|not_authed|token_revoked|account_inactive/.test(r?.error || '')) { console.log(`${label}: ${r.error}: session ended, log in again and rerun with --reuse-index`); process.exit(3); } // no .done marker
      console.log(`${label}: ${r?.error}`); break;
    }
    for (const m of r.messages) {
      const top = await msgOut(m);
      const thread = humanThread(m) ? await replies(ch.id, m.ts) : [];
      f.write(JSON.stringify({ channel: ch.id, channel_name: label, ...top, thread }) + '\n'); k++;
    }
    cur = r.response_metadata?.next_cursor; await sleep(1300);
  } while (cur);
  await new Promise((r) => f.end(r)); fs.writeFileSync(done, String(k)); console.log(`${label}: ${k} messages`);
};
if (ALL) {
  const convs = []; let cursor;
  do { const r = await api('users.conversations', { types: NO_DM ? 'public_channel,private_channel' : 'public_channel,private_channel,mpim,im', exclude_archived: 'false', limit: 999, ...(cursor ? { cursor } : {}) }); if (!r?.ok) { console.log('users.conversations', r?.error); break; } convs.push(...r.channels); cursor = r.response_metadata?.next_cursor; await sleep(3000); } while (cursor);
  fs.writeFileSync(path.join(OUT, 'conversations.json'), JSON.stringify(convs.map((c) => ({ id: c.id, name: c.name, is_im: c.is_im, is_mpim: c.is_mpim, is_private: c.is_private, is_archived: c.is_archived, user: c.user })), null, 1));
  console.log('conversations', convs.length);
  const mine = new Map(); for (const th of roots.values()) mine.set(th.channel, (mine.get(th.channel) || 0) + th.my_count);
  const tier = (c) => (c.is_im || c.is_mpim ? 3 : /(^|-)(dev|stage|staging|test)(-|$)/.test(c.name || '') && /alert/.test(c.name || '') ? 2 : FIRST.some((w) => (c.name || '').includes(w)) ? 0 : 1); // dev/stage alert channels after the rest
  convs.sort((a, b) => tier(a) - tier(b) || (mine.get(b.id) || 0) - (mine.get(a.id) || 0));
  for (const c of convs) {
    const label = c.is_im ? 'dm-' + (await uname(c.user)).replace(/[^\p{L}\p{N}._-]+/gu, '_') : (c.name || c.id);
    await dumpConv(c, label);
  }
  // threads where the user wrote in conversations he is no longer a member of: not covered by the channel dumps
  const member = new Set(convs.map((c) => c.id));
  const outside = [...roots.values()].filter((th) => !member.has(th.channel));
  const ofile = path.join(OUT, 'threads_outside.jsonl');
  if (outside.length && !NO_OUTSIDE && !fs.existsSync(ofile + '.done')) {
    const f = fs.createWriteStream(ofile); let j = 0;
    for (const th of outside) {
      const msgs = await replies(th.channel, th.thread_ts);
      f.write(JSON.stringify({ ...th, n: msgs.length, messages: msgs }) + '\n');
      if (++j % 25 === 0) console.log(`outside threads ${j}/${outside.length}`);
    }
    await new Promise((r) => f.end(r)); fs.writeFileSync(ofile + '.done', String(j)); console.log(`outside threads: ${j}`);
  }
}
if (CHANNELS.length) {
  const all = []; let cursor;
  do { const r = await api('conversations.list', { types: 'public_channel,private_channel', limit: 1000, ...(cursor ? { cursor } : {}) }); if (!r?.ok) break; all.push(...r.channels); cursor = r.response_metadata?.next_cursor; await sleep(1500); } while (cursor);
  const oldest = String(Date.parse(SINCE) / 1000);
  for (const name of CHANNELS) {
    const ch = all.find((c) => c.name === name);
    if (!ch) { console.log(`channel #${name} not found or not visible`); continue; }
    const f = fs.createWriteStream(path.join(OUT, 'channels', `${name}.jsonl`)); let cur, k = 0;
    do {
      const r = await api('conversations.history', { channel: ch.id, oldest, limit: 200, ...(cur ? { cursor: cur } : {}) });
      if (!r?.ok) { console.log(`#${name}: ${r?.error}`); break; }
      for (const m of r.messages) {
        const top = await msgOut(m);
        const thread = humanThread(m) ? await replies(ch.id, m.ts) : [];
        f.write(JSON.stringify({ channel_name: name, ...top, thread }) + '\n'); k++;
      }
      cur = r.response_metadata?.next_cursor; await sleep(1300);
    } while (cur);
    f.end(); console.log(`#${name}: ${k} messages`);
  }
}
fs.writeFileSync(path.join(OUT, 'users.json'), JSON.stringify(Object.fromEntries(users), null, 1));
writeSummary();
console.log('done:', roots.size, 'threads', CHANNELS.length ? `+ ${CHANNELS.length} channels` : '');
ws.close();

import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { createServer } from 'node:net';
import { randomUUID } from 'node:crypto';

const image = process.env.HUIFA_IMAGE ?? 'huifa-rust:local';
const platform = process.env.HUIFA_PLATFORM ?? 'linux/amd64';
const name = 'huifa-browser-check-' + randomUUID();
const volume = name + '-data';
const account = 'browser-acceptance';
const password = randomUUID() + randomUUID();
const cookieCanary = randomUUID();
let origin;
let cookie;
let csrf;
let stream;
const streamAbort = new AbortController();

function docker(args, allowFailure = false) {
  const result = spawnSync('docker', args, { encoding:'utf8',timeout:120_000,maxBuffer:4*1024*1024,shell:false });
  if (!allowFailure && result.status !== 0) throw new Error(result.stderr || result.error?.message || 'Docker command failed');
  return result.stdout.trim();
}
const sleep = milliseconds => new Promise(resolve => setTimeout(resolve,milliseconds));
async function waitFor(check, timeout = 60_000) {
  const deadline = Date.now() + timeout;
  let failure;
  while (Date.now() < deadline) {
    try { if (await check()) return; } catch(error) { failure = error; }
    await sleep(250);
  }
  throw failure ?? new Error('Browser acceptance timed out');
}
async function request(path, body, extra = {}) {
  const headers = { Origin:origin };
  if (cookie) headers.Cookie = cookie;
  if (csrf) headers['X-CSRF-Token'] = csrf;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  Object.assign(headers, extra);
  const response = await fetch(origin+path,{method:body === undefined?'GET':'POST',headers,
    body:body === undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(45_000)});
  const value = await response.json();
  assert.equal(response.status,200,`${path}: ${value.error ?? response.status}`);
  return {response,value};
}

async function installLoginFixture() {
  const port = docker(['exec',name,'sh','-c','cat /data/browser/*/DevToolsActivePort']).split(/\r?\n/)[0];
  assert.match(port,/^\d+$/);
  const sockets = new Set();
  // Reach CDP through docker exec; keep its container port bound to loopback.
  const relay = createServer(socket => {
    sockets.add(socket);
    const pipe = spawn('docker',['exec','-i',name,'bash','-c','exec 3<>"/dev/tcp/127.0.0.1/$1"; cat <&3 & cat >&3','bash',port],{stdio:['pipe','pipe','ignore'],windowsHide:true});
    socket.pipe(pipe.stdin); pipe.stdout.pipe(socket);
    pipe.on('error',()=>socket.destroy()); pipe.on('exit',()=>socket.destroy());
    pipe.stdin.on('error',()=>{});
    socket.on('error',()=>{});
    socket.on('close',()=>{ sockets.delete(socket); pipe.kill(); });
  });
  await new Promise(resolve=>relay.listen(0,'127.0.0.1',resolve));
  let cdp;
  try {
    const base = `http://127.0.0.1:${relay.address().port}`;
    let target;
    await waitFor(async()=>{
      const targets = await (await fetch(base+'/json/list',{signal:AbortSignal.timeout(5000)})).json();
      target=targets.find(target=>target.type==='page' && target.url.startsWith('https://example.com'));
      return Boolean(target);
    },30_000);
    cdp = new WebSocket(base.replace('http:','ws:')+new URL(target.webSocketDebuggerUrl).pathname);
    await new Promise((resolve,reject)=>{
      const timeout=setTimeout(()=>reject(new Error('CDP fixture connection timed out')),5000);
      cdp.onopen=()=>{clearTimeout(timeout);resolve();};
      cdp.onerror=error=>{clearTimeout(timeout);reject(error);};
    });
    let nextId=0;
    const rpc = (method,params={}) => new Promise((resolve,reject)=>{
      const id=++nextId;
      const timeout=setTimeout(()=>{cdp.removeEventListener('message',receive);reject(new Error('CDP fixture timed out'));},5000);
      const receive = event=>{const value=JSON.parse(event.data);if(value.id!==id)return;clearTimeout(timeout);cdp.removeEventListener('message',receive);value.error?reject(new Error(value.error.message)):resolve(value.result);};
      cdp.addEventListener('message',receive);cdp.send(JSON.stringify({id,method,params}));
    });
    await rpc('Page.stopLoading');
    await rpc('Page.setBypassCSP',{enabled:true});
    const {frameTree}=await rpc('Page.getFrameTree');
    await rpc('Page.setDocumentContent',{frameId:frameTree.frame.id,html:`<!doctype html><title>Remote login acceptance</title><body style="margin:0;font:24px sans-serif;background:#fff;color:#111">
      <h1 style="margin:20px">Remote login acceptance</h1>
      <input id="account" aria-label="Account" style="position:absolute;left:30px;top:100px;width:300px;height:40px;font:24px sans-serif">
      <button style="position:absolute;left:30px;top:180px;width:300px;height:50px;font:24px sans-serif"
        onclick="if(document.getElementById('account').value==='accepted'){document.cookie='remoteSession=${cookieCanary}; Path=/; SameSite=Lax';this.textContent='Saved'}">Sign in</button></body>`});
  } finally {
    cdp?.close(); for(const socket of sockets)socket.destroy();
    await new Promise(resolve=>relay.close(resolve));
  }
}

try {
  docker(['volume','create',volume]);
  const options = ['run','-d','--name',name,'--platform',platform,'--read-only','--tmpfs','/tmp:rw,exec,nosuid,nodev,size=536870912',
    '--cap-drop','ALL','--security-opt','no-new-privileges:true','-p','127.0.0.1::19491','-v',`${volume}:/data`,
    '-e',`HUIFA_ACCOUNT=${account}`,'-e',`HUIFA_PASSWORD=${password}`];
  docker([...options,image]);
  origin = 'http://'+docker(['port',name,'19491/tcp']).split(/\r?\n/)[0];
  await waitFor(async () => (await fetch(origin+'/api/session',{signal:AbortSignal.timeout(2000)})).status === 401);
  const login = await request('/api/login',{account,password});
  cookie = login.response.headers.get('set-cookie').split(';')[0];
  csrf = login.value.csrfToken;
  const headers = { Origin:origin,Cookie:cookie };
  let frames = 0;
  let lastFrame;
  const response = await fetch(origin+'/api/browser/events',{headers,signal:streamAbort.signal});
  assert.equal(response.status,200);
  stream = (async () => {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let pending = '';
    try {
      while (true) {
        const {done,value} = await reader.read();
        if (done) break;
        pending += decoder.decode(value,{stream:true});
        let end;
        while ((end=pending.indexOf('\n\n')) >= 0) {
          const event = pending.slice(0,end); pending=pending.slice(end+2);
          const data = event.split('\n').find(line=>line.startsWith('data:'))?.slice(5).trim();
          if (!data) continue;
          const value = JSON.parse(data);
          if (value.kind !== 'browser.frame') continue;
          const jpeg = Buffer.from(value.data.data,'base64');
          assert.equal(jpeg[0],0xff); assert.equal(jpeg[1],0xd8);
          assert.equal(value.data.width,1280); assert.equal(value.data.height,800);
          assert.ok(jpeg.length > 1000);
          lastFrame=jpeg; frames++;
        }
      }
    } catch(error) { if (!streamAbort.signal.aborted) throw error; }
  })();
  for(const url of ['http://127.0.0.1:19491','http://[::ffff:127.0.0.1]:19491']) {
    const response=await fetch(origin+'/api/browser/start',{method:'POST',headers:{Origin:origin,Cookie:cookie,'X-CSRF-Token':csrf,'Content-Type':'application/json'},body:JSON.stringify({url}),signal:AbortSignal.timeout(5000)});
    assert.equal(response.status,400,'local login URL must be rejected');
  }
  const url = 'https://example.com';
  await request('/api/browser/start',{url});
  await installLoginFixture();
  await waitFor(()=>frames > 0);
  const sockets = docker(['exec',name,'cat','/proc/net/tcp']);
  const listeners = sockets.split('\n').slice(1).map(row=>row.trim().split(/\s+/)).filter(row=>row[3] === '0A');
  assert.ok(listeners.some(row=>row[1].startsWith('0100007F:')), 'CDP must listen on loopback');
  assert.ok(!listeners.some(row=>!row[1].startsWith('0100007F:') && row[1] !== '00000000:4C23'), 'Only web port 19491 may listen beyond loopback');
  await request('/api/browser/input',{type:'click',x:120,y:120});
  await request('/api/browser/input',{type:'text',text:'accepted'});
  await request('/api/browser/input',{type:'key',key:'Tab'});
  await request('/api/browser/input',{type:'click',x:150,y:200});
  await sleep(500);
  const saved = (await request('/api/browser/save',{name:'Remote acceptance'})).value;
  assert.ok(saved.id); assert.equal(saved.name,'Remote acceptance');
  assert.ok(!JSON.stringify(saved).includes(cookieCanary));
  assert.equal((await request('/api/browser')).value.active,false);
  assert.equal(docker(['exec',name,'find','/data/browser','-mindepth','1','-maxdepth','1','-type','d']), '');
  assert.ok(!docker(['logs',name]).includes(cookieCanary));
  const stored = docker(['exec',name,'sh','-c','grep -R -a -l "$1" /data/credentials || true','sh',cookieCanary]);
  assert.ok(stored.endsWith(`/${saved.id}.json`));
  assert.ok(lastFrame && frames >= 1);
  await request('/api/browser/start',{url});
  await request('/api/logout',{});
  const denied = await fetch(origin+'/api/browser',{headers:{Origin:origin,Cookie:cookie},signal:AbortSignal.timeout(5000)});
  assert.equal(denied.status,401);
  assert.equal(docker(['exec',name,'find','/data/browser','-mindepth','1','-maxdepth','1','-type','d']), '');
  assert.ok(!docker(['top',name,'-eo','pid,comm']).split('\n').some(command=>command.trim().split(/\s+/)[1]==='chromium'));
  console.log(`Remote browser acceptance passed: ${platform}, authenticated SSE JPEG, mouse/text/key input, local plaintext Cookie save, loopback CDP, profile and logout process cleanup`);
} finally {
  streamAbort.abort(); await stream?.catch(()=>{});
  docker(['rm','-f','-v',name],true); docker(['volume','rm',volume],true);
}

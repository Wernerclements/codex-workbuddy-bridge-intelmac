'use strict';
// OS argv retains the bridge task identity. All protection material stays in
// this WorkBuddy process and its private startup socket; never in Python/logs.
const [, , task, parent, entry, ...args] = process.argv;
if (!/^[0-9a-f-]{36}$/.test(task) || !/^\d+$/.test(parent) || process.ppid !== Number(parent)) process.exit(2);
const fs = require('fs'), path = require('path'), net = require('net'), crypto = require('crypto');
const reaper = require('child_process').spawn(process.execPath,
  [path.join(__dirname, 'worker_reaper.cjs'), task, String(process.pid), parent],
  {env: {ELECTRON_RUN_AS_NODE: '1'}, stdio: 'ignore'});
reaper.on('error', () => { process.exit(3); });
reaper.on('exit', () => { process.exit(3); });
const directory = process.env.WORKBUDDY_BRIDGE_BOOTSTRAP_DIR;
let protection, bootstrap, encoded, readyServer;
let credentialAccepted = false, runtimeReported = false;
let stage = 'product';
function announce() {
  if (credentialAccepted && runtimeReported) process.stdout.write('INTELMAC_PROTECTED_RUNTIME_READY\n');
}
function dispose() {
  try { if (bootstrap && protection) protection.disposeAtRestEncryptionBootstrap(bootstrap); }
  catch { /* No native error details may enter the bridge's output. */ }
  finally {
    for (const key of [bootstrap?.symmetricKey, bootstrap?.userKey]) {
      if (Buffer.isBuffer(key)) key.fill(0);
      else if (Buffer.isBuffer(key?.key)) key.key.fill(0);
    }
  }
  bootstrap = encoded = undefined;
}
function fail(reason = stage) { dispose(); process.stderr.write('INTELMAC_CREDENTIAL_BOOTSTRAP_FAILED\nINTELMAC_STAGE_' + reason + '\n'); process.exit(3); }
function loadProduct() {
  const info = fs.lstatSync(directory);
  if (!info.isDirectory() || info.uid !== process.getuid() || (info.mode & 0o777) !== 0o700) fail();
  // Load the GUI's product snapshot before the CLI freezes its initial model
  // catalog. A path alone can be resolved after the older snapshot is frozen.
  const fd = fs.openSync(process.env.ACC_PRODUCT_CONFIG_PATH, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  try {
    const metadata = fs.fstatSync(fd);
    if (!metadata.isFile() || metadata.uid !== process.getuid() || (metadata.mode & 0o022)
        || metadata.size <= 0 || metadata.size > 8000000) fail();
    const snapshot = fs.readFileSync(fd, 'utf8');
    const configuration = JSON.parse(snapshot);
    const authenticationTypes = {'5.6.2': 'cli-external-link', '5.7.7': 'cli-external-link',
      '5.8.2': 'account-scoped-external-link'};
    const expectedAuthentication = authenticationTypes[process.env.WORKBUDDY_BRIDGE_APP_VERSION];
    if (!expectedAuthentication || configuration.authentication?.id !== 'workbuddy-desktop'
        || configuration.authentication?.type !== expectedAuthentication) fail();
    const model = process.env.WORKBUDDY_BRIDGE_MODEL;
    if (['5.7.7', '5.8.2'].includes(process.env.WORKBUDDY_BRIDGE_APP_VERSION) && model &&
        (!Array.isArray(configuration.models) || !configuration.models.some(item => item.id === model)
         || !Array.isArray(configuration.agents) || !configuration.agents.some(agent =>
           agent.name === 'cli' && Array.isArray(agent.models) && agent.models.includes(model)))) return false;
    process.env.ACC_PRODUCT_CONFIG_V3 = snapshot;
    delete process.env.ACC_PRODUCT_CONFIG_PATH;
  } finally { fs.closeSync(fd); }
  return true;
}
function startRuntime() {
stage = 'native';
try {
  const functions = ['init_dist', 'normalizeAtRestBuildKeyCapabilities', 'keyblobPathFor',
    'loadOrCreateKeyblobMasterKey', 'encodeAtRestEncryptionBootstrap', 'disposeAtRestEncryptionBootstrap'];
  const profiles = {
    '5.6.2': {module: 'process-cpu-sampler.js', exports: functions},
    '5.7.7': {module: 'process-cpu-sampler.js', exports: ['$', '_t', 'tt', 'nt', 'Z', 'X']},
    '5.8.2': {module: 'app-lifetime-job.js', exports: ['T', 'U', 'D', 'O', 'C', 'S']}
  };
  const profile = profiles[process.env.WORKBUDDY_BRIDGE_APP_VERSION];
  if (!profile) fail();
  const native = require(path.resolve(path.dirname(entry), '../../main', profile.module));
  protection = {};
  for (let index = 0; index < functions.length; index++) {
    if (typeof native[profile.exports[index]] !== 'function') fail();
    protection[functions[index]] = native[profile.exports[index]];
  }
  protection.init_dist();
  const caps = protection.normalizeAtRestBuildKeyCapabilities(process._linkedBinding('electron_browser_workbuddy_storage').loggerGet());
  bootstrap = {policy: 'fields'};
  if (caps.symmetric.status === 'available') bootstrap.symmetricKey = caps.symmetric.value;
  if (caps.symmetric.status !== 'available' || caps.developerPublicKey.status !== 'available') fail();
  bootstrap.developerPublicKey = caps.developerPublicKey.value;
  const master = protection.loadOrCreateKeyblobMasterKey({
    keyblobPath: protection.keyblobPathFor(process.env.CODEBUDDY_CONFIG_DIR),
    protectorKey: caps.symmetric.value, createIfMissing: false
  });
  if (master.status !== 'available') fail();
  bootstrap.userKey = master.value;
  encoded = protection.encodeAtRestEncryptionBootstrap(bootstrap);
} catch { fail(); }
stage = 'handshake';
const token = crypto.randomBytes(32).toString('hex'), worker = 'codex-intelmac-' + task;
const readyPath = path.join(directory, 'r.sock'), credentialPath = path.join(directory, 'c.sock');
setInterval(() => {
  try { if (process.ppid !== Number(parent)) throw new Error(); process.kill(Number(parent), 0); }
  catch { dispose(); process.kill(-process.pid, 'SIGTERM'); }
}, 500).unref();
process.once('exit', dispose);
readyServer = net.createServer(socket => {
  let buffer = '';
  socket.setTimeout(3000, () => socket.destroy());
  socket.on('error', () => {});
  socket.on('data', data => {
    buffer += data.toString('utf8');
    if (buffer.length > 8192) return socket.destroy();
    const index = buffer.indexOf('\n');
    if (index < 0) return;
    try {
      const frame = JSON.parse(buffer.slice(0, index)), p = frame.params || {};
      if (frame.jsonrpc !== '2.0' || !['number','string'].includes(typeof frame.id)
          || frame.method !== 'session.ready' || p.protocolVersion !== 1 || p.sessionId !== worker || p.token !== token
          || p.pid !== process.pid || !Number.isInteger(p.port) || p.port < 1 || p.port > 65535) return socket.destroy();
      socket.end(JSON.stringify({jsonrpc: '2.0', id: frame.id, result: {ok: true}}) + '\n');
      runtimeReported = true; announce();
      readyServer.close();
    } catch { socket.destroy(); }
  });
});
readyServer.on('error', fail);
readyServer.listen(readyPath, () => {
  fs.chmodSync(readyPath, 0o600);
  Object.assign(process.env, {
    CODEBUDDY_SIDECAR_READY_SESSION_ID: worker,
    CODEBUDDY_SIDECAR_READY_TOKEN: token,
    CODEBUDDY_SIDECAR_READY_SOCKET: readyPath,
    CODEBUDDY_SIDECAR_CREDENTIAL_BOOTSTRAP_SOCKET: credentialPath,
    WORKBUDDY_AT_REST_ENCRYPTION: 'fields'
  });
  const deadline = Date.now() + 20000;
  function deliver() {
    const socket = net.createConnection(credentialPath);
    let buffer = '', settled = false, connected = false;
    function retry(error) {
      if (settled) return;
      settled = true; socket.destroy();
      if (connected || !error || !['ENOENT','ECONNREFUSED'].includes(error.code) || Date.now() >= deadline) return fail(connected ? 'credential_response' : 'credential_connect');
      setTimeout(deliver, 100);
    }
    // After a GUI restart the application can be busy loading its modules.
    // Do not abandon an already connected protected handshake after only 2s.
    // Native receiver validation and the overall connect deadline stay intact.
    socket.setTimeout(10000, () => retry());
    socket.once('connect', () => {
      connected = true;
      try { const info = fs.lstatSync(credentialPath);
        if (!info.isSocket() || info.uid !== process.getuid()) return fail();
      } catch { return fail(); }
      socket.write(JSON.stringify({version: 1, sessionId: worker,
        token, pid: process.pid, bootstrap: encoded}) + '\n');
    });
    socket.on('error', retry);
    socket.on('close', () => { if (!settled) retry(); });
    socket.on('data', data => {
      buffer += data.toString('utf8');
      if (buffer.length > 8192) return fail();
      const index = buffer.indexOf('\n');
      if (index < 0) return;
      try {
        const ack = JSON.parse(buffer.slice(0, index));
        if (ack.ok !== true || ack.policy !== 'fields') return fail('credential_ack');
        settled = true; socket.destroy(); dispose(); credentialAccepted = true; announce();
      } catch { fail(); }
    });
  }
  setTimeout(deliver, 0);
  process.argv = [process.execPath, entry, ...args];
  stage = 'cli';
  try { require(entry); } catch { fail('cli_load'); }
});
}
// The GUI first publishes a base catalog while its account overlay loads.
// Wait before native bootstrap/CLI initialization, never change the requested
// model or replay a prompt. Only this application process reads the snapshot.
const productDeadline = Date.now() + 30000;
function waitProduct() {
  try {
    if (loadProduct()) return startRuntime();
    if (Date.now() >= productDeadline) return fail();
    setTimeout(waitProduct, 250);
  } catch { fail(); }
}
waitProduct();

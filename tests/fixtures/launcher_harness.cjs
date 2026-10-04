'use strict';
// Original test doubles for the public bridge wrapper; no application code.
const fs = require('fs'), net = require('net'), vm = require('vm');
const [source, directory, mode] = process.argv.slice(2);
const realProcess = process, entry = '/synthetic/app.asar/cli/bin/codebuddy';
const product = require('path').join(directory, 'synthetic-product.json');
fs.writeFileSync(product, mode === 'malformed_product' ? '{bad json' : JSON.stringify({authentication:
  {id: mode === 'wrong_product_auth' ? 'external-api' : 'workbuddy-desktop', type: 'cli-external-link'}}), {mode: 0o600});
const task = '11111111-2222-4333-8444-555555555555';
const secret = 'SYNTHETIC-PROTECTION-MATERIAL';
const audit = {cli_started: false, create_if_missing: null, getter_called: false,
  disposed: 0, payload_valid: false, ready_ack: false, ready_rejected: false, marker: false,
  reaper_safe_env: false, key_erased: false, inline_product: false};
const symmetric = {key: Buffer.from(secret)}, user = Buffer.from(secret);
realProcess.on('exit', () => { audit.key_erased = symmetric.key.every(byte => byte === 0);
  console.log('HARNESS:' + JSON.stringify(audit)); });
const timer = setTimeout(() => realProcess.exit(9), 4000);
const mockProcess = {argv: [process.execPath, source, task, String(process.ppid), entry, '--serve'],
  env: {WORKBUDDY_BRIDGE_BOOTSTRAP_DIR: directory, CODEBUDDY_CONFIG_DIR: '/synthetic/config', ACC_PRODUCT_CONFIG_PATH: product},
  pid: process.pid, ppid: process.ppid, execPath: process.execPath,
  getuid: process.getuid.bind(process), kill: process.kill.bind(process),
  stdout: {write(value) { if (value === 'INTELMAC_PROTECTED_RUNTIME_READY\n') audit.marker = true; realProcess.stdout.write(value); }},
  stderr: realProcess.stderr,
  exit: process.exit.bind(process), once: process.once.bind(process),
  _linkedBinding(name) {
    if (name !== 'electron_browser_workbuddy_storage') throw new Error('unexpected binding');
    return {loggerGet() { audit.getter_called = true; return 'native-capability-double'; }};
  }};
const protection = {
  init_dist() {},
  normalizeAtRestBuildKeyCapabilities(raw) {
    if (raw !== 'native-capability-double') throw new Error('unexpected native getter');
    return {symmetric: {status: 'available', value: symmetric},
      developerPublicKey: {status: mode === 'missing_public_key' ? 'missing' : 'available', value: secret}};
  },
  keyblobPathFor(config) { if (config !== '/synthetic/config') throw new Error('unexpected config'); return '/synthetic/keyblob'; },
  loadOrCreateKeyblobMasterKey(options) {
    audit.create_if_missing = options.createIfMissing;
    if (options.createIfMissing !== false || options.protectorKey !== symmetric) throw new Error('unsafe key request');
    return {status: mode === 'missing_keyblob' ? 'missing' : 'available', value: user};
  },
  encodeAtRestEncryptionBootstrap(value) {
    if (value.policy !== 'fields' || value.userKey !== user) throw new Error('unsafe bootstrap');
    return secret;
  },
  disposeAtRestEncryptionBootstrap() { audit.disposed += 1;
    if (mode === 'dispose_throws') throw new Error(secret); }
};
function finish() { clearTimeout(timer); realProcess.exit(0); }
function cli() {
  audit.cli_started = true;
  const env = mockProcess.env;
  audit.inline_product = JSON.parse(env.ACC_PRODUCT_CONFIG_V3).authentication.id === 'workbuddy-desktop'
    && !('ACC_PRODUCT_CONFIG_PATH' in env);
  if (env.WORKBUDDY_AT_REST_ENCRYPTION !== 'fields') throw new Error('unsafe policy');
  const receiver = net.createServer(socket => {
    let buffer = '';
    socket.on('error', () => {});
    socket.on('data', data => {
      buffer += data.toString('utf8');
      if (!buffer.includes('\n')) return;
      const frame = JSON.parse(buffer.split('\n')[0]);
      audit.payload_valid = frame.version === 1 && frame.sessionId === env.CODEBUDDY_SIDECAR_READY_SESSION_ID
        && frame.token === env.CODEBUDDY_SIDECAR_READY_TOKEN && frame.pid === process.pid && frame.bootstrap === secret;
      const ack = {ok: true, policy: mode === 'wrong_ack' ? 'disabled' : 'fields'};
      const encoded = JSON.stringify(ack) + '\n';
      socket.write(encoded.slice(0, 3));
      setTimeout(() => socket.end(encoded.slice(3)), 5);
      receiver.close();
    });
  });
  receiver.listen(env.CODEBUDDY_SIDECAR_CREDENTIAL_BOOTSTRAP_SOCKET, () => {
    fs.chmodSync(env.CODEBUDDY_SIDECAR_CREDENTIAL_BOOTSTRAP_SOCKET, 0o600);
    const socket = net.createConnection(env.CODEBUDDY_SIDECAR_READY_SOCKET);
    let buffer = '';
    socket.on('error', () => {});
    socket.once('connect', () => socket.write(JSON.stringify({jsonrpc: '2.0', id: 17,
      method: 'session.ready', params: {protocolVersion: 1,
        sessionId: env.CODEBUDDY_SIDECAR_READY_SESSION_ID,
        token: mode === 'wrong_token' ? 'wrong-token' : env.CODEBUDDY_SIDECAR_READY_TOKEN,
        pid: mode === 'wrong_pid' ? process.pid + 1 : process.pid, port: 3456}}) + '\n'));
    socket.on('data', data => { buffer += data.toString('utf8'); });
    socket.on('close', () => {
      if (buffer) { const reply = JSON.parse(buffer.split('\n')[0]); audit.ready_ack = reply.id === 17 && reply.result.ok === true; }
      else audit.ready_rejected = true;
      if (mode !== 'wrong_ack') setTimeout(finish, 100);
    });
  });
}
const context = {process: mockProcess, Buffer, Date, setTimeout, setInterval, __dirname: require('path').dirname(source),
  console: {log(value) { if (value === 'INTELMAC_PROTECTED_RUNTIME_READY') audit.marker = true; console.log(value); },
    error: console.error.bind(console)},
  require(name) {
    if (name === 'child_process') return {spawn(command, args, options) {
      audit.reaper_safe_env = command === realProcess.execPath && args[0].endsWith('worker_reaper.cjs')
        && Object.keys(options.env).length === 1 && options.env.ELECTRON_RUN_AS_NODE === '1';
      return new (require('events').EventEmitter)();
    }};
    if (name.endsWith('/main/process-cpu-sampler.js')) return protection;
    if (name === entry) return cli();
    if (['fs', 'path', 'net', 'crypto'].includes(name)) return require(name);
    throw new Error('unexpected module');
  }};
vm.runInNewContext(fs.readFileSync(source, 'utf8'), context, {filename: source});

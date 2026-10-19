'use strict';
// Original test doubles for the public bridge wrapper; no application code.
const fs = require('fs'), net = require('net'), vm = require('vm');
const [source, directory, mode] = process.argv.slice(2);
const realProcess = process, entry = '/synthetic/app.asar/cli/bin/codebuddy';
const product = require('path').join(directory, 'synthetic-product.json');
const is582 = mode.startsWith('v582_'), is583 = mode.startsWith('v583_');
const isAccountScoped = is582 || is583;
function writeProduct(ready) {
  fs.writeFileSync(product, mode === 'malformed_product' ? '{bad json' : JSON.stringify({authentication:
    {id: mode === 'wrong_product_auth' ? 'external-api' : 'workbuddy-desktop',
      type: isAccountScoped && mode !== 'v582_wrong_auth_type' && mode !== 'v583_wrong_auth_type'
        ? 'account-scoped-external-link' : 'cli-external-link'},
    models: ready ? [{id:'synthetic-model'}] : [], agents: [{name:'cli', models:ready ? ['synthetic-model'] : []}]}), {mode: 0o600});
}
writeProduct(!['new_delayed_model','new_missing_model','v582_delayed_model','v582_missing_model',
  'v583_delayed_model','v583_missing_model'].includes(mode));
if (['new_delayed_model','v582_delayed_model','v583_delayed_model'].includes(mode)) setTimeout(() => writeProduct(true), 100);
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
  env: {WORKBUDDY_BRIDGE_BOOTSTRAP_DIR: directory, CODEBUDDY_CONFIG_DIR: '/synthetic/config', ACC_PRODUCT_CONFIG_PATH: product,
    WORKBUDDY_BRIDGE_APP_VERSION: is583 ? '5.8.3' : is582 ? '5.8.2' : mode.startsWith('new_') ? '5.7.7'
      : mode === 'v584_unknown_version' ? '5.8.4' : mode === 'unknown_version' ? '99.0.0' : '5.6.2'},
  pid: process.pid, ppid: process.ppid, execPath: process.execPath,
  getuid: process.getuid.bind(process), kill: process.kill.bind(process),
  stdout: {write(value) { if (value === 'INTELMAC_PROTECTED_RUNTIME_READY\n') audit.marker = true; realProcess.stdout.write(value); }},
  stderr: realProcess.stderr,
  exit: process.exit.bind(process), once: process.once.bind(process),
  _linkedBinding(name) {
    if (name !== 'electron_browser_workbuddy_storage') throw new Error('unexpected binding');
    return {loggerGet() { audit.getter_called = true; return 'native-capability-double'; }};
  }};
if (['new_delayed_model','new_missing_model','v582_delayed_model','v582_missing_model',
  'v583_delayed_model','v583_missing_model'].includes(mode)) mockProcess.env.WORKBUDDY_BRIDGE_MODEL = 'synthetic-model';
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
      setTimeout(() => socket.end(encoded.slice(3)),
        ['new_delayed_ack','v582_delayed_ack','v583_delayed_ack'].includes(mode) ? 2200 : 5);
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
      if (mode !== 'wrong_ack') setTimeout(finish,
        ['new_delayed_ack','v582_delayed_ack','v583_delayed_ack'].includes(mode) ? 2400 : 100);
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
    if (name.endsWith('/main/process-cpu-sampler.js') || name.endsWith('/main/app-lifetime-job.js')) {
      const v582 = mode.startsWith('v582_'), v583 = mode.startsWith('v583_');
      const relocated = v582 || v583;
      if (relocated !== name.endsWith('/main/app-lifetime-job.js')) throw new Error('wrong version module');
      if (mode === 'v582_missing_module' || mode === 'v583_missing_module') throw new Error('missing version module');
      if (!mode.startsWith('new_') && !relocated) return protection;
      const keys = ['init_dist','normalizeAtRestBuildKeyCapabilities','keyblobPathFor',
        'loadOrCreateKeyblobMasterKey','encodeAtRestEncryptionBootstrap','disposeAtRestEncryptionBootstrap'];
      const aliases = v583 ? ['D', 'G', 'k', 'A', 'T', 'w']
        : v582 ? ['T', 'U', 'D', 'O', 'C', 'S'] : ['$', '_t', 'tt', 'nt', 'Z', 'X'];
      const values = Object.fromEntries(keys.map((key,index) => [aliases[index], protection[key]]));
      if (mode === 'new_missing_export') delete values.Z;
      if (mode === 'v582_missing_export') delete values.C;
      if (mode === 'v583_missing_export') delete values.G;
      const nonFunctionExport = mode.match(/^v583_non_function_export_(\d)$/);
      if (nonFunctionExport) values[aliases[Number(nonFunctionExport[1])]] = {};
      return values;
    }
    if (name === entry) return cli();
    if (['fs', 'path', 'net', 'crypto'].includes(name)) return require(name);
    throw new Error('unexpected module');
  }};
if (['new_missing_model','v582_missing_model','v583_missing_model'].includes(mode)) {
  let now = 0;
  context.Date = {now() { now += 10000; return now; }};
}
vm.runInNewContext(fs.readFileSync(source, 'utf8'), context, {filename: source});

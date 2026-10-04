'use strict';
// This helper holds the isolated process group until TERM-resistant descendants
// are gone. It has no credential environment or model capabilities.
const [, , task, leader, owner] = process.argv;
const group = Number(leader), parent = Number(owner);
if (!/^[0-9a-f-]{36}$/.test(task) || !Number.isInteger(group) || group <= 1
    || !Number.isInteger(parent) || parent <= 1 || process.ppid !== group) process.exit(2);
const {execFileSync} = require('child_process');
function ownGroup() {
  try { return Number(execFileSync('/bin/ps', ['-p', String(process.pid), '-o', 'pgid='], {encoding:'utf8'}).trim()) === group; }
  catch { return false; }
}
if (!ownGroup()) process.exit(2);
let terminating = false;
function terminate() {
  if (terminating) return;
  terminating = true;
  if (!ownGroup()) process.exit(2);
  try { process.kill(-group, 'SIGTERM'); } catch { process.exit(0); }
  setTimeout(() => { if (ownGroup()) { try { process.kill(-group, 'SIGKILL'); } catch {} } process.exit(0); }, 1000);
}
process.on('SIGTERM', terminate);
setInterval(() => {
  try { if (process.ppid !== group) throw new Error(); process.kill(parent, 0); }
  catch { terminate(); }
}, 250);

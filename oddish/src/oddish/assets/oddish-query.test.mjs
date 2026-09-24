import { test } from 'node:test';
import assert from 'node:assert';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

function run(args, fetchImpl) {
  const res = spawnSync('node', ['./oddish-query', ...args], {
    cwd: new URL('.', import.meta.url).pathname,
    env: { ...process.env, ODDISH_API_BASE_URL: 'http://x', ODDISH_API_KEY: 'k',
           ODDISH_QUERY_TEST_FIXTURE: JSON.stringify(fetchImpl) },
    encoding: 'utf8',
  });
  return res.stdout.trim();
}

function runApi(args, fixture, extraEnv) {
  const res = spawnSync('node', ['./oddish-query', ...args], {
    cwd: new URL('.', import.meta.url).pathname,
    env: { ...process.env, ODDISH_API_BASE_URL: 'http://x', ODDISH_API_KEY: 'k',
           ODDISH_PROBE_TASK_ID: 'task-123',
           ODDISH_QUERY_TEST_FIXTURE: JSON.stringify(fixture), ...(extraEnv || {}) },
    encoding: 'utf8',
  });
  return res.stdout.trim();
}

test('401 → credential expired', () => {
  const out = run(['trials', 'result', 'x'], { __status: 401 });
  assert.deepEqual(JSON.parse(out), { error: 'session credential expired', status: 401 });
});

test('task cat without task id dies clearly', () => {
  const out = runApi(['task', 'cat', 'a.txt'], {}, { ODDISH_PROBE_TASK_ID: '' });
  assert.match(out, /task id/i);
});

test('task cat fetches a task source file without adding a solution prefix', () => {
  const out = runApi(['task', 'cat', 'instruction.md'],
    { '/tasks/task-123/files/instruction.md': { path: 'instruction.md', content: 'TASK-INSTRUCTIONS' } });
  assert.match(out, /TASK-INSTRUCTIONS/);
});

test('task file reads use the pinned task version', () => {
  const out = runApi(
    ['task', 'cat', 'instruction.md'],
    {
      '/tasks/task-123/files/instruction.md?version=7': {
        path: 'instruction.md',
        content: 'VERSION-SEVEN',
      },
    },
    { ODDISH_PROBE_TASK_VERSION: '7' },
  );
  assert.match(out, /VERSION-SEVEN/);
});

test('task fetch downloads the complete source tree into --into', () => {
  const dest = fs.mkdtempSync(path.join(os.tmpdir(), 'task-dest-'));
  const out = runApi(['task', 'fetch', '--into', dest],
    { '/tasks/task-123/files': { files: [
        { path: 'instruction.md', content: 'TASK-INSTRUCTIONS' },
        { path: 'tests/test.sh', content: 'VERIFIER' } ] } });
  assert.equal(fs.readFileSync(path.join(dest, 'instruction.md'), 'utf8'), 'TASK-INSTRUCTIONS');
  assert.equal(fs.readFileSync(path.join(dest, 'tests/test.sh'), 'utf8'), 'VERIFIER');
});

test('task fetch preserves presigned binary bytes and their sha256', () => {
  const dest = fs.mkdtempSync(path.join(os.tmpdir(), 'task-binary-dest-'));
  const original = Buffer.from([0x00, 0x7f, 0x80, 0xff, 0x41, 0xc3, 0x28]);
  const url = `data:application/octet-stream;base64,${original.toString('base64')}`;
  const out = runApi(['task', 'fetch', '--into', dest],
    { '/tasks/task-123/files': { files: [
        { path: 'solution/engine_core.bin', size: original.length, url } ] } });

  const downloaded = fs.readFileSync(path.join(dest, 'solution/engine_core.bin'));
  const sha256 = (value) => createHash('sha256').update(value).digest('hex');
  assert.deepEqual(downloaded, original);
  assert.equal(sha256(downloaded), sha256(original));
});

test('task fetch rejects a downloaded file whose size changed in transit', () => {
  const dest = fs.mkdtempSync(path.join(os.tmpdir(), 'task-corrupt-dest-'));
  const original = Buffer.from([0x80, 0xff]);
  const url = `data:application/octet-stream;base64,${original.toString('base64')}`;
  const out = runApi(['task', 'fetch', '--into', dest],
    { '/tasks/task-123/files': { files: [
        { path: 'solution/engine_core.bin', size: original.length + 1, url } ] } });

  assert.deepEqual(JSON.parse(out), {
    error: 'task file size mismatch: solution/engine_core.bin (expected 3, got 2)',
    status: 5,
  });
  assert.ok(!fs.existsSync(path.join(dest, 'solution/engine_core.bin')));
});

test('--help lists the command groups', () => {
  const out = runApi(['--help'], {});
  assert.match(out, /task cat/);
  assert.match(out, /task fetch/);
  assert.doesNotMatch(out, /solution|verify run|harbor src|tasks |experiments|PROBE-ONLY/);
  assert.match(out, /trials result/);
  assert.match(out, /trials trajectory/);
  assert.match(out, /trials verifier/);
  assert.match(out, /trials logs/);
});

test('trial result prints the complete endpoint JSON without MAX_BYTES slicing', () => {
  const payload = { trial_results: [{ trial_name: 't-1', output: 'r'.repeat(24000) }] };
  const out = runApi(['trials', 'result', 't-1'], { '/trials/t-1/result': payload });
  assert.deepEqual(JSON.parse(out), { trial_id: 't-1', result: payload });
  assert.ok(out.length > 16000);
});

test('trial trajectory prints the complete endpoint JSON without tail slicing', () => {
  const payload = { trial_name: 't-1', steps: [{ message: 's'.repeat(24000) }] };
  const out = runApi(
    ['trials', 'trajectory', 't-1'],
    { '/trials/t-1/trajectory': payload },
  );
  assert.deepEqual(JSON.parse(out), { trial_id: 't-1', trajectory: payload });
  assert.ok(out.length > 16000);
});

test('trial verifier returns complete verifier streams with trial identity', () => {
  const payload = {
    verifier: { stdout: 'PASS\n', stderr: 'warning\n' },
    exception: null,
    agent: { setup: 'ignored by this bounded view' },
  };
  const out = runApi(
    ['trials', 'verifier', 't-1'],
    { '/trials/t-1/logs/structured?verifier_only=true': payload },
  );
  assert.deepEqual(JSON.parse(out), {
    trial_id: 't-1',
    verifier: payload.verifier,
    exception: null,
  });
});

test('trial logs retain log-specific truncation', () => {
  const payload = { logs: 'l'.repeat(24000) };
  const out = runApi(['trials', 'logs', 't-1'], { '/trials/t-1/logs': payload });
  assert.match(out, /truncated/);
  assert.ok(out.length < 16000);
});

test('the removed logs --trajectory option fails instead of changing resource meaning', () => {
  const out = runApi(['trials', 'logs', 't-1', '--trajectory'], {});
  assert.deepEqual(JSON.parse(out), { error: 'unknown option: --trajectory', status: 2 });
});

test('per-command help explains usage', () => {
  const out = runApi(['task', '--help'], {});
  assert.match(out, /task (cat|fetch)/);
});

test('no stage-dir env is referenced', () => {
  const src = fs.readFileSync(new URL('./oddish-query', import.meta.url).pathname, 'utf8');
  assert.doesNotMatch(src, /ODDISH_PROBE_STAGE_DIR/);
  assert.doesNotMatch(src, /requireStage|stagePath/);
});

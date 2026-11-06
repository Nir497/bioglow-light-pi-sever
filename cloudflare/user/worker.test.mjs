import test from 'node:test';
import assert from 'node:assert/strict';
import worker from './worker.mjs';
test('root is served from packaged User assets', async () => {
  let path;
  const response = await worker.fetch(new Request('https://glow-user.example/'), {ASSETS: {fetch(r) {path = new URL(r.url).pathname; return new Response('user');}}});
  assert.equal(path, '/user.html');
  assert.equal(await response.text(), 'user');
});
test('Developer and destructive API routes are not forwarded', async () => {
  for (const [method,path] of [['POST','/v1/configuration/restore'],['DELETE','/v1/recordings/abc'],['GET','/v1/configuration']]) {
    assert.equal((await worker.fetch(new Request('https://example.com'+path,{method}),{})).status,404);
  }
});
test('API forwarding keeps queries and disables caching', async () => {
  const previous=globalThis.fetch;
  globalThis.fetch=async (url,options)=>{assert.equal(String(url),'http://pi.example/v1/history?source=birdcast');assert.equal(options.redirect,'manual');return new Response('{}');};
  try {
    const response=await worker.fetch(new Request('https://user.example/v1/history?source=birdcast'),{PI_ORIGIN:'http://pi.example'});
    assert.equal(response.headers.get('Cache-Control'),'no-store');
  } finally {globalThis.fetch=previous;}
});

const allowed = (method, path) =>
  (method === 'GET' && (/^\/v1\/(latest|history|recordings)$/.test(path) || /^\/v1\/recordings\/[a-f0-9]{32}(\/analysis)?$/.test(path))) ||
  (method === 'POST' && ['/v1/user-aim', '/v1/recordings', '/v1/snapshots'].includes(path));
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname.startsWith('/v1/')) {
      if (!allowed(request.method, url.pathname)) return new Response('Not found', {status: 404});
      const target = new URL(env.PI_ORIGIN);
      target.pathname = url.pathname;
      target.search = url.search;
      const headers = new Headers();
      for (const name of ['content-type', 'range', 'if-range']) {
        if (request.headers.has(name)) headers.set(name, request.headers.get(name));
      }
      try {
        const upstream = await fetch(target, {
          method: request.method, headers,
          body: request.method === 'POST' ? request.body : undefined,
          redirect: 'manual', signal: AbortSignal.timeout(60000)
        });
        const response = new Response(upstream.body, upstream);
        response.headers.set('Cache-Control', 'no-store');
        return response;
      } catch {
        return Response.json({error: {message: 'The Pi is temporarily unavailable.'}}, {status: 502});
      }
    }
    if (!['GET', 'HEAD'].includes(request.method)) return new Response('Method not allowed', {status: 405});
    if (url.pathname === '/' || url.pathname === '/user') url.pathname = '/user.html';
    return env.ASSETS.fetch(new Request(url, request));
  }
};

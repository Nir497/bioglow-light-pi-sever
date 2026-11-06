# glow-user Worker

The Worker hosts User assets and proxies the User API to the Pi tunnel. Hardware, recordings, analysis and the Developer dashboard remain on the Pi. The browser uses same-origin `/v1` requests, so CORS changes are unnecessary. API responses are never cached; audio responses stream through the Worker.

Deploy from this repository on a machine authenticated to your Cloudflare account:

```bash
python3 cloudflare/user/prepare.py
npx wrangler@4 login
npx wrangler@4 deploy --config cloudflare/user/wrangler.jsonc
```

`glow-user` is the Worker name. Cloudflare returns its full `https://glow-user.<account-subdomain>.workers.dev` address on deployment. A custom domain requires a full hostname, not just `glow-user`.

`PI_ORIGIN` is the existing published Pi tunnel hostname, not localhost. It retains HTTP because HTTPS for that origin has not been verified. Do not point the Pi tunnel at the Worker: this would create a proxy loop. No tunnel token is needed in the Worker.

Verify the Worker root, `/v1/latest`, recording playback/capture, migration refresh and aiming. Developer configuration and deletion API routes are not forwarded by this Worker. The existing Pi dashboard remains available during migration; remove its User assets only after the Worker is deployed and verified.

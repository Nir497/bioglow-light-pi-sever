# Cloudflare dashboard tunnel

For outbound readings and lamp/animal telemetry to the separate `bioglow-data`
Worker, see [the data receiver setup](cloudflare/data/README.md). It uses HTTPS
uploads and does not require changing this tunnel.

The existing dashboard stays on port 8000. A remotely managed Cloudflare Tunnel
forwards the public hostname to `http://127.0.0.1:8000` on the Pi. No application
code changes or router port forwarding are needed. Local dashboard access stays
available.

The selected configuration is public, with no Cloudflare Access login. Anyone
who can reach the hostname can use the dashboard, including microphone capture,
audio downloads, recording deletion, and lighting controls.

## Pi preparation

On this Pi, Cloudflare's official signed apt repository is configured in
`/etc/apt/sources.list.d/cloudflared.list`, using
`/usr/share/keyrings/cloudflare-main.gpg`. The ARM64 package is installed.

`/etc/systemd/system/cloudflared.service` is enabled at boot and runs:

```bash
/usr/bin/cloudflared --no-autoupdate tunnel run --token-file /etc/cloudflared/tunnel-token
```

The root-owned token directory has mode 0700; the token file must have mode 0600.
The service skips startup until that file exists, restarts after failures, and
logs to the system journal. Package updates use apt rather than self-updates.

The tunnel service forwards traffic to port 8000. To have the Python dashboard
server start automatically on boot alongside Cloudflare Tunnel, run:

```bash
sudo ./scripts/install_service.sh
```

Run only one dashboard server. Automatic microphone scheduling starts stopped
after a dashboard restart, as before.

## Cloudflare UI and token handoff

1. In Cloudflare, open **Networking → Tunnels → Create Tunnel** and name the
   tunnel `bioglow-pi`. Choose the cloudflared connector if prompted.
2. Select **Debian / ARM64**. The package and service are already prepared on the
   Pi, so do not run the displayed `service install` command over the existing
   service. Copy only the token following `service install`.
3. On the Pi, save that token without putting it in shell history:

   ```bash
   install -m 0600 /dev/null /home/nir/bioglow-birdcast/.runtime/cloudflared-token
   read -r -s -p 'Paste tunnel token: ' bioglow_tunnel_token
   printf '\n'
   printf '%s' "$bioglow_tunnel_token" > /home/nir/bioglow-birdcast/.runtime/cloudflared-token
   unset bioglow_tunnel_token
   ```

   This staging file is ignored by Git. Tell the configuring agent **token ready**
   and provide the desired public hostname. The agent installs the token as
   `/etc/cloudflared/tunnel-token` with mode 0600, clears the staging file, starts
   the service, and verifies connection. Never commit or paste the token into
   documentation.
4. When Cloudflare shows the connector as **Healthy**, select the tunnel and open
   **Routes → Add route → Published application**. Some dashboard versions call
   this **Published application routes** or **Public Hostnames**.
5. Choose subdomain `bioglow`, select your Cloudflare domain, and leave the path
   blank. Set the service URL to `http://127.0.0.1:8000`. If the form separates
   type and URL, choose **HTTP** and enter `127.0.0.1:8000`. Save the route.
6. Open `https://bioglow.<your-domain>`. Do not add an Access application for the
   selected public configuration.

See Cloudflare's [tunnel setup guide](https://developers.cloudflare.com/tunnel/get-started/)
and [official package repository](https://pkg.cloudflare.com/).

## Verification and operations

```bash
systemctl is-enabled cloudflared.service
systemctl is-active cloudflared.service
sudo journalctl -u cloudflared.service -n 50 --no-pager
curl --noproxy '*' --max-time 3 -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/
sudo systemctl restart cloudflared.service
```

The service is expected to be inactive before the token is installed. Once
connected, verify HTTPS dashboard loading, live readings, history, and playback
of an existing recording, along with continued LAN access. Restarting the
connector should recover public access without restarting the dashboard.

A Cloudflare 502 usually means the connector cannot reach the dashboard; check
port 8000 and the route's HTTP URL. For a disconnected tunnel, check the service
journal, token, Pi Internet connection, and outbound TCP/UDP port 7844. If stale
assets or readings appear, check for existing Cloudflare cache rules covering the
hostname and exclude this dashboard from those rules.

To stop this Pi's connector, use `sudo systemctl disable --now cloudflared.service`.
This leaves the local dashboard running. Remove the published route in Cloudflare
to remove its hostname routing. Rotate the tunnel token in Cloudflare if exposed,
replace the protected token file, and restart the connector.

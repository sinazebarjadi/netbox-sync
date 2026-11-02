# netbox-mcp

Read-only MCP server exposing NetBox inventory to Brain Bot (NanoCortex).

## Tools

| Tool | Description |
|------|-------------|
| `netbox_get_device` | Full device record by name / serial / management IP (name, role, vendor, model, serial, asset tag, site, rack, U position, mgmt IP, hardware specs, power draw, status, custom fields) |
| `netbox_search_devices` | Substring search across name + serial |
| `netbox_rack_inventory` | All devices in a rack with U positions + power feed + amps |
| `netbox_interface_info` | Interfaces of a device + assigned IPs |
| `netbox_lookup_ip` | Who owns an IP: device, interface, prefix, DNS |
| `netbox_device_power` | Live PSU draw + rack feed electrical spec |

## On the NetBox box (`<your-netbox-host>`)

- Service: `systemctl status netbox-mcp` (systemd, auto-restart)
- Endpoint (via nginx on 443): `https://<your-netbox-host>/mcp/sse`
- Backend (localhost only): `127.0.0.1:8901`
- Token: `NETBOX_TOKEN` in `/home/sina/netbox-mcp/.env` (0600), read-only (`write_enabled: False`)
- venv: `/home/sina/netbox-mcp/.venv` (fully offline-installed)

## NanoCortex registration

Register a **remote** MCP in Brain Bot pointing to:

```
https://<your-netbox-host>/mcp/sse
```

- Name: `netbox`
- Grant it to Brain Bot's agent (or make public for your team).

## Verify

```bash
python e2e_test.py   # full protocol test from anywhere that reaches the URL
```

## Security model

- NetBox token is read-only at API + object-permission level
- This server only issues HTTP GET (see `netbox_api.py` — GET-only client)
- Token never appears in tool output, logs, or replies (audit the code: no `NETBOX_TOKEN` outside env read)
- nginx front is the same TLS as NetBox itself

"""Tell NanoCortex to pull the remote NetBox MCP server into Brain Bot.

Server-side state (verified):
 - systemd unit installed? verified below
 - SSE up at <netbox-host>:8901? verified below
After this file is delivered to the operator, the remaining NanoCortex steps
(grant card + ask) are done by hand in the Brain Bot UI.
"""
REGISTER_PAYLOAD = {
    "name": "netbox",
    "title": "NetBox Inventory (read-only)",
    "serverType": "remote",             # remote SSE endpoint, not stdio
    "endpoint": "https://<netbox-host>/mcp/sse",
    "description": ("Read-only NetBox inventory via MCP: devices, racks, "
                    "interfaces, IPs, power. Auth handled server-side; the "
                    "NetBox token never leaves the MCP host."),
    "public": False,                    # private; enable per-agent via grant
}
GRANT_PAYLOAD = {
    "name": "netbox",
    # "agentGroupId": "<Brain Bot agent/group id>",
}
print(__doc__)
print("REGISTER:", REGISTER_PAYLOAD)
print("GRANT:", GRANT_PAYLOAD)

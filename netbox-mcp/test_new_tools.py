import os
os.environ.setdefault('NETBOX_TOKEN','unit-test-token')
os.environ.setdefault('NETBOX_URL','https://unit.test')
import ast, json, sys, types, pathlib
sys.path.insert(0, r'G:\NetBoX\code\github\netbox-mcp')

# FastMCP may be absent locally; stub only its decorator machinery for unit tests.
class _StubMCP:
    def __init__(self, *a, **kw):
        self.tools = {}
    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco
    def run(self, *a, **kw): pass

mcp_mod = types.ModuleType('mcp.server.fastmcp')
mcp_mod.FastMCP = lambda *a, **kw: _StubMCP()
sys.modules['mcp.server.fastmcp'] = mcp_mod

import importlib
import server as srv
import netbox_api as nb


class FakeGet:
    def __init__(self):
        self.calls = []
    def __call__(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        table = {
            '/api/ipam/vlans/': {'results':[{'id':1,'vid':100,'name':'vlan100','status':{'value':'active'},'site':{'name':'HQ'},'tenant':None,'description':'Test VLAN','role':None,'custom_fields':{}}]},
            '/api/ipam/vlans/1/': {'id':1,'vid':100,'name':'vlan100','status':{'value':'active'},'site':{'name':'HQ'},'tenant':None,'description':'Test VLAN','role':None,'custom_fields':{}},
            '/api/ipam/vrfs/': {'results':[{'id':1,'name':'HA','rd':'65000:1','tenant':None,'description':'HA VRF','enforce_unique':True,'custom_fields':{}}]},
            '/api/ipam/prefixes/': {'results':[{'id':413,'prefix':'10.0.0.0/24','status':{'value':'active'},'vrf':{'name':'HA'},'vlan':{'vid':100,'name':'vlan100'},'site':{'name':'HQ'},'role':None,'description':'Test prefix','tenant':None,'custom_fields':{}}]},
            '/api/ipam/prefixes/413/': {'id':413,'prefix':'10.0.0.0/24','status':{'value':'active'},'vrf':{'name':'HA'},'vlan':{'vid':100,'name':'vlan100'},'site':{'name':'HQ'},'role':None,'description':'Test prefix','tenant':None,'custom_fields':{}},
            '/api/dcim/cables/': {'results':[{'id':2958,'type':'copper','status':{'value':'connected'},'a_terminations':[{'object_type':'dcim.interface','object':{'name':'eth0','device':{'name':'HOST-A'}}}],'b_terminations':[{'object_type':'dcim.interface','object':{'name':'swp1','device':{'name':'SW-1'}}}],'description':'Test cable','length':2.0,'length_unit':'m'}]},
            '/api/dcim/interfaces/11/': {'id':11,'name':'eth0','device':{'id':8,'name':'HOST-A'},'type':{'value':'1000base-t'},'enabled':True,'mac_address':'00:11:22:33:44:55','mtu':1500,'mode':{'value':'access'},'description':'Test iface','cable':{'id':2958}},
        }
        if path in table: return table[path]
        if path == '/api/ipam/ip-addresses/': return {'results':[{'address':'10.0.0.10/24','status':{'value':'active'},'dns_name':'host-a.example','description':'x'}]}
        raise AssertionError(f'unexpected path {path}')


def test_get_only_guard():
    src = pathlib.Path(r'G:\NetBoX\code\github\netbox-mcp\netbox_api.py').read_text()
    assert 'requests.post' not in src and 'requests.put' not in src and 'requests.delete' not in src and 'requests.patch' not in src
    for func in srv.netbox_list_vlans, srv.netbox_vlan_detail, srv.netbox_list_vrfs, srv.netbox_list_prefixes, srv.netbox_prefix_detail, srv.netbox_list_cables, srv.netbox_interface_details:
        assert callable(func)


def test_list_vlans_pagination_and_shape():
    fake = FakeGet(); orig = nb.get; nb.get = fake
    try:
        result = srv.netbox_list_vlans(site='HQ', limit=50)
        assert any('/api/ipam/vlans/' == p for p,_ in fake.calls)
        d = json.loads(result)
        assert d['count'] == 1
        assert d['vlans'][0]['vid'] == 100
        assert d['vlans'][0]['name'] == 'vlan100'
    finally: nb.get = orig


def test_vlan_detail():
    fake = FakeGet(); orig = nb.get; nb.get = fake
    try:
        d = json.loads(srv.netbox_vlan_detail(1))
        assert d['found'] and d['vlan']['vid'] == 100
        assert fake.calls[0][0] == '/api/ipam/vlans/1/'
    finally: nb.get = orig


def test_list_vrfs():
    fake = FakeGet(); orig = nb.get; nb.get = fake
    try:
        d = json.loads(srv.netbox_list_vrfs())
        assert d['count'] == 1 and d['vrfs'][0]['rd'] == '65000:1'
    finally: nb.get = orig


def test_list_prefixes_filters():
    fake = FakeGet(); orig = nb.get; nb.get = fake
    try:
        d = json.loads(srv.netbox_list_prefixes(vrf='HA', vlan=100, site='HQ'))
        assert d['count'] == 1 and d['prefixes'][0]['prefix'] == '10.0.0.0/24'
        assert d['prefixes'][0]['vlan']['vid'] == 100
    finally: nb.get = orig


def test_prefix_detail():
    fake = FakeGet(); orig = nb.get; nb.get = fake
    try:
        d = json.loads(srv.netbox_prefix_detail(413))
        assert d['found'] and d['prefix']['prefix'] == '10.0.0.0/24'
    finally: nb.get = orig


def test_list_cables_terminations():
    fake = FakeGet(); orig = nb.get; nb.get = fake
    try:
        d = json.loads(srv.netbox_list_cables(device='HOST-A'))
        assert d['count'] == 1
        assert d['cables'][0]['a_terminations'][0]['device'] == 'HOST-A'
    finally: nb.get = orig


def test_interface_details_includes_ips():
    fake = FakeGet(); orig = nb.get; nb.get = fake
    try:
        d = json.loads(srv.netbox_interface_details(11))
        assert d['found'] and d['interface']['name'] == 'eth0'
        assert d['ip_addresses'][0]['address'] == '10.0.0.10/24'
    finally: nb.get = orig


if __name__ == '__main__':
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            try:
                fn(); print('PASS', name)
            except Exception as e:
                print('FAIL', name, e)
                failures += 1
    raise SystemExit(failures)

#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Andrei-Alexandru Bleortu
"""horaco — a Cisco IOS-like CLI wrapper over Horaco / ZX-SWTG124AS web-managed switches.

These AliExpress 2.5G/10G switches (model ZX-SWTG124AS, Realtek-based, firmware V1.9)
expose only an HTTP UI on port 80 with a clumsy per-VLAN port-membership matrix and a
separate per-port PVID form. This wrapper hides that and gives you an IOS-style CLI:

    show vlan / show vlan brief
    show interfaces status
    show running-config
    show version
    configure terminal
        vlan 50
            name Guest
        no vlan 50
        interface gi1            (also: gi1-3, "interface range gi1,gi5")
            description Uplink   (local-only label; ports 1-4 are 2.5G, 5-6 are 10G)
            switchport mode access
            switchport access vlan 42
            switchport mode trunk
            switchport trunk native vlan 1
            switchport trunk allowed vlan 10,12,777
            switchport trunk allowed vlan add 44
            switchport trunk allowed vlan remove 44
            shutdown / no shutdown
        end
    write memory                 (persists to flash; the web UI calls this "Save")

Auth model (reverse-engineered): the login form sets cookie `admin=md5(user+password)`
and that cookie alone authorizes every subsequent .cgi request.

Inventory + credentials come from a YAML file (switches.sops.yaml, decrypted on the fly
via `sops`, or a plaintext switches.yml). See README.md.

This file is dependency-light: only `requests` (already in the repo venv).
"""

import argparse
import hashlib
import os
import re
import shlex
import subprocess
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------- #
# Config discovery
#
# This tool is meant to live as a standalone repo / git submodule, while the
# switch inventory (and any local data) lives OUTSIDE it — typically in the
# parent directory. So we search, in order: $HORACO_SWITCHES, the current
# working directory, then this script's directory and its parents.
# --------------------------------------------------------------------------- #
INVENTORY_NAMES = ("switches.sops.yaml", "switches.yml", "switches.yaml")


def _search_dirs():
    dirs, seen = [], set()
    for d in [os.getcwd(), HERE, *(_parents(HERE, 4))]:
        if d and d not in seen:
            seen.add(d)
            dirs.append(d)
    return dirs


def _parents(path, n):
    out = []
    for _ in range(n):
        nxt = os.path.dirname(path)
        if nxt == path:
            break
        out.append(nxt)
        path = nxt
    return out


def find_inventory():
    """Locate the inventory file, or return None."""
    env = os.environ.get("HORACO_SWITCHES")
    if env:
        return env if os.path.exists(env) else None
    for d in _search_dirs():
        for name in INVENTORY_NAMES:
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p
    return None


def data_dir():
    """Directory that holds the inventory (where descriptions.yml also lives)."""
    inv = find_inventory()
    return os.path.dirname(os.path.abspath(inv)) if inv else os.getcwd()


def load_inventory():
    """Return dict name -> {host,user,password,ports}. SOPS file or plaintext."""
    import yaml  # local import; only needed here

    path = find_inventory()
    if not path:
        sys.exit(
            "No inventory found. Create switches.sops.yaml or switches.yml next to "
            "the tool or in its parent dir, or set $HORACO_SWITCHES "
            "(see switches.example.yml)."
        )
    if ".sops." in os.path.basename(path):
        raw = subprocess.run(["sops", "--decrypt", path], capture_output=True, text=True)
        if raw.returncode != 0:
            sys.exit(f"sops decrypt failed for {path}:\n{raw.stderr}")
        data = yaml.safe_load(raw.stdout)
    else:
        with open(path) as fh:
            data = yaml.safe_load(fh)
    return data.get("switches", data)


# --------------------------------------------------------------------------- #
# Helpers for the switch's compressed port lists, e.g. "1,3,5-6"
# --------------------------------------------------------------------------- #
def parse_portlist(s):
    """'1,3,5-6' -> {1,3,5,6}. '-' or '' -> set()."""
    s = (s or "").strip()
    if not s or s == "-":
        return set()
    out = set()
    for chunk in s.split(","):
        chunk = chunk.strip()
        if "-" in chunk:
            a, b = chunk.split("-")
            out.update(range(int(a), int(b) + 1))
        elif chunk:
            out.add(int(chunk))
    return out


def fmt_portlist(ports):
    """{1,3,5,6} -> '1,3,5-6'."""
    ports = sorted(ports)
    if not ports:
        return "-"
    runs = []
    start = prev = ports[0]
    for p in ports[1:]:
        if p == prev + 1:
            prev = p
            continue
        runs.append((start, prev))
        start = prev = p
    runs.append((start, prev))
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


ACCEPT_NAMES = {0: "all", 1: "tag-only", 2: "untag-only"}
ACCEPT_CODES = {"all": 0, "tag-only": 1, "untag-only": 2, "tag": 1, "untag": 2}

# port.cgi speed_duplex select codes. The text form (cfg_speed) like '2500M/Full' is
# normalised to one of these keys; 2.5G ports accept 0-6, 10G ports accept 0,4,5,6,8.
SPEED_CODES = {
    "auto": 0, "10half": 1, "10full": 2, "100half": 3, "100full": 4,
    "1000": 5, "1000full": 5, "2500": 6, "2500full": 6, "10g": 8, "10gfull": 8,
}
JUMBO_CODES = {"1522": 0, "1536": 1, "1552": 2, "9216": 3, "16383": 4}
STORM_CODES = {  # storm-control keyword -> switch storm_filter value
    "unknown-unicast": 0, "unknown-multicast": 1, "multicast": 2, "broadcast": 3,
}
MIRROR_DIR = {"rx": 1, "tx": 2, "both": 3}


def desc_file():
    """Sidecar path for port descriptions — lives beside the inventory, not in the tool."""
    return os.path.join(data_dir(), "descriptions.yml")


def port_capability(port):
    """ZX-SWTG124AS layout: ports 1-4 are 2.5G copper, ports 5-6 are 10G SFP+."""
    return "10G" if port >= 5 else "2.5G"


def _norm_speed(s):
    """'2500Full' -> '2500M', '10GFull' -> '10G', '1000Full' -> '1000M'."""
    s = (s or "").strip()
    m = re.match(r"(\d+G?)(Full|Half)?", s)
    if not m:
        return "-"
    rate = m.group(1)
    return rate if rate.endswith("G") else rate + "M"


def load_descriptions(switch_name):
    """Return {port:int -> description:str} for a switch (local sidecar, not on device)."""
    import yaml

    path = desc_file()
    if not os.path.exists(path):
        return {}
    with open(path) as fh:
        data = yaml.safe_load(fh) or {}
    return {int(p): d for p, d in (data.get(switch_name) or {}).items()}


def save_descriptions(switch_name, desc):
    """Persist {port -> description} for a switch back into the shared sidecar file."""
    import yaml

    path = desc_file()
    data = {}
    if os.path.exists(path):
        with open(path) as fh:
            data = yaml.safe_load(fh) or {}
    data[switch_name] = {int(p): d for p, d in sorted(desc.items()) if d}
    with open(path, "w") as fh:
        fh.write(
            "# Local port descriptions for Horaco switches.\n"
            "# The ZX-SWTG124AS firmware has no on-device port-name field, so horaco.py\n"
            "# stores 'description' here. Keyed by switch name, then port number.\n"
        )
        yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=True)


# --------------------------------------------------------------------------- #
# Switch driver
# --------------------------------------------------------------------------- #
class Switch:
    def __init__(self, name, host, user, password, ports=6, dry_run=False, timeout=10):
        self.name = name
        self.host = host
        self.user = user
        self.password = password
        self.nports = int(ports)
        self.dry_run = dry_run
        self.timeout = timeout
        self.token = hashlib.md5(f"{user}{password}".encode()).hexdigest()
        self.s = requests.Session()
        self.s.headers["Cookie"] = f"admin={self.token}"
        self._base = f"http://{host}"
        self._logged_in = False

    # -- auth --------------------------------------------------------------- #
    def login(self):
        """Establish the switch's single server-side admin session.

        The cookie alone is not enough on a cold session: until the login form is
        POSTed, pages render their template but the server leaves the data blank.
        """
        r = self.s.post(
            self._base + "/login.cgi",
            data={
                "username": self.user,
                "password": self.password,
                "Response": self.token,
                "language": "EN",
            },
            timeout=self.timeout,
        )
        r.raise_for_status()
        self._logged_in = True

    # -- low-level ---------------------------------------------------------- #
    def _get(self, path):
        if not self._logged_in:
            self.login()
        r = self.s.get(self._base + path, timeout=self.timeout)
        r.raise_for_status()
        if 'location.replace("/login.cgi")' in r.text or (
            "login.cgi" in r.text and "formSubmit" in r.text
        ):
            # session lost / contended — try once more
            self.login()
            r = self.s.get(self._base + path, timeout=self.timeout)
            r.raise_for_status()
            if 'location.replace("/login.cgi")' in r.text:
                sys.exit(
                    f"[{self.name}] auth rejected (another admin session may be "
                    "active, or credentials are wrong)"
                )
        return r.text

    def _post(self, path, data):
        if self.dry_run:
            print(f"  DRY-RUN POST {path}  {data}")
            return ""
        if not self._logged_in:
            self.login()
        r = self.s.post(self._base + path, data=data, timeout=self.timeout)
        r.raise_for_status()
        return r.text

    # -- read state --------------------------------------------------------- #
    def fetch_vlans(self):
        """vid -> {'name':str, 'tagged':set, 'untagged':set}."""
        html = self._get("/vlan.cgi?page=static")
        vlans = {}
        # rows: VLAN link | name | member | tagged | untagged | delete
        row_re = re.compile(
            r'pickVlanId=(\d+)">\d+</a></td>\s*'
            r"<td>(.*?)</td>\s*"  # name
            r"<td nowrap>(.*?)</td>\s*"  # member
            r"<td nowrap>(.*?)</td>\s*"  # tagged
            r"<td nowrap>(.*?)</td>",  # untagged
            re.S,
        )
        for vid, name, _member, tagged, untagged in row_re.findall(html):
            vlans[int(vid)] = {
                "name": name.strip(),
                "tagged": parse_portlist(tagged),
                "untagged": parse_portlist(untagged),
            }
        return vlans

    def fetch_ports(self):
        """port(1-based) -> {'pvid':int,'accept':code,'state':str,'speed':str,'link':str}."""
        ports = {p: {} for p in range(1, self.nports + 1)}
        # PVID + accept from port_based page (plain-text scrape)
        txt = re.sub(r"<[^>]*>", "", self._get("/vlan.cgi?page=port_based"))
        lines = [l.strip() for l in txt.splitlines() if l.strip()]
        # find sequences: "Port N", pvid, accept-type
        accept_lookup = {"All": 0, "Tag-only": 1, "Untag-only": 2}
        i = 0
        while i < len(lines):
            m = re.fullmatch(r"Port (\d+)", lines[i])
            if m and i + 2 < len(lines) and lines[i + 1].isdigit():
                p = int(m.group(1))
                if p in ports:
                    ports[p]["pvid"] = int(lines[i + 1])
                    ports[p]["accept"] = accept_lookup.get(lines[i + 2], 0)
                i += 3
                continue
            i += 1
        # link/speed/state from stats page
        stxt = re.sub(r"<[^>]*>", "", self._get("/port.cgi?page=stats"))
        slines = [l.strip() for l in stxt.splitlines() if l.strip()]
        i = 0
        while i < len(slines):
            m = re.fullmatch(r"Port (\d+)", slines[i])
            if m and i + 2 < len(slines):
                p = int(m.group(1))
                if p in ports:
                    ports[p]["state"] = slines[i + 1]
                    ports[p]["link"] = slines[i + 2]
                i += 3
                continue
            i += 1
        # negotiated link speed from the port.cgi status table:
        #   Port N | State | Config-speed | Actual-speed | FlowCfg | FlowActual
        cells = [
            c.strip()
            for c in re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "|", self._get("/port.cgi"))).split("|")
            if c.strip()
        ]
        try:
            h = next(k for k in range(len(cells) - 3) if cells[k : k + 4] == ["Config", "Actual", "Config", "Actual"])
        except StopIteration:
            h = None
        if h is not None:
            seg = cells[h + 4 :]
            i = 0
            while i < len(seg):
                m = re.fullmatch(r"Port (\d+)", seg[i])
                if m and i + 3 < len(seg):
                    p = int(m.group(1))
                    if p in ports:
                        ports[p]["speed"] = _norm_speed(seg[i + 3])
                    i += 6
                    continue
                i += 1
        return ports

    # -- write state -------------------------------------------------------- #
    def write_vlan(self, vid, name, untagged, tagged):
        """Create/replace a full 802.1Q VLAN entry. untagged/tagged are port sets (1-based)."""
        data = {"vid": str(vid), "name": name or ""}
        for idx in range(self.nports):  # form is 0-based: vlanPort_0..N-1
            p = idx + 1
            val = 0 if p in untagged else 1 if p in tagged else 2
            data[f"vlanPort_{idx}"] = str(val)
        self._post("/vlan.cgi?page=static", data)

    def delete_vlan(self, vid):
        self._post(
            "/vlan.cgi?page=getRmvVlanEntry",
            {f"remove_{vid}": "on", "Delete": "    Delete    "},
        )

    def set_pvid(self, ports, pvid, accept_code):
        """ports: iterable of 1-based port numbers."""
        data = [("ports", str(p - 1)) for p in ports]  # form is 0-based, multi-select
        data.append(("pvid", str(pvid)))
        data.append(("vlan_accept_frame_type", str(accept_code)))
        self._post("/vlan.cgi?page=port_based", data)

    def save(self):
        self._post("/save.cgi", {"cmd": "save"})

    def version(self):
        txt = re.sub(r"<[^>]*>", "", self._get("/info.cgi"))
        return [l.strip() for l in txt.splitlines() if l.strip()]

    # -- generic page text scrape helper ----------------------------------- #
    def _text_lines(self, path):
        html = self._get(path)
        html = re.sub(r"<script\b.*?</script>", "", html, flags=re.S | re.I)
        html = re.sub(r"<style\b.*?</style>", "", html, flags=re.S | re.I)
        txt = re.sub(r"<[^>]*>", "\n", html)
        return [l.strip() for l in txt.splitlines() if l.strip()]

    # ===================================================================== #
    #  System: IP / user / port speed-duplex-flow
    # ===================================================================== #
    def fetch_ip(self):
        """Return {'ip','netmask','gateway','dhcp'} from ip.cgi."""
        html = self._get("/ip.cgi")
        def val(name):
            m = re.search(rf'name="{name}"[^>]*value="([^"]*)"', html)
            return m.group(1) if m else ""
        dhcp = "1" if re.search(r'name="dhcp_state".*?<option value="1"[^>]*selected', html, re.S) else "0"
        return {"ip": val("ip"), "netmask": val("netmask"),
                "gateway": val("gateway"), "dhcp": dhcp}

    def set_ip(self, ip, netmask, gateway, dhcp):
        """DANGEROUS: changes management IP. dhcp 0/1."""
        self._post("/ip.cgi", {
            "ip": ip, "netmask": netmask, "gateway": gateway,
            "dhcp_state": str(dhcp), "cmd": "ip",
        })

    def set_user(self, username, password):
        """DANGEROUS: changes admin credentials."""
        self._post("/user.cgi", {
            "mname": username, "mpass": password, "mpass2": password,
            "cmd": "passwd",
        })

    def set_port_cfg(self, port, enable, speed_code, flow):
        """Full per-port config: state, speed/duplex code, flow control (0/1)."""
        self._post("/port.cgi", {
            "portid": str(port - 1),
            "state": "1" if enable else "0",
            "speed_duplex": str(speed_code),
            "flow": str(flow),
            "cmd": "port",
        })

    def fetch_port_cfg(self):
        """port -> {'state','cfg_speed','act_speed','flow_cfg'} (config columns)."""
        cells = [c.strip() for c in
                 re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "|", self._get("/port.cgi"))).split("|")
                 if c.strip()]
        # The status table rows are 6 cells each:
        #   Port N | State | Config-speed | Actual-speed | Flow-cfg | Flow-actual
        # Only treat a "Port N" as a row when the next cell is an admin state, so we skip
        # the config-form port <select> options (Port 1..6 with no State after them).
        out = {}
        i = 0
        while i < len(cells):
            m = re.fullmatch(r"Port (\d+)", cells[i])
            if m and i + 4 < len(cells) and cells[i + 1] in ("Enable", "Disable"):
                p = int(m.group(1))
                out[p] = {"state": cells[i + 1], "cfg_speed": cells[i + 2],
                          "act_speed": cells[i + 3], "flow_cfg": cells[i + 4]}
                i += 6
                continue
            i += 1
        return out

    # ===================================================================== #
    #  QoS
    # ===================================================================== #
    def set_port_priority(self, ports, prio_code):
        data = [("portid", str(p - 1)) for p in ports]
        data += [("port_priority", str(prio_code)), ("cmd", "portprio")]
        self._post("/qos.cgi?page=port_pri", data)

    def set_queue_weight(self, queues, weight):
        """queues: iterable of 1-based queue ids (1-8). weight 0=strict, 1-15."""
        data = [("queueid", str(q - 1)) for q in queues]
        data += [("weight", str(weight)), ("cmd", "qweight")]
        self._post("/qos.cgi?page=que_weight", data)

    def fetch_qos(self):
        return {"port_pri": self._text_lines("/qos.cgi?page=port_pri"),
                "sched": self._text_lines("/qos.cgi?page=pkt_sch")}

    # ===================================================================== #
    #  Loop protection / STP
    # ===================================================================== #
    def set_loop(self, func_type, interval=2, recover=10):
        """func_type: 0 Off, 1 Loop Detection, 2 Loop Prevention, 3 Spanning Tree."""
        self._post("/loop.cgi", {
            "func_type": str(func_type),
            "interval_time": str(interval), "recover_time": str(recover),
            "cmd": "loop",
        })

    def set_loop_port(self, ports, enable):
        data = [("portid", str(p - 1)) for p in ports]
        data += [("portEnable", "1" if enable else "0"), ("cmd", "rlp")]
        self._post("/loop_port.cgi", data)

    def set_stp_global(self, version, priority, maxage=20, hello=2, delay=15):
        """version: 0 STP, 1 RSTP. priority multiple of 4096."""
        self._post("/loop.cgi?page=stp_global", {
            "version": str(version), "priority": str(priority),
            "maxage": str(maxage), "hello": str(hello), "delay": str(delay),
            "cmd": "stp",
        })

    def set_stp_port(self, ports, cost, priority, p2p, edge):
        """cost int (0=auto). priority 0-240 step16. p2p: false/true/auto. edge: false/true."""
        data = [("portid", str(p - 1)) for p in ports]
        data += [("cost", str(cost)), ("priority", str(priority)),
                 ("p2p", p2p), ("edge", edge), ("cmd", "stp_port")]
        self._post("/loop.cgi?page=stp_port", data)

    def fetch_loop(self):
        return {"loop": self._text_lines("/loop.cgi"),
                "stp_global": self._text_lines("/loop.cgi?page=stp_global"),
                "stp_port": self._text_lines("/loop.cgi?page=stp_port")}

    # ===================================================================== #
    #  IGMP snooping
    # ===================================================================== #
    def set_igmp(self, enable):
        data = {"cmd": "enable_igmp"}
        if enable:
            data["enable_igmp"] = "on"
        self._post("/igmp.cgi?page=enable_igmp", data)

    def fetch_igmp(self):
        html = self._get("/igmp.cgi?page=dump")
        on = bool(re.search(r'name="enable_igmp"[^>]*checked', html))
        return {"enabled": on, "lines": self._text_lines("/igmp.cgi?page=dump")}

    # ===================================================================== #
    #  Link aggregation (trunk)
    # ===================================================================== #
    def set_trunk(self, group_id, trunk_type, ports):
        """group_id 1/2; trunk_type 0 static / 1 LACP; ports 1-based set."""
        data = [("id", str(group_id)), ("trunk_type", str(trunk_type))]
        data += [("ports", str(p - 1)) for p in ports]
        data.append(("cmd", "trunk"))
        self._post("/trunk.cgi?page=group", data)

    def delete_trunk(self, group_id):
        self._post("/trunk.cgi?page=group_remove",
                   {f"remove_{group_id}": "on", "cmd": "group_remove"})

    def fetch_trunk(self):
        return self._text_lines("/trunk.cgi?page=group")

    # ===================================================================== #
    #  Port mirroring / isolation / bandwidth
    # ===================================================================== #
    def set_mirror(self, direction, dest_port, source_port):
        """direction 1 Rx / 2 Tx / 3 Both. dest 0-based value; source 'Port N'."""
        self._post("/port.cgi?page=mirroring", {
            "mirror_direction": str(direction),
            "mirroring_port": str(dest_port - 1),
            "mirrored_port": f"Port {source_port}",
            "cmd": "mirror",
        })

    def delete_mirror(self):
        self._post("/port.cgi?page=delete_mirror", {"cmd": "del_mirror"})

    def set_isolation(self, ports, isolated_from):
        """Ports `ports` are isolated from ports `isolated_from` (both 1-based sets)."""
        data = [("port", f"Port {p}") for p in ports]
        data += [("isolationlist", f"Port {p}") for p in isolated_from]
        data.append(("cmd", "portisolation"))
        self._post("/port.cgi?page=isolation", data)

    def set_bw(self, ports, direction, state, rate):
        """direction 0 ingress / 1 egress. state 0/1. rate kbit/sec (ignored if disabled)."""
        data = [("portid", str(p - 1)) for p in ports]
        data += [("type", str(direction)), ("state", str(state)),
                 ("rate", str(rate)), ("cmd", "bandwidthcontrol")]
        self._post("/port.cgi?page=bwctrl", data)

    def fetch_mirror(self):
        return self._text_lines("/port.cgi?page=mirroring")

    def fetch_isolation(self):
        return self._text_lines("/port.cgi?page=isolation")

    def fetch_bw(self):
        return self._text_lines("/port.cgi?page=bw_ctrl")

    # ===================================================================== #
    #  Forwarding: jumbo frame / storm control
    # ===================================================================== #
    def set_jumbo(self, code):
        """code 0=1522,1=1536,2=1552,3=9216,4=16383."""
        self._post("/fwd.cgi?page=jumboframe", {"jumboframe": str(code), "cmd": "jumboframe"})

    def fetch_jumbo(self):
        html = self._get("/fwd.cgi?page=jumboframe")
        m = re.search(r'<option value="(\d+)"[^>]*selected[^>]*>\s*(\d+)', html)
        return m.group(2) if m else "?"

    def set_storm(self, storm_filter, ports, action, rate):
        """storm_filter: 0 unknown-unicast,1 unknown-multicast,2 known-multicast,3 broadcast.
        action 0 off / 1 on. ports 1-based. rate kbps."""
        data = [("storm_filter", str(storm_filter))]
        data += [("portid", f"Port {p}") for p in ports]
        data += [("action", str(action)), ("rate", str(rate)), ("cmd", "storm")]
        self._post("/fwd.cgi?page=storm_ctrl", data)

    def fetch_storm(self):
        return self._text_lines("/fwd.cgi?page=storm_ctrl")

    # ===================================================================== #
    #  MAC address table / static MAC / port security
    # ===================================================================== #
    def fetch_mac_table(self):
        """Return {'raw': lines, 'macs': rows} from the dynamic forwarding table."""
        lines = self._text_lines("/mac.cgi?page=fwd_tbl")
        rows = [ln for ln in lines if re.match(r"[0-9A-Fa-f:]{17}", ln)]
        return {"raw": lines, "macs": rows}

    def clear_mac_table(self):
        self._post("/mac.cgi?page=fwd_tbl", {"cmd": "mactblclr"})

    def add_static_mac(self, mac, vlan, port):
        self._post("/mac.cgi?page=static", {
            "mac": mac, "vlan": str(vlan), "src": str(port - 1), "cmd": "macstatic",
        })

    def delete_static_mac(self, idx):
        self._post("/mac.cgi?page=staticdel",
                   {f"remove_{idx}": "on", "cmd": "macstatictbl"})

    def fetch_static_mac(self):
        return self._text_lines("/mac.cgi?page=static")

    def set_mac_constraint(self, ports, state, limit):
        """Per-port MAC count limit (port security). state 0/1, limit int."""
        data = [("portid", str(p - 1)) for p in ports]
        data += [("state", str(state)), ("limit", str(limit)), ("cmd", "mac_constraint")]
        self._post("/mac_constraint.cgi", data)

    def fetch_mac_constraint(self):
        return self._text_lines("/mac_constraint.cgi")

    # ===================================================================== #
    #  EEE
    # ===================================================================== #
    def set_eee(self, enable):
        self._post("/eee.cgi", {"func_type": "1" if enable else "0", "cmd": "loop"})

    def fetch_eee(self):
        html = self._get("/eee.cgi")
        return bool(re.search(r'name="func_type".*?<option value="1"[^>]*selected', html, re.S))

    # ===================================================================== #
    #  Tools: config backup / restore, firmware, reboot, factory reset
    # ===================================================================== #
    def backup_config(self, dest_path):
        """Download the binary config blob (GET, safe)."""
        if not self._logged_in:
            self.login()
        r = self.s.get(self._base + "/config_back.cgi?cmd=conf_backup", timeout=self.timeout)
        r.raise_for_status()
        with open(dest_path, "wb") as fh:
            fh.write(r.content)
        return len(r.content)

    def restore_config(self, src_path):
        """DANGEROUS: upload a config blob (multipart)."""
        if self.dry_run:
            print(f"  DRY-RUN POST /config_back.cgi?cmd=conf_restore  <file {src_path}>")
            return
        if not self._logged_in:
            self.login()
        with open(src_path, "rb") as fh:
            self.s.post(self._base + "/config_back.cgi?cmd=conf_restore",
                        files={"submitFile": (os.path.basename(src_path), fh)},
                        timeout=self.timeout).raise_for_status()

    def firmware_upgrade(self):
        """DANGEROUS: enters bootloader for firmware upload."""
        self._post("/fwug.cgi", {"cmd": "enter_loader"})

    def reboot(self):
        """DANGEROUS: reboot the switch."""
        self._post("/reboot.cgi", {"cmd": "reboot"})

    def factory_reset(self):
        """DANGEROUS: restore factory defaults."""
        self._post("/reset.cgi", {"cmd": "factory_default"})


# --------------------------------------------------------------------------- #
# Interface naming — Cisco-style media types
#   physical ports 1-4 (2.5G)  -> TwoGigabitEthernet1..4   (abbrev "tw")
#   physical ports 5-6 (10G)   -> TenGigabitEthernet1..2   (abbrev "te")
# --------------------------------------------------------------------------- #
_IFTYPES = {  # canonical name -> (min abbrev, physical-port offset, count)
    "twogigabitethernet": ("tw", 0, 4),  # phys = offset + index  (Two1 -> 1)
    "tengigabitethernet": ("te", 4, 2),  # Ten1 -> phys 5
}


def port_name(p):
    """Physical port (1-based) -> full Cisco-style name."""
    return f"TwoGigabitEthernet{p}" if p <= 4 else f"TenGigabitEthernet{p - 4}"


def port_short(p):
    return f"Tw{p}" if p <= 4 else f"Te{p - 4}"


def iface_names():
    """All valid full interface names, for completion / '?'."""
    return [f"TwoGigabitEthernet{i}" for i in (1, 2, 3, 4)] + [
        f"TenGigabitEthernet{i}" for i in (1, 2)
    ]


def _iftype(letters):
    """Resolve a (possibly abbreviated) type prefix -> canonical name, or raise."""
    letters = letters.lower()
    hits = [name for name, (ab, _, _) in _IFTYPES.items() if name.startswith(letters)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ValueError(f"unknown interface type '{letters}'")
    raise ValueError(f"ambiguous interface type '{letters}' ({', '.join(_IFTYPES[h][0] for h in hits)})")


def ifname_to_port(token):
    """'tw1', 'te2', 'TwoGigabitEthernet3', or bare '5' -> physical port int."""
    token = token.strip()
    m = re.fullmatch(r"([A-Za-z]+)\s*(\d+)", token)
    if m:
        canon = _iftype(m.group(1))
        _, off, count = _IFTYPES[canon]
        idx = int(m.group(2))
        if not 1 <= idx <= count:
            raise ValueError(f"{canon} has no port {idx} (1-{count})")
        return off + idx
    if re.fullmatch(r"\d+", token):  # bare physical port number
        return int(token)
    raise ValueError(f"invalid interface '{token}'")


def parse_ifrange(spec):
    """'tw1-4', 'te1,te2', 'TwoGigabitEthernet2', '1,5' -> sorted physical port ints."""
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            pa = ifname_to_port(a)
            # end may be a bare number sharing the start's type, or a full name
            b = b.strip()
            pb = ifname_to_port(b) if re.search(r"[A-Za-z]", b) else (
                _IFTYPES[_iftype(re.match(r"([A-Za-z]+)", a.strip()).group(1))][1] + int(b)
                if re.search(r"[A-Za-z]", a) else int(b)
            )
            out.update(range(min(pa, pb), max(pa, pb) + 1))
        else:
            out.add(ifname_to_port(part))
    return sorted(out)


# --------------------------------------------------------------------------- #
# IOS-ish command engine — declarative grammar with abbreviation, ? and tab
# --------------------------------------------------------------------------- #
class CmdError(Exception):
    """Raised by the grammar resolver for unknown/incomplete/ambiguous input."""


def _confirm(prompt_text):
    """Interactive [y/N] gate for disruptive actions. Returns True only on explicit yes."""
    try:
        ans = input(f"{prompt_text} [y/N] ")
    except EOFError:
        return False
    return ans.strip().lower().startswith("y")


# Token-spec constructors used in the grammar table below.
def _lit(w):
    return ("lit", w)


def _kw(name, *choices):
    """A choice keyword. Each choice is either a bare string or a (choice, help) pair.

    Per-choice help lives HERE, in the grammar tree — so the same word ('on', 'auto',
    'static') can carry a different '?' description in each command, instead of a single
    global meaning. spec[2] = tuple of choice strings; spec[3] = {choice: help}.
    """
    names, help_map = [], {}
    for c in choices:
        if isinstance(c, tuple):
            names.append(c[0])
            help_map[c[0]] = c[1]
        else:
            names.append(c)
    return ("kw", name, tuple(names), help_map)


def _int(name):
    return ("int", name)


def _rest(name, hint=""):
    return ("rest", name, hint)


def _arg(name):
    """A single positional token (not abbreviated, not joined)."""
    return ("arg", name)


def _iface(name="range"):
    return ("iface", name)


# Reused (choice, help) sets — defined once so both the set and 'no' rows stay in sync.
_RL_DIR = (("ingress", "Limit received (ingress) traffic"),
           ("egress", "Limit transmitted (egress) traffic"))
_STORM_KIND = (("broadcast", "Broadcast frames"),
               ("multicast", "Known multicast frames"),
               ("unknown-unicast", "Unknown (flooded) unicast frames"),
               ("unknown-multicast", "Unknown multicast frames"))


ALL = ("exec", "config", "vlan", "iface")
CFG = ("config", "vlan", "iface")  # config-level cmds also re-dispatch from sub-modes

# Per-keyword help text shown by '?' (IOS-style description column).
HELP = {
    "show": "Display switch information",
    "vlan": "VLAN table ('show') / create-enter a VLAN (config)",
    "brief": "Condensed output",
    "interfaces": "Per-port status",
    "status": "Per-port status",
    "running-config": "Current running configuration",
    "version": "Model / firmware / addresses",
    "enable": "Enter privileged mode (already privileged)",
    "configure": "Enter global configuration mode",
    "terminal": "Configure from the terminal",
    "write": "Save running config to flash",
    "memory": "Save to flash",
    "copy": "Copy configuration",
    "startup-config": "The saved (flash) configuration",
    "exit": "Leave the current mode",
    "quit": "Leave the current mode",
    "end": "Return to privileged EXEC mode",
    "no": "Negate / remove a setting",
    "interface": "Select an interface to configure",
    "range": "Select a range of interfaces",
    "name": "Set the VLAN name",
    "description": "Set a local port description",
    "shutdown": "Administratively disable the port",
    "switchport": "Configure Layer-2 switching",
    "access": "Access-port settings",
    "mode": "Select a mode / variant",
    "trunk": "Trunk-port settings",
    "exclusive-trunk": "Trunk accepting tagged frames only (native retained but ignored)",
    "native": "Trunk native (untagged) VLAN",
    "allowed": "VLANs carried on the trunk",
    # system / ip / user
    "ip": "IP / management / IGMP settings",
    "address": "Set management IP address",
    "dhcp": "Obtain management IP via DHCP",
    "username": "Set the admin account username + password",
    "mac": "MAC address-table / static MAC",
    "address-table": "Layer-2 MAC forwarding table",
    "static": "Static (non-protocol) entry / aggregation",
    "dynamic": "Dynamic (learned) MAC entries",
    "clear": "Clear / reset a table or counters",
    # interface-level new
    "speed": "Set port speed (auto/10/100/1000/2500/10g)",
    "duplex": "Set port duplex (auto/half/full)",
    "flowcontrol": "Set 802.3x flow control (on/off)",
    "priority": "Set a priority value",
    "storm-control": "Per-port storm control",
    "rate-limit": "Per-port ingress/egress bandwidth limit",
    "isolation": "Port isolation (block forwarding to listed ports)",
    "channel-group": "Add this port to a port-channel (EtherChannel)",
    "spanning-tree": "STP/RSTP settings",
    "cost": "STP path cost (0 = auto)",
    "port-priority": "STP port priority (0-240, step 16)",
    "link-type": "STP point-to-point link type",
    "portfast": "STP edge port (portfast)",
    "loop-protection": "Loop protection (global mechanism / per-port apply)",
    "port-security": "Per-port learned-MAC count limit",
    "maximum": "Maximum number of MAC addresses",
    # qos / scheduler
    "qos": "Quality of Service",
    "wrr": "Weighted round-robin queue weight",
    "scheduler": "Queue scheduling (WRR weights / strict)",
    # global features
    "jumbo-frame": "Maximum frame size in bytes",
    "system": "Firmware management (boot system)",
    "igmp": "IGMP snooping",
    "snooping": "Enable snooping",
    "energy-efficient-ethernet": "802.3az Energy Efficient Ethernet",
    "eee": "802.3az Energy Efficient Ethernet",
    "monitor": "Port mirroring (SPAN) session",
    "session": "Mirroring session",
    "source": "Mirror source port",
    "destination": "Mirror destination port",
    "port-channel": "Link-aggregation group (EtherChannel)",
    "etherchannel": "EtherChannel / link-aggregation status",
    "summary": "One-line summary",
    # tools
    "backup": "Save the running config to a local file",
    "boot": "Firmware management",
    "reload": "Reboot the switch (disruptive)",
    "erase": "Erase configuration",
    "factory-reset": "Restore factory defaults (disruptive)",
    # Note: keyword-CHOICE descriptions (on/off/auto/stp/rx/...) are NOT here — they
    # live inline in each _kw(...) in the grammar tree so the same word can mean
    # different things in different commands. Only literals/keyword-NAMES live here.
    "level": "Storm threshold rate",
    "strict": "Strict-priority scheduling",
}
ARG_HELP = {
    "vid": "<1-4094>  VLAN ID", "list": "<vlan-list>  e.g. 10,12,777",
    "text": "<line>", "addr": "<A.B.C.D>", "mask": "<A.B.C.D>  netmask",
    "gw": "<A.B.C.D>  gateway", "user": "<name>", "pass": "<password>",
    "n": "<integer>", "rate": "<kbps>", "mac": "<HH:HH:HH:HH:HH:HH>",
    "id": "<group-id 1-2>", "cost": "<0-200000000, 0=auto>",
    "prio": "<priority>", "file": "<path>",
    "weight": "<1-15>  WRR weight", "queue": "<1-8>  egress queue",
    "if": "<interface>", "src": "<interface>", "dst": "<interface>",
    "ports": "<interface-range>", "peers": "<interface-range>",
}


class CLI:
    def __init__(self, sw):
        self.sw = sw
        self.mode = "exec"  # exec | config | vlan | iface
        self.ctx = {}  # current vlan id / iface ports
        self.dirty = False  # unsaved changes ON THE SWITCH (flash)
        self.desc = load_descriptions(sw.name)  # local port descriptions
        self.history = []  # interactive line history
        self.cmds = self._build_grammar()

    # ---- grammar table ---------------------------------------------------- #
    def _build_grammar(self):
        """Declarative command table: (modes, [token-specs], handler, help)."""
        return [
            # show — available in every mode
            (ALL, [_lit("show"), _lit("vlan")], lambda a: self.show_vlan(), "VLAN table"),
            (ALL, [_lit("show"), _lit("vlan"), _lit("brief")], lambda a: self.show_vlan(True), "VLAN table (brief)"),
            (ALL, [_lit("show"), _lit("interfaces"), _lit("status")], lambda a: self.show_interfaces(), "port status"),
            (ALL, [_lit("show"), _lit("interfaces")], lambda a: self.show_interfaces(), "port status"),
            (ALL, [_lit("show"), _lit("running-config")], lambda a: self.show_running(), "running config"),
            (ALL, [_lit("show"), _lit("version")], lambda a: self.show_version(), "system info"),
            # exec / mode transitions
            (("exec",), [_lit("enable")], lambda a: None, "Turn on privileged commands (already privileged)"),
            (("exec",), [_lit("configure"), _lit("terminal")], self._h_configure, "enter config mode"),
            (("exec",), [_lit("configure")], self._h_configure, "enter config mode"),
            # save — every mode
            (ALL, [_lit("write"), _lit("memory")], self._h_write, "save to flash"),
            (ALL, [_lit("write")], self._h_write, "save to flash"),
            (ALL, [_lit("copy"), _lit("running-config"), _lit("startup-config")], self._h_write, "save to flash"),
            (ALL, [_lit("exit")], self._h_exit, "leave current mode"),
            (ALL, [_lit("quit")], self._h_exit, "leave current mode"),
            (ALL, [_lit("end")], self._h_end, "return to exec mode"),
            # config-level (also valid from vlan/iface sub-modes -> re-dispatch)
            (CFG, [_lit("vlan"), _int("vid")], self._h_vlan, "Create / enter a VLAN"),
            (CFG, [_lit("no"), _lit("vlan"), _int("vid")], self._h_no_vlan, "delete VLAN"),
            (CFG, [_lit("interface"), _iface()], self._h_interface, "enter interface"),
            (CFG, [_lit("interface"), _lit("range"), _iface()], self._h_interface, "enter interface range"),
            # vlan mode
            (("vlan",), [_lit("name"), _rest("text", "<vlan-name>")], self._h_name, "set VLAN name"),
            # interface mode
            (("iface",), [_lit("description"), _rest("text", "<text>")], self._h_description, "local port label"),
            (("iface",), [_lit("no"), _lit("description")], self._h_no_description, "clear description"),
            (("iface",), [_lit("shutdown")], self._h_shutdown, "disable port"),
            (("iface",), [_lit("no"), _lit("shutdown")], self._h_no_shutdown, "enable port"),
            (("iface",), [_lit("switchport"), _lit("access"), _lit("vlan"), _int("vid")], self._h_access, "access VLAN"),
            (("iface",), [_lit("switchport"), _lit("mode"), _kw("mode",
                ("access", "Single untagged VLAN"),
                ("trunk", "Tag all VLANs, accept all frames"),
                ("exclusive-trunk", "Tag all VLANs, accept tagged only"))], self._h_mode, "port mode"),
            (("iface",), [_lit("switchport"), _lit("trunk"), _lit("native"), _lit("vlan"), _int("vid")], self._h_native, "trunk native VLAN"),
            (("iface",), [_lit("no"), _lit("switchport"), _lit("trunk"), _lit("native"), _lit("vlan")], self._h_no_native, "reset native to VLAN 1"),
            (("iface",), [_lit("switchport"), _lit("trunk"), _lit("allowed"), _lit("vlan"), _kw("op", ("add", "Add VLANs to the tagged set"), ("remove", "Remove VLANs from the tagged set")), _rest("list", "<vlan-list>")], self._h_allowed, "edit trunk VLANs"),
            (("iface",), [_lit("switchport"), _lit("trunk"), _lit("allowed"), _lit("vlan"), _rest("list", "<vlan-list>")], self._h_allowed, "set trunk VLANs"),

            # ---- new SHOW commands (all modes) ---------------------------- #
            (ALL, [_lit("show"), _lit("ip")], lambda a: self.show_ip(), "management IP"),
            (ALL, [_lit("show"), _lit("mac"), _lit("address-table")], lambda a: self.show_mac_table(), "MAC table"),
            (ALL, [_lit("show"), _lit("mac"), _lit("address-table"), _lit("static")], lambda a: self.show_static_mac(), "static MACs"),
            (ALL, [_lit("show"), _lit("spanning-tree")], lambda a: self.show_stp(), "STP status"),
            (ALL, [_lit("show"), _lit("qos")], lambda a: self.show_qos(), "QoS settings"),
            (ALL, [_lit("show"), _lit("storm-control")], lambda a: self.show_storm(), "storm control"),
            (ALL, [_lit("show"), _lit("ip"), _lit("igmp"), _lit("snooping")], lambda a: self.show_igmp(), "IGMP snooping"),
            (ALL, [_lit("show"), _lit("etherchannel"), _lit("summary")], lambda a: self.show_trunk(), "port-channel summary"),
            (ALL, [_lit("show"), _lit("etherchannel")], lambda a: self.show_trunk(), "port-channel summary"),
            (ALL, [_lit("show"), _lit("monitor")], lambda a: self.show_mirror(), "port mirroring"),
            (ALL, [_lit("show"), _lit("isolation")], lambda a: self.show_isolation(), "port isolation"),
            (ALL, [_lit("show"), _lit("jumbo-frame")], lambda a: self.show_jumbo(), "jumbo frame size"),
            (ALL, [_lit("show"), _lit("eee")], lambda a: self.show_eee(), "EEE state"),
            (ALL, [_lit("show"), _lit("rate-limit")], lambda a: self.show_bw(), "bandwidth limits"),
            (ALL, [_lit("show"), _lit("port-security")], lambda a: self.show_mac_constraint(), "MAC count limits"),

            # ---- System: IP / username (DANGEROUS - dry-run/guarded) ------ #
            (CFG, [_lit("ip"), _lit("address"), _arg("addr"), _arg("mask"), _arg("gw")], self._h_ip_address, "set mgmt IP (disruptive)"),
            (CFG, [_lit("ip"), _lit("address"), _lit("dhcp")], self._h_ip_dhcp, "mgmt IP via DHCP (disruptive)"),
            (CFG, [_lit("username"), _arg("user"), _lit("password"), _arg("pass")], self._h_username, "set admin user/pass (disruptive)"),

            # ---- Global feature toggles ----------------------------------- #
            (CFG, [_lit("jumbo-frame"), _kw("size",
                ("1522", "1522 bytes (standard Ethernet + tag)"), ("1536", "1536 bytes"),
                ("1552", "1552 bytes"), ("9216", "9216 bytes (jumbo)"),
                ("16383", "16383 bytes (maximum)"))], self._h_jumbo, "set jumbo frame size (bytes)"),
            (CFG, [_lit("ip"), _lit("igmp"), _lit("snooping")], lambda a: self._h_igmp(True), "enable IGMP snooping"),
            (CFG, [_lit("no"), _lit("ip"), _lit("igmp"), _lit("snooping")], lambda a: self._h_igmp(False), "disable IGMP snooping"),
            (CFG, [_lit("energy-efficient-ethernet")], lambda a: self._h_eee(True), "enable EEE"),
            (CFG, [_lit("no"), _lit("energy-efficient-ethernet")], lambda a: self._h_eee(False), "disable EEE"),
            (CFG, [_lit("loop-protection"), _kw("mode",
                ("off", "No loop protection"),
                ("loop-detection", "Detect loops and report (no blocking)"),
                ("loop-prevention", "Detect loops and block the port"))], self._h_loop_mode, "global loop-protection mechanism"),
            (CFG, [_lit("no"), _lit("loop-protection")], lambda a: self._h_loop_mode_off(), "disable loop protection"),

            # ---- QoS scheduler (global) ----------------------------------- #
            (CFG, [_lit("qos"), _lit("scheduler"), _lit("strict"), _int("queue")], self._h_sched_strict, "queue strict priority"),
            (CFG, [_lit("qos"), _lit("scheduler"), _lit("wrr"), _int("queue"), _int("weight")], self._h_sched_wrr, "queue WRR weight"),

            # ---- Spanning tree (global) ----------------------------------- #
            (CFG, [_lit("spanning-tree"), _lit("mode"), _kw("mode",
                ("stp", "802.1D Spanning Tree Protocol"),
                ("rstp", "802.1w Rapid Spanning Tree Protocol"))], self._h_stp_mode, "enable STP/RSTP as the loop mechanism"),
            (CFG, [_lit("no"), _lit("spanning-tree")], lambda a: self._h_loop_mode_off(), "disable spanning-tree (loop mechanism off)"),
            (CFG, [_lit("spanning-tree"), _lit("priority"), _int("prio")], self._h_stp_priority, "bridge priority (x4096)"),
            (CFG, [_lit("spanning-tree"), _lit("max-age"), _int("n")], lambda a: self._h_stp_timer("maxage", a["n"]), "STP max-age"),
            (CFG, [_lit("spanning-tree"), _lit("hello-time"), _int("n")], lambda a: self._h_stp_timer("hello", a["n"]), "STP hello time"),
            (CFG, [_lit("spanning-tree"), _lit("forward-time"), _int("n")], lambda a: self._h_stp_timer("delay", a["n"]), "STP forward delay"),

            # ---- Link aggregation (global) — IOS port-channel ------------- #
            (CFG, [_lit("port-channel"), _int("id"), _lit("mode"), _kw("mode",
                ("on", "Static aggregation, no LACP"),
                ("active", "LACP, actively negotiate"),
                ("passive", "LACP, respond only")),
                _lit("interface"), _iface("ports")], self._h_trunk, "create/modify port-channel"),
            (CFG, [_lit("no"), _lit("port-channel"), _int("id")], self._h_no_trunk, "delete port-channel"),

            # ---- Port mirroring (global) ---------------------------------- #
            (CFG, [_lit("monitor"), _lit("session"), _lit("source"), _arg("src"), _kw("dir", ("rx", "Mirror received traffic"), ("tx", "Mirror transmitted traffic"), ("both", "Mirror both directions")), _lit("destination"), _arg("dst")], self._h_monitor, "mirror src->dst"),
            (CFG, [_lit("no"), _lit("monitor"), _lit("session")], self._h_no_monitor, "delete mirror session"),

            # ---- Static MAC / MAC table (global) -------------------------- #
            (CFG, [_lit("mac"), _lit("address-table"), _lit("static"), _arg("mac"), _lit("vlan"), _int("vid"), _lit("interface"), _iface("if")], self._h_static_mac, "add static MAC"),
            (CFG, [_lit("no"), _lit("mac"), _lit("address-table"), _lit("static"), _int("n")], self._h_no_static_mac, "delete static MAC by index"),
            (ALL, [_lit("clear"), _lit("mac"), _lit("address-table"), _lit("dynamic")], self._h_clear_mac, "clear learned MACs"),

            # ---- interface-mode: speed / duplex / flow / qos / stp / etc -- #
            (("iface",), [_lit("speed"), _kw("speed",
                ("auto", "Auto-negotiate speed"), ("10", "Force 10 Mbit/s"),
                ("100", "Force 100 Mbit/s"), ("1000", "Force 1 Gbit/s"),
                ("2500", "Force 2.5 Gbit/s"), ("10g", "Force 10 Gbit/s (SFP+)"))],
                self._h_speed, "port speed"),
            (("iface",), [_lit("duplex"), _kw("duplex",
                ("auto", "Auto-negotiate duplex"), ("half", "Half duplex (10/100M only)"),
                ("full", "Full duplex"))], self._h_duplex, "port duplex"),
            (("iface",), [_lit("flowcontrol"), _kw("state",
                ("on", "Enable 802.3x flow control"),
                ("off", "Disable flow control"))], self._h_flow, "flow control"),
            (("iface",), [_lit("qos"), _lit("priority"), _int("prio")], self._h_qos_priority, "default priority queue"),
            (("iface",), [_lit("rate-limit"), _kw("dir", *_RL_DIR), _int("rate")], self._h_rate_limit, "bandwidth limit (kbps)"),
            (("iface",), [_lit("no"), _lit("rate-limit"), _kw("dir", *_RL_DIR)], self._h_no_rate_limit, "remove bandwidth limit"),
            (("iface",), [_lit("storm-control"), _kw("kind", *_STORM_KIND), _lit("level"), _int("rate")], self._h_storm_on, "enable storm control"),
            (("iface",), [_lit("no"), _lit("storm-control"), _kw("kind", *_STORM_KIND)], self._h_storm_off, "disable storm control"),
            (("iface",), [_lit("isolation"), _iface("peers")], self._h_isolation, "isolate from ports"),
            (("iface",), [_lit("no"), _lit("isolation")], self._h_no_isolation, "clear isolation"),
            (("iface",), [_lit("channel-group"), _int("id"), _lit("mode"), _kw("mode",
                ("on", "Static aggregation, no LACP"),
                ("active", "LACP, actively negotiate"),
                ("passive", "LACP, respond only"))], self._h_channel_group, "add to port-channel"),
            (("iface",), [_lit("no"), _lit("channel-group")], self._h_no_channel_group, "remove from port-channel"),
            (("iface",), [_lit("loop-protection")], lambda a: self._h_loop_port(True), "apply loop protection on this port"),
            (("iface",), [_lit("no"), _lit("loop-protection")], lambda a: self._h_loop_port(False), "exempt this port from loop protection"),
            (("iface",), [_lit("port-security"), _lit("maximum"), _int("n")], self._h_port_security, "set MAC limit"),
            (("iface",), [_lit("no"), _lit("port-security")], self._h_no_port_security, "disable MAC limit"),
            (("iface",), [_lit("spanning-tree"), _lit("cost"), _int("cost")], self._h_stp_cost, "STP path cost"),
            (("iface",), [_lit("spanning-tree"), _lit("port-priority"), _int("prio")], self._h_stp_pport, "STP port priority"),
            (("iface",), [_lit("spanning-tree"), _lit("link-type"), _kw("p2p",
                ("point-to-point", "Treat link as point-to-point (fast transition)"),
                ("shared", "Treat link as shared (half-duplex segment)"),
                ("auto", "Derive link type from duplex"))], self._h_stp_p2p, "STP link type"),
            (("iface",), [_lit("spanning-tree"), _lit("portfast")], lambda a: self._h_stp_edge(True), "STP edge port"),
            (("iface",), [_lit("no"), _lit("spanning-tree"), _lit("portfast")], lambda a: self._h_stp_edge(False), "clear STP edge"),

            # ---- Tools (exec) --------------------------------------------- #
            (("exec",), [_lit("copy"), _lit("running-config"), _lit("backup"), _rest("file", "<path>")], self._h_backup, "download config blob"),
            (("exec",), [_lit("copy"), _lit("backup"), _lit("running-config"), _rest("file", "<path>")], self._h_restore, "restore config (disruptive)"),
            (("exec",), [_lit("boot"), _lit("system")], self._h_firmware, "firmware upgrade (disruptive)"),
            (("exec",), [_lit("reload")], self._h_reload, "reboot switch (disruptive)"),
            (("exec",), [_lit("erase"), _lit("startup-config")], self._h_factory, "factory reset (disruptive)"),
            (("exec",), [_lit("factory-reset")], self._h_factory, "factory reset (disruptive)"),
        ]

    # ---- show commands ---------------------------------------------------- #
    def show_vlan(self, brief=False):
        vlans = self.sw.fetch_vlans()
        print(f"{'VLAN':<6}{'Name':<20}{'Untagged':<14}{'Tagged':<14}")
        print("-" * 54)
        for vid in sorted(vlans):
            v = vlans[vid]
            print(
                f"{vid:<6}{v['name'][:19]:<20}"
                f"{fmt_portlist(v['untagged']):<14}{fmt_portlist(v['tagged']):<14}"
            )

    def show_interfaces(self):
        ports = self.sw.fetch_ports()
        vlans = self.sw.fetch_vlans()
        print(
            f"{'Interface':<11}{'Cap':<6}{'Link':<10}{'Speed':<8}{'Admin':<8}{'PVID':<6}"
            f"{'Mode':<10}{'Description':<22}{'VLANs (u=untag,t=tag)'}"
        )
        print("-" * 107)
        for p in range(1, self.sw.nports + 1):
            info = ports[p]
            u = sorted(vid for vid, v in vlans.items() if p in v["untagged"])
            t = sorted(vid for vid, v in vlans.items() if p in v["tagged"])
            mode = "access" if not t else ("ex-trunk" if info.get("accept") == 1 else "trunk")
            memb = ",".join([f"{vid}u" for vid in u] + [f"{vid}t" for vid in t]) or "-"
            speed = info.get("speed", "-") if info.get("link") == "Link Up" else "-"
            print(
                f"{port_short(p):<11}{port_capability(p):<6}{info.get('link','?'):<10}"
                f"{speed:<8}{info.get('state','?'):<8}{info.get('pvid','?'):<6}{mode:<10}"
                f"{(self.desc.get(p,'') or '-')[:21]:<22}{memb}"
            )

    def show_running(self):
        vlans = self.sw.fetch_vlans()
        ports = self.sw.fetch_ports()
        print(f"! running-config of {self.sw.name} ({self.sw.host})")
        print("!")
        # Global settings (best-effort; ignore parse failures so config still prints).
        try:
            jumbo = self.sw.fetch_jumbo()
            if jumbo not in ("?", "1522"):
                print(f"jumbo-frame {jumbo}")
            if self.sw.fetch_igmp()["enabled"]:
                print("ip igmp snooping")
            if self.sw.fetch_eee():
                print("energy-efficient-ethernet")
            print("!")
        except Exception:
            pass
        pcfg = {}
        try:
            pcfg = self.sw.fetch_port_cfg()
        except Exception:
            pass
        for vid in sorted(vlans):
            if vid == 1:
                continue
            v = vlans[vid]
            print(f"vlan {vid}")
            if v["name"]:
                print(f" name {v['name']}")
        print("!")
        for p in range(1, self.sw.nports + 1):
            info = ports[p]
            u = sorted(vid for vid, v in vlans.items() if p in v["untagged"])
            t = sorted(vid for vid, v in vlans.items() if p in v["tagged"])
            print(f"interface {port_name(p)}")
            if self.desc.get(p):
                print(f" description {self.desc[p]}")
            if t:
                print(" switchport mode " + ("exclusive-trunk" if info.get("accept") == 1 else "trunk"))
                native = info.get("pvid") or 1
                if native != 1:  # native VLAN 1 is the default and is left implicit
                    print(f" switchport trunk native vlan {native}")
                allowed = sorted(set(t) | set(u))
                print(f" switchport trunk allowed vlan {','.join(map(str, allowed))}")
            else:
                print(" switchport mode access")
                print(f" switchport access vlan {info.get('pvid','?')}")
            pc = pcfg.get(p, {})
            cs = (pc.get("cfg_speed") or "").lower()
            if cs and not cs.startswith("auto"):
                spk = {"10m/half": "10", "10m/full": "10", "100m/half": "100",
                       "100m/full": "100", "1000m/full": "1000",
                       "2500m/full": "2500", "10g/full": "10g"}.get(cs.replace(" ", ""))
                if spk:
                    print(f" speed {spk}")
            if (pc.get("flow_cfg") or "").lower().startswith("on"):
                print(" flowcontrol on")
            if info.get("state") == "Disable":
                print(" shutdown")
            print("!")

    def show_version(self):
        for line in self.sw.version():
            print(line)

    # ---- config helpers --------------------------------------------------- #
    def _apply_port_membership(self, ports, untagged_vid, tagged_vids, accept, set_pvid=True):
        """Recompute and push VLAN membership for the given ports, then set PVID.

        The switch stores membership per-VLAN, so we read all VLANs, flip these
        ports inside each affected VLAN, and re-POST only the VLANs that changed.
        """
        vlans = self.sw.fetch_vlans()
        keep = set(tagged_vids) | ({untagged_vid} if untagged_vid else set())
        # make sure target VLANs exist
        for vid in keep:
            if vid not in vlans:
                vlans[vid] = {"name": "", "tagged": set(), "untagged": set()}
        changed = set()
        for vid, v in vlans.items():
            for port in ports:
                want = (
                    "u" if vid == untagged_vid
                    else "t" if vid in tagged_vids
                    else None
                )
                cur = "u" if port in v["untagged"] else "t" if port in v["tagged"] else None
                if want == cur:
                    continue
                v["untagged"].discard(port)
                v["tagged"].discard(port)
                if want == "u":
                    v["untagged"].add(port)
                elif want == "t":
                    v["tagged"].add(port)
                changed.add(vid)
        for vid in sorted(changed):
            v = vlans[vid]
            self.sw.write_vlan(vid, v["name"], v["untagged"], v["tagged"])
        if set_pvid and (untagged_vid or tagged_vids):
            self.sw.set_pvid(ports, untagged_vid or (sorted(tagged_vids)[0]), ACCEPT_CODES[accept])
        self.dirty = True

    # ---- command engine: matcher / resolver / completion ----------------- #
    def run(self, raw):
        line = raw.strip()
        if not line or line.startswith("!"):
            return
        if line.endswith("?"):
            return self._help_for(line[:-1])
        try:
            toks = shlex.split(line)
        except ValueError as e:
            print(f"% parse error: {e}")
            return
        try:
            handler, args = self._resolve(toks)
        except CmdError as e:
            print(str(e))
            return
        try:
            handler(args)
        except CmdError as e:
            print(str(e))

    @staticmethod
    def _sig(pattern):
        parts = []
        for spec in pattern:
            if spec[0] == "lit":
                parts.append(spec[1])
            elif spec[0] == "kw":
                parts.append("|".join(spec[2]))
            else:
                parts.append(f"<{spec[1]}>")
        return " ".join(parts)

    def _try(self, pattern, tokens):
        """Match tokens against a pattern with abbreviation.

        Returns (status, args, next_spec, score):
          status 'full'    — pattern + tokens both consumed
                 'partial' — tokens ran out mid-pattern (next_spec = expected)
                 'fail'    — mismatch
        score = (literals_matched, kw_matched, -free_args) for specificity ranking.
        """
        args = {}
        ti = lits = kws = frees = 0
        for spec in pattern:
            kind = spec[0]
            if kind in ("rest", "iface"):  # consume the rest of the line (terminal)
                if ti >= len(tokens):  # nothing to consume yet -> expects a value
                    return ("partial", args, spec, (lits, kws, -frees))
                args[spec[1]] = " ".join(tokens[ti:])
                return ("full", args, None, (lits, kws, -(frees + 1)))
            if ti >= len(tokens):
                return ("partial", args, spec, (lits, kws, -frees))
            tok = tokens[ti].lower()
            if kind == "lit":
                if not spec[1].startswith(tok):
                    return ("fail", None, None, None)
                lits += 1
            elif kind == "kw":
                if tok in spec[2]:  # exact full match wins over longer prefixes (e.g. '10' vs '100'/'1000'/'10g')
                    ms = [tok]
                else:
                    ms = [c for c in spec[2] if c.startswith(tok)]
                if len(ms) != 1:
                    return ("fail", None, None, None)
                args[spec[1]] = ms[0]
                kws += 1
            elif kind == "int":
                if not re.fullmatch(r"-?\d+", tok):
                    return ("fail", None, None, None)
                args[spec[1]] = int(tok)
                frees += 1
            else:  # arg
                args[spec[1]] = tokens[ti]
                frees += 1
            ti += 1
        if ti < len(tokens):
            return ("fail", None, None, None)  # leftover tokens
        return ("full", args, None, (lits, kws, -frees))

    def _resolve(self, tokens):
        fulls, has_partial = [], False
        for modes, pattern, handler, _help in self.cmds:
            if self.mode not in modes:
                continue
            st, args, _nxt, score = self._try(pattern, tokens)
            if st == "full":
                fulls.append((score, handler, args, pattern))
            elif st == "partial":
                has_partial = True
        if fulls:
            fulls.sort(key=lambda x: x[0], reverse=True)
            tied = [f for f in fulls if f[0] == fulls[0][0]]
            if len(tied) > 1:
                raise CmdError("% Ambiguous command — matches: " + ", ".join(self._sig(f[3]) for f in tied))
            return fulls[0][1], fulls[0][2]
        if has_partial:
            raise CmdError("% Incomplete command (type '?' to see options)")
        raise CmdError(f"% Unrecognized command: {' '.join(tokens)}")

    def _next(self, completed):
        """Specs expected at the cursor after `completed` tokens, plus a <cr> flag."""
        specs, seen, cr = [], set(), False
        for modes, pattern, handler, _help in self.cmds:
            if self.mode not in modes:
                continue
            st, _a, nxt, _s = self._try(pattern, completed)
            if st == "partial" and nxt is not None:
                if repr(nxt) not in seen:
                    seen.add(repr(nxt))
                    specs.append(nxt)
            elif st == "full":
                cr = True
        return specs, cr

    @staticmethod
    def _split_partial(buf):
        """Split a (possibly mid-word) buffer into (completed_tokens, partial_word)."""
        try:
            toks = shlex.split(buf) if buf.strip() else []
        except ValueError:
            return None, None
        if buf.endswith(" ") or not buf.strip():
            return toks, ""
        return toks[:-1], toks[-1].lower()

    @staticmethod
    def _skeleton(pattern):
        return tuple(
            spec[1] if spec[0] == "lit" else "|".join(spec[2]) if spec[0] == "kw" else f"<{spec[1]}>"
            for spec in pattern
        )

    def _token_help(self, completed, token):
        """Help text for a literal `token` offered after `completed`.

        Uses the contributing command's own help when the token effectively names one
        command (so 'vlan' reads 'VLAN table' under 'show' but 'create/enter a VLAN' at
        config level); falls back to the generic table when the token branches.
        """
        contribs = []
        for modes, pattern, _handler, chelp in self.cmds:
            if self.mode not in modes:
                continue
            st, _a, nxt, _s = self._try(pattern, completed)
            if st == "partial" and nxt is not None and nxt[0] == "lit" and nxt[1] == token:
                contribs.append((self._skeleton(pattern), chelp))
        if not contribs:
            return HELP.get(token, "")
        contribs.sort(key=lambda x: len(x[0]))
        base = contribs[0][0]
        if all(sk[: len(base)] == base for sk, _ in contribs):  # all variants of one command
            return contribs[0][1] or HELP.get(token, "")
        return HELP.get(token, "")  # genuinely different commands -> generic

    def help_rows(self, completed, partial):
        """(token, description) rows valid at the cursor — for the '?' display."""
        specs, cr = self._next(completed)
        rows = []
        for spec in specs:
            kind = spec[0]
            if kind == "lit" and spec[1].startswith(partial):
                rows.append((spec[1], self._token_help(completed, spec[1])))
            elif kind == "kw":
                kwhelp = spec[3] if len(spec) > 3 else {}
                rows += [(c, kwhelp.get(c) or HELP.get(c, ""))
                         for c in spec[2] if c.startswith(partial)]
            elif kind == "iface":
                rows += [
                    (n, "2.5G port" if n.startswith("Two") else "10G port")
                    for n in iface_names()
                    if n.lower().startswith(partial)
                ]
            elif kind == "int" and not partial:
                rows.append((f"<{spec[1]}>", ARG_HELP.get(spec[1], "<integer>")))
            elif kind in ("rest", "arg") and not partial:
                rows.append((f"<{spec[1]}>", ARG_HELP.get(spec[1], (len(spec) > 2 and spec[2]) or "")))
        if cr and not partial:
            rows.append(("<cr>", ""))
        return rows

    def completions(self, completed, partial):
        """Sorted candidate strings for Tab completion of the current word."""
        specs, _cr = self._next(completed)
        cands = []
        for spec in specs:
            if spec[0] == "lit":
                cands.append(spec[1])
            elif spec[0] == "kw":
                cands += list(spec[2])
            elif spec[0] == "iface":
                cands += iface_names()
        return sorted({c for c in cands if c.lower().startswith(partial)})

    def _help_for(self, prefix):
        """Text-path '?' (used by -c scripts and the non-tty fallback)."""
        completed, partial = self._split_partial(prefix)
        if completed is None:
            print("% parse error")
            return
        rows = self.help_rows(completed, partial)
        if not rows:
            print("% No matching options")
            return
        for tok, desc in rows:
            print(f"  {tok:<28}{desc}")

    # ---- handlers --------------------------------------------------------- #
    def _h_configure(self, a):
        self.mode = "config"

    def _h_write(self, a):
        self.sw.save()
        self.dirty = False
        print("Saved to flash.")

    def _h_exit(self, a):
        if self.mode == "exec":
            raise EOFError
        self.mode = "config" if self.mode in ("vlan", "iface") else "exec"

    def _h_end(self, a):
        self.mode = "exec"

    def _h_vlan(self, a):
        vid = a["vid"]
        self.ctx = {"vid": vid}
        self.mode = "vlan"
        if vid not in self.sw.fetch_vlans():
            self.sw.write_vlan(vid, "", set(), set())
            self.dirty = True

    def _h_no_vlan(self, a):
        self.sw.delete_vlan(a["vid"])
        self.dirty = True

    def _h_interface(self, a):
        try:
            ports = parse_ifrange(a["range"])
        except ValueError as e:
            raise CmdError(f"% {e}")
        if not ports or any(p > self.sw.nports for p in ports):
            raise CmdError(f"% Invalid interface: {a['range']}")
        self.ctx = {"ports": ports}
        self.mode = "iface"

    def _h_name(self, a):
        vid = self.ctx["vid"]
        v = self.sw.fetch_vlans().get(vid, {"untagged": set(), "tagged": set()})
        self.sw.write_vlan(vid, a["text"], v["untagged"], v["tagged"])
        self.dirty = True

    def _h_description(self, a):
        for p in self.ctx["ports"]:
            self.desc[p] = a["text"]
        save_descriptions(self.sw.name, self.desc)

    def _h_no_description(self, a):
        for p in self.ctx["ports"]:
            self.desc.pop(p, None)
        save_descriptions(self.sw.name, self.desc)

    def _h_shutdown(self, a):
        # port.cgi posts state+speed+flow together, so preserve the configured
        # speed/flow when toggling admin state (don't reset the port to Auto).
        for p in self.ctx["ports"]:
            _en, sp, fl = self._port_state(p)
            self.sw.set_port_cfg(p, False, sp, fl)
        self.dirty = True

    def _h_no_shutdown(self, a):
        for p in self.ctx["ports"]:
            _en, sp, fl = self._port_state(p)
            self.sw.set_port_cfg(p, True, sp, fl)
        self.dirty = True

    def _h_access(self, a):
        self._apply_port_membership(
            self.ctx["ports"], untagged_vid=a["vid"], tagged_vids=set(), accept="untag"
        )

    def _set_accept(self, ports, accept):
        """Push only the accepted-frame-type for each port, keeping its current PVID."""
        info = self.sw.fetch_ports()
        for p in ports:
            self.sw.set_pvid([p], info[p].get("pvid") or 1, ACCEPT_CODES[accept])
        self.dirty = True

    def _h_mode(self, a):
        ports = self.ctx["ports"]
        mode = a["mode"]
        if mode == "access":
            # Access: untagged in the current access/PVID VLAN, strip every tag.
            native = self._current_untagged(ports) or self._current_native(ports) or 1
            self._apply_port_membership(
                ports, untagged_vid=native, tagged_vids=set(), accept="untag"
            )
            print(f"(access VLAN {native}; use 'switchport access vlan N' to change)")
            return
        # trunk / exclusive-trunk: tag ALL existing VLANs (IOS default with no allow-list),
        # leaving native/PVID and any untagged membership untouched.
        untagged_vid = self._current_untagged(ports)
        all_vids = set(self.sw.fetch_vlans())
        tagged = all_vids - ({untagged_vid} if untagged_vid else set())
        self._apply_port_membership(
            ports, untagged_vid=untagged_vid, tagged_vids=tagged, accept="all", set_pvid=False
        )
        if mode == "exclusive-trunk":
            self._set_accept(ports, "tag")  # Tag-only: drop untagged ingress (native ignored)
            print(f"(exclusive trunk — tagged-only, native retained but ignored; "
                  f"VLANs {','.join(map(str, sorted(tagged)))})")
        else:
            self._set_accept(ports, "all")
            print(f"(trunk allowing all VLANs: {','.join(map(str, sorted(tagged)))}; "
                  "narrow with 'switchport trunk allowed vlan ...')")

    def _h_native(self, a):
        ports = self.ctx["ports"]
        self._apply_port_membership(
            ports, untagged_vid=a["vid"], tagged_vids=self._current_tagged(ports), accept="all"
        )

    def _h_no_native(self, a):
        # IOS: 'no switchport trunk native vlan' resets the native VLAN to 1.
        ports = self.ctx["ports"]
        self._apply_port_membership(
            ports, untagged_vid=1, tagged_vids=self._current_tagged(ports), accept="all"
        )

    def _h_allowed(self, a):
        ports = self.ctx["ports"]
        untagged_vid = self._current_untagged(ports)
        cur = self._current_tagged(ports)
        lst = parse_portlist(a["list"])
        op = a.get("op")
        if op == "add":
            tagged = cur | lst
        elif op == "remove":
            tagged = cur - lst
        else:
            tagged = lst
        tagged -= {untagged_vid} if untagged_vid else set()
        self._apply_port_membership(
            ports, untagged_vid=untagged_vid, tagged_vids=tagged, accept="all", set_pvid=False
        )

    def _current_tagged(self, ports):
        vlans = self.sw.fetch_vlans()
        out = set()
        for vid, v in vlans.items():
            if any(p in v["tagged"] for p in ports):
                out.add(vid)
        return out

    def _current_native(self, ports):
        p = sorted(ports)[0]
        return self.sw.fetch_ports()[p].get("pvid") or 1  # default native VLAN is 1

    def _current_untagged(self, ports):
        """VLAN in which the first port is currently an untagged member, or None."""
        p = sorted(ports)[0]
        for vid, v in self.sw.fetch_vlans().items():
            if p in v["untagged"]:
                return vid
        return None

    # ====================================================================== #
    #  NEW show commands
    # ====================================================================== #
    def show_ip(self):
        ip = self.sw.fetch_ip()
        print(f"Management IP : {ip['ip']}")
        print(f"Netmask       : {ip['netmask']}")
        print(f"Gateway       : {ip['gateway']}")
        print(f"DHCP client   : {'enabled' if ip['dhcp'] == '1' else 'disabled'}")

    def show_mac_table(self):
        t = self.sw.fetch_mac_table()
        if not t["macs"]:
            print("(no dynamic MAC entries — or table parsed empty)")
        for ln in t["macs"]:
            print(ln)

    def show_static_mac(self):
        for ln in self.sw.fetch_static_mac():
            print(ln)

    def show_stp(self):
        d = self.sw.fetch_loop()
        print("== Loop protocol =="); [print(" ", l) for l in d["loop"]]
        print("== STP global ==");    [print(" ", l) for l in d["stp_global"]]
        print("== STP port ==");      [print(" ", l) for l in d["stp_port"]]

    def show_qos(self):
        d = self.sw.fetch_qos()
        print("== Port priority =="); [print(" ", l) for l in d["port_pri"]]
        print("== Scheduler ==");     [print(" ", l) for l in d["sched"]]

    def show_storm(self):
        [print(l) for l in self.sw.fetch_storm()]

    def show_igmp(self):
        d = self.sw.fetch_igmp()
        print(f"IGMP snooping: {'enabled' if d['enabled'] else 'disabled'}")
        for l in d["lines"]:
            print(" ", l)

    def show_trunk(self):
        [print(l) for l in self.sw.fetch_trunk()]

    def show_mirror(self):
        [print(l) for l in self.sw.fetch_mirror()]

    def show_isolation(self):
        [print(l) for l in self.sw.fetch_isolation()]

    def show_jumbo(self):
        print(f"Jumbo frame size: {self.sw.fetch_jumbo()} bytes")

    def show_eee(self):
        print(f"EEE: {'enabled' if self.sw.fetch_eee() else 'disabled'}")

    def show_bw(self):
        [print(l) for l in self.sw.fetch_bw()]

    def show_mac_constraint(self):
        [print(l) for l in self.sw.fetch_mac_constraint()]

    # ====================================================================== #
    #  NEW handlers — system
    # ====================================================================== #
    def _h_ip_address(self, a):
        if not self.sw.dry_run and not _confirm(
                f"Change management IP to {a['addr']}/{a['mask']} gw {a['gw']}? "
                "You may lose connectivity."):
            print("Aborted."); return
        self.sw.set_ip(a["addr"], a["mask"], a["gw"], 0)
        self.dirty = True

    def _h_ip_dhcp(self, a):
        if not self.sw.dry_run and not _confirm(
                "Switch management IP to DHCP? You may lose connectivity."):
            print("Aborted."); return
        cur = self.sw.fetch_ip() if not self.sw.dry_run else {"ip": "0.0.0.0", "netmask": "0.0.0.0", "gateway": "0.0.0.0"}
        self.sw.set_ip(cur["ip"], cur["netmask"], cur["gateway"], 1)
        self.dirty = True

    def _h_username(self, a):
        if not self.sw.dry_run and not _confirm(
                f"Change admin account to user '{a['user']}'?"):
            print("Aborted."); return
        self.sw.set_user(a["user"], a["pass"])
        self.dirty = True

    # ---- global features -------------------------------------------------- #
    def _h_jumbo(self, a):
        self.sw.set_jumbo(JUMBO_CODES[a["size"]])
        self.dirty = True

    def _h_igmp(self, enable):
        self.sw.set_igmp(enable)
        self.dirty = True

    def _h_eee(self, enable):
        self.sw.set_eee(enable)
        self.dirty = True

    def _h_loop_mode(self, a):
        # The switch has ONE global loop mechanism (func_type): Off / Loop Detection /
        # Loop Prevention / Spanning Tree. These three pick the non-STP options; use
        # 'spanning-tree mode {stp|rstp}' to select Spanning Tree instead.
        code = {"off": 0, "loop-detection": 1, "loop-prevention": 2}[a["mode"]]
        self.sw.set_loop(code)
        self.dirty = True

    def _h_loop_mode_off(self):
        self.sw.set_loop(0)
        self.dirty = True

    # ---- QoS scheduler ---------------------------------------------------- #
    def _h_sched_strict(self, a):
        q = a["queue"]
        if not 1 <= q <= 8:
            raise CmdError("% queue must be 1-8")
        self.sw.set_queue_weight([q], 0)
        self.dirty = True

    def _h_sched_wrr(self, a):
        q, w = a["queue"], a["weight"]
        if not 1 <= q <= 8:
            raise CmdError("% queue must be 1-8")
        if not 1 <= w <= 15:
            raise CmdError("% weight must be 1-15 (use 'strict' for strict priority)")
        self.sw.set_queue_weight([q], w)
        self.dirty = True

    # ---- STP global ------------------------------------------------------- #
    def _stp_globals(self):
        """Parse current STP global values to preserve untouched fields."""
        html = self.sw._get("/loop.cgi?page=stp_global")
        def field(name, default):
            m = re.search(rf'name="{name}"[^>]*value="(\d+)"', html)
            return int(m.group(1)) if m else default
        ver = 1 if re.search(r'name="version".*?value="1"[^>]*selected', html, re.S) else 0
        pm = re.search(r'name="priority".*?value="(\d+)"[^>]*selected', html, re.S)
        prio = int(pm.group(1)) if pm else 32768
        return {"version": ver, "priority": prio,
                "maxage": field("maxage", 20), "hello": field("hello", 2),
                "delay": field("delay", 15)}

    def _push_stp_global(self, **over):
        g = ({"version": 1, "priority": 32768, "maxage": 20, "hello": 2, "delay": 15}
             if self.sw.dry_run else self._stp_globals())
        g.update(over)
        self.sw.set_stp_global(g["version"], g["priority"], g["maxage"], g["hello"], g["delay"])
        self.dirty = True

    def _h_stp_mode(self, a):
        # Selecting an STP version also switches the switch's single global loop
        # mechanism to Spanning Tree (func_type=3), then sets STP vs RSTP.
        self.sw.set_loop(3)
        self._push_stp_global(version=1 if a["mode"] == "rstp" else 0)
        self.dirty = True

    def _h_stp_priority(self, a):
        if a["prio"] % 4096 != 0 or not 0 <= a["prio"] <= 61440:
            raise CmdError("% priority must be a multiple of 4096 (0-61440)")
        self._push_stp_global(priority=a["prio"])

    def _h_stp_timer(self, which, val):
        self._push_stp_global(**{which: val})

    # ---- link aggregation ------------------------------------------------- #
    @staticmethod
    def _lag_type(mode):
        """IOS channel mode -> switch trunk_type. on=static(0), active/passive=LACP(1)."""
        return 0 if mode == "on" else 1

    def _h_trunk(self, a):
        try:
            ports = parse_ifrange(a["ports"])
        except ValueError as e:
            raise CmdError(f"% {e}")
        if not 1 <= a["id"] <= 2:
            raise CmdError("% port-channel id must be 1 or 2 (this switch has 2 groups)")
        self.sw.set_trunk(a["id"], self._lag_type(a["mode"]), ports)
        self.dirty = True

    def _h_no_trunk(self, a):
        if not 1 <= a["id"] <= 2:
            raise CmdError("% port-channel id must be 1 or 2")
        self.sw.delete_trunk(a["id"])
        self.dirty = True

    def _h_channel_group(self, a):
        if not 1 <= a["id"] <= 2:
            raise CmdError("% port-channel id must be 1 or 2 (this switch has 2 groups)")
        self.sw.set_trunk(a["id"], self._lag_type(a["mode"]), self.ctx["ports"])
        self.dirty = True

    def _h_no_channel_group(self, a):
        # IOS removes the port from whatever group it's in. The switch deletes by group
        # and re-adds by full member list, so we can't surgically drop one port without
        # re-reading group membership, which this firmware does not expose cleanly.
        raise CmdError("% This switch cannot remove a single port from a group via the API; "
                       "use 'no port-channel <id>' to delete the whole group, then "
                       "recreate it with the remaining ports.")

    # ---- mirroring -------------------------------------------------------- #
    def _h_monitor(self, a):
        try:
            src = parse_ifrange(a["src"])
            dst = parse_ifrange(a["dst"])
        except ValueError as e:
            raise CmdError(f"% {e}")
        if len(dst) != 1:
            raise CmdError("% destination must be a single port")
        for sp in src:
            self.sw.set_mirror(MIRROR_DIR[a["dir"]], dst[0], sp)
        self.dirty = True

    def _h_no_monitor(self, a):
        self.sw.delete_mirror()
        self.dirty = True

    # ---- static MAC ------------------------------------------------------- #
    def _h_static_mac(self, a):
        try:
            ports = parse_ifrange(a["if"])
        except ValueError as e:
            raise CmdError(f"% {e}")
        if len(ports) != 1:
            raise CmdError("% static MAC needs exactly one interface")
        self.sw.add_static_mac(a["mac"], a["vid"], ports[0])
        self.dirty = True

    def _h_no_static_mac(self, a):
        self.sw.delete_static_mac(a["n"])
        self.dirty = True

    def _h_clear_mac(self, a):
        self.sw.clear_mac_table()
        print("Dynamic MAC entries cleared.")

    # ====================================================================== #
    #  NEW handlers — interface mode
    # ====================================================================== #
    def _port_state(self, p):
        """Current admin (bool enabled), speed code, flow (0/1) for a port."""
        cfg = ({} if self.sw.dry_run else self.sw.fetch_port_cfg().get(p, {}))
        state = cfg.get("state", "Enable")
        enabled = not state.lower().startswith("dis")
        sp = SPEED_CODES.get(re.sub(r"[^0-9gG]", "", cfg.get("cfg_speed", "auto")).lower(), 0)
        if cfg.get("cfg_speed", "").lower().startswith("auto"):
            sp = 0
        flow = 1 if cfg.get("flow_cfg", "Off").lower().startswith("on") else 0
        return enabled, sp, flow

    def _h_speed(self, a):
        code = {"auto": 0, "10": 2, "100": 4, "1000": 5, "2500": 6, "10g": 8}[a["speed"]]
        for p in self.ctx["ports"]:
            if p >= 5 and code in (1, 2, 3):
                raise CmdError(f"% 10G port {port_short(p)} does not support {a['speed']}M")
            en, _sp, fl = self._port_state(p)
            self.sw.set_port_cfg(p, en, code, fl)
        self.dirty = True

    def _h_duplex(self, a):
        # The switch has no independent duplex field — speed and duplex share one code.
        # We read the port's current speed and recombine. On dry-run we cannot read it,
        # so warn that the emitted code assumes the current rate is Auto.
        if self.sw.dry_run and a["duplex"] != "auto":
            print("  (dry-run: cannot read current speed; assuming Auto for the combined code)")
        for p in self.ctx["ports"]:
            en, sp, fl = self._port_state(p)
            d = a["duplex"]
            if d == "auto":
                code = 0
            else:
                # derive rate from current code
                rate = {1: "10", 2: "10", 3: "100", 4: "100", 5: "1000",
                        6: "2500", 8: "10g", 0: "auto"}.get(sp, "auto")
                if rate in ("1000", "2500", "10g") and d == "half":
                    raise CmdError(f"% {rate} does not support half duplex")
                code = {("10", "half"): 1, ("10", "full"): 2,
                        ("100", "half"): 3, ("100", "full"): 4,
                        ("1000", "full"): 5, ("2500", "full"): 6,
                        ("10g", "full"): 8, ("auto", "full"): 0,
                        ("auto", "half"): 0}[(rate, d)]
            self.sw.set_port_cfg(p, en, code, fl)
        self.dirty = True

    def _h_flow(self, a):
        fl = 1 if a["state"] == "on" else 0
        for p in self.ctx["ports"]:
            en, sp, _f = self._port_state(p)
            self.sw.set_port_cfg(p, en, sp, fl)
        self.dirty = True

    def _h_qos_priority(self, a):
        if not 1 <= a["prio"] <= 8:
            raise CmdError("% priority must be 1-8")
        self.sw.set_port_priority(self.ctx["ports"], a["prio"] - 1)
        self.dirty = True

    def _h_rate_limit(self, a):
        d = 0 if a["dir"] == "ingress" else 1
        self.sw.set_bw(self.ctx["ports"], d, 1, a["rate"])
        self.dirty = True

    def _h_no_rate_limit(self, a):
        d = 0 if a["dir"] == "ingress" else 1
        self.sw.set_bw(self.ctx["ports"], d, 0, 0)
        self.dirty = True

    def _h_storm_on(self, a):
        self.sw.set_storm(STORM_CODES[a["kind"]], self.ctx["ports"], 1, a["rate"])
        self.dirty = True

    def _h_storm_off(self, a):
        self.sw.set_storm(STORM_CODES[a["kind"]], self.ctx["ports"], 0, 0)
        self.dirty = True

    def _h_isolation(self, a):
        try:
            peers = parse_ifrange(a["peers"])
        except ValueError as e:
            raise CmdError(f"% {e}")
        self.sw.set_isolation(self.ctx["ports"], peers)
        self.dirty = True

    def _h_no_isolation(self, a):
        self.sw.set_isolation(self.ctx["ports"], set())
        self.dirty = True

    def _h_loop_port(self, enable):
        self.sw.set_loop_port(self.ctx["ports"], enable)
        self.dirty = True

    def _h_port_security(self, a):
        self.sw.set_mac_constraint(self.ctx["ports"], 1, a["n"])
        self.dirty = True

    def _h_no_port_security(self, a):
        self.sw.set_mac_constraint(self.ctx["ports"], 0, 0)
        self.dirty = True

    def _stp_port_state(self, ports):
        """Best-effort current (cost, priority, p2p, edge) for the first port,
        so editing one attribute preserves the others. Defaults on dry-run / parse miss."""
        cost, prio, p2p, edge = 0, 128, "auto", "false"
        if self.sw.dry_run:
            return cost, prio, p2p, edge
        # The stp_port status table prints per-port: Port|State|Role|CostCfg|CostAct|Prio|...
        cells = [c.strip() for c in re.sub(r"\s+", " ",
                 re.sub(r"<[^>]+>", "|", self.sw._get("/loop.cgi?page=stp_port"))).split("|")
                 if c.strip()]
        p0 = sorted(ports)[0]
        for i, c in enumerate(cells):
            m = re.fullmatch(r"Port (\d+)", c)
            if m and int(m.group(1)) == p0 and i + 9 < len(cells):
                try:
                    cost = int(cells[i + 3])
                except ValueError:
                    cost = 0
                try:
                    prio = int(cells[i + 5])
                except (ValueError, IndexError):
                    prio = 128
                break
        return cost, prio, p2p, edge

    def _h_stp_cost(self, a):
        for p in self.ctx["ports"]:
            _c, prio, p2p, edge = self._stp_port_state([p])
            self.sw.set_stp_port([p], a["cost"], prio, p2p, edge)
        self.dirty = True

    def _h_stp_pport(self, a):
        if a["prio"] % 16 != 0 or not 0 <= a["prio"] <= 240:
            raise CmdError("% port-priority must be a multiple of 16 (0-240)")
        for p in self.ctx["ports"]:
            cost, _pr, p2p, edge = self._stp_port_state([p])
            self.sw.set_stp_port([p], cost, a["prio"], p2p, edge)
        self.dirty = True

    def _h_stp_p2p(self, a):
        p2p = {"point-to-point": "true", "shared": "false", "auto": "auto"}[a["p2p"]]
        for p in self.ctx["ports"]:
            cost, prio, _p, edge = self._stp_port_state([p])
            self.sw.set_stp_port([p], cost, prio, p2p, edge)
        self.dirty = True

    def _h_stp_edge(self, enable):
        for p in self.ctx["ports"]:
            cost, prio, p2p, _e = self._stp_port_state([p])
            self.sw.set_stp_port([p], cost, prio, p2p, "true" if enable else "false")
        self.dirty = True

    # ====================================================================== #
    #  NEW handlers — tools (DANGEROUS gated)
    # ====================================================================== #
    def _h_backup(self, a):
        path = a["file"].strip()
        if self.sw.dry_run:
            print(f"  DRY-RUN GET /config_back.cgi?cmd=conf_backup -> {path}")
            return
        n = self.sw.backup_config(path)
        print(f"Wrote {n} bytes to {path}")

    def _h_restore(self, a):
        path = a["file"].strip()
        if not os.path.exists(path) and not self.sw.dry_run:
            raise CmdError(f"% file not found: {path}")
        if not self.sw.dry_run and not _confirm(
                f"Restore config from {path}? The switch will apply it and may reboot."):
            print("Aborted."); return
        self.sw.restore_config(path)
        print("Config restore submitted.")

    def _h_firmware(self, a):
        if not self.sw.dry_run and not _confirm(
                "Enter firmware-upgrade (bootloader) mode? This is DISRUPTIVE and the "
                "switch will go offline awaiting a firmware image."):
            print("Aborted."); return
        self.sw.firmware_upgrade()
        print("Firmware upgrade mode entered.")

    def _h_reload(self, a):
        if not self.sw.dry_run and not _confirm("Reboot the switch now?"):
            print("Aborted."); return
        self.sw.reboot()
        print("Reboot requested.")

    def _h_factory(self, a):
        if not self.sw.dry_run and not _confirm(
                "FACTORY RESET — erase ALL configuration and restore defaults?"):
            print("Aborted."); return
        self.sw.factory_reset()
        print("Factory reset requested.")


# --------------------------------------------------------------------------- #
# REPL / entrypoint
# --------------------------------------------------------------------------- #
def prompt(cli):
    name = cli.sw.name
    if cli.mode == "exec":
        return f"{name}# "
    if cli.mode == "config":
        return f"{name}(config)# "
    if cli.mode == "vlan":
        return f"{name}(config-vlan-{cli.ctx['vid']})# "
    if cli.mode == "iface":
        ports = cli.ctx["ports"]
        tag = port_short(ports[0]) if len(ports) == 1 else "range"
        return f"{name}(config-if-{tag})# "
    return f"{name}# "


def _raw_partial(text):
    """Last whitespace-delimited word of `text`, preserving case (for in-place edit)."""
    m = re.search(r"(\S*)$", text)
    return m.group(1) if m else ""


def read_line(cli, prompt_str):
    """A tiny IOS-style line editor: '?' shows context help instantly (no Enter),
    Tab completes, with basic editing + history. Returns the line, None on Ctrl-C,
    raises EOFError on Ctrl-D at an empty line."""
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    buf, cur = "", 0
    hist = cli.history
    hidx = len(hist)
    out = sys.stdout

    def redraw():
        out.write("\r\x1b[K" + prompt_str + buf)
        back = len(buf) - cur
        if back > 0:
            out.write(f"\x1b[{back}D")
        out.flush()

    def show_help():
        completed, partial = cli._split_partial(buf[:cur])
        out.write("?\r\n")
        rows = cli.help_rows(completed, partial) if completed is not None else []
        if rows:
            for tok, desc in rows:
                out.write(f"  {tok:<28}{desc}\r\n")
        else:
            out.write("  % No matching options\r\n")
        redraw()

    def do_tab():
        nonlocal buf, cur
        completed, partial = cli._split_partial(buf[:cur])
        if completed is None:
            return
        cands = cli.completions(completed, partial)
        raw = _raw_partial(buf[:cur])
        start = cur - len(raw)
        if not cands:
            return
        if len(cands) == 1:
            ins = cands[0] + " "
        else:
            common = os.path.commonprefix(cands)
            ins = common if len(common) > len(raw) else None
            if ins is None:  # ambiguous — list candidates IOS-style, keep the line
                out.write("\r\n  " + "  ".join(cands) + "\r\n")
                redraw()
                return
        buf = buf[:start] + ins + buf[cur:]
        cur = start + len(ins)
        redraw()

    try:
        tty.setraw(fd)
        redraw()
        while True:
            ch = os.read(fd, 1)
            if not ch:
                raise EOFError
            o = ch[0]
            if o in (13, 10):  # Enter
                out.write("\r\n")
                out.flush()
                if buf.strip():
                    hist.append(buf)
                return buf
            if o == 3:  # Ctrl-C
                out.write("^C\r\n")
                out.flush()
                return None
            if o == 4:  # Ctrl-D
                if not buf:
                    raise EOFError
                continue
            if o == 9:  # Tab
                do_tab()
                continue
            if ch == b"?":
                show_help()
                continue
            if o in (127, 8):  # Backspace
                if cur > 0:
                    buf = buf[: cur - 1] + buf[cur:]
                    cur -= 1
                    redraw()
                continue
            if o == 21:  # Ctrl-U clear line
                buf, cur = "", 0
                redraw()
                continue
            if o == 1:  # Ctrl-A home
                cur = 0
                redraw()
                continue
            if o == 5:  # Ctrl-E end
                cur = len(buf)
                redraw()
                continue
            if o == 27:  # escape sequence (arrows, home/end, delete)
                seq = os.read(fd, 2)
                if seq[:1] == b"[":
                    code = seq[1:2]
                    if code == b"D" and cur > 0:  # left
                        cur -= 1
                    elif code == b"C" and cur < len(buf):  # right
                        cur += 1
                    elif code in (b"A", b"B"):  # up / down history
                        if code == b"A" and hidx > 0:
                            hidx -= 1
                            buf = hist[hidx]
                        elif code == b"B":
                            hidx = min(hidx + 1, len(hist))
                            buf = hist[hidx] if hidx < len(hist) else ""
                        cur = len(buf)
                    elif code == b"H":
                        cur = 0
                    elif code == b"F":
                        cur = len(buf)
                    elif code == b"3":  # Delete (sends ESC[3~)
                        os.read(fd, 1)
                        if cur < len(buf):
                            buf = buf[:cur] + buf[cur + 1 :]
                    redraw()
                continue
            if 32 <= o < 127:  # printable
                buf = buf[:cur] + chr(o) + buf[cur:]
                cur += 1
                redraw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def repl(cli):
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    print(f"Connected to {cli.sw.name} ({cli.sw.host}).")
    while True:
        try:
            if interactive:
                line = read_line(cli, prompt(cli))
                if line is None:  # Ctrl-C — abandon this line, keep going
                    continue
            else:
                line = input(prompt(cli))
        except (EOFError, KeyboardInterrupt):
            print()
            break
        try:
            cli.run(line)
        except EOFError:
            break
        except SystemExit:
            raise
        except Exception as e:  # keep the shell alive on per-command errors
            print(f"% error: {e}")
    if cli.dirty:
        ans = input("Unsaved changes. Save to flash? [y/N] ")
        if ans.strip().lower().startswith("y"):
            cli.sw.save()
            print("Saved.")


def main():
    ap = argparse.ArgumentParser(description="IOS-like CLI for Horaco ZX-SWTG124AS switches")
    ap.add_argument("switch", nargs="?", help="switch name from inventory")
    ap.add_argument("-c", "--command", help="run one command (or ';'-separated) and exit")
    ap.add_argument("-n", "--dry-run", action="store_true", help="print POSTs instead of sending")
    ap.add_argument("-l", "--list", action="store_true", help="list inventory and exit")
    args = ap.parse_args()

    inv = load_inventory()
    if args.list or not args.switch:
        print("Available switches:")
        for name, cfg in inv.items():
            print(f"  {name:<12} {cfg['host']}")
        if args.list:
            return
        if not args.switch:
            return

    if args.switch not in inv:
        sys.exit(f"unknown switch '{args.switch}'. Known: {', '.join(inv)}")
    cfg = inv[args.switch]
    sw = Switch(
        args.switch, cfg["host"], cfg["user"], cfg["password"],
        ports=cfg.get("ports", 6), dry_run=args.dry_run,
    )
    cli = CLI(sw)

    if args.command:
        for c in args.command.split(";"):
            try:
                cli.run(c.strip())
            except EOFError:  # 'exit' from exec mode in a script
                break
        if cli.dirty and not args.dry_run:
            sw.save()
            print("Saved to flash.")
        return
    repl(cli)


if __name__ == "__main__":
    main()

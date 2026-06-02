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

# Physical port capability for the ZX-SWTG124AS: ports 1-4 are 2.5G, 5-6 are 10G(SFP+).
def desc_file():
    """Sidecar path for port descriptions — lives beside the inventory, not in the tool."""
    return os.path.join(data_dir(), "descriptions.yml")


def port_capability(port):
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

    def set_port_admin(self, port, enable):
        self._post(
            "/port.cgi",
            {
                "portid": str(port - 1),
                "state": "1" if enable else "0",
                "speed_duplex": "0",  # 0 = Auto
                "flow": "0",
                "cmd": "port",
            },
        )

    def save(self):
        self._post("/save.cgi", {"cmd": "save"})

    def version(self):
        txt = re.sub(r"<[^>]*>", "", self._get("/info.cgi"))
        return [l.strip() for l in txt.splitlines() if l.strip()]


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


# Token-spec constructors used in the grammar table below.
def _lit(w):
    return ("lit", w)


def _kw(name, *choices):
    return ("kw", name, tuple(choices))


def _int(name):
    return ("int", name)


def _rest(name, hint=""):
    return ("rest", name, hint)


def _iface(name="range"):
    return ("iface", name)


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
    "mode": "Set the port mode (access/trunk/exclusive-trunk)",
    "trunk": "Trunk: tagged members of all VLANs, accept all frames",
    "exclusive-trunk": "Trunk accepting tagged frames only (native retained but ignored)",
    "native": "Trunk native (untagged) VLAN",
    "allowed": "VLANs carried on the trunk",
    "add": "Add to the current set",
    "remove": "Remove from the current set",
}
ARG_HELP = {"vid": "<1-4094>  VLAN ID", "list": "<vlan-list>  e.g. 10,12,777", "text": "<line>"}


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
            (("iface",), [_lit("switchport"), _lit("mode"), _kw("mode", "access", "trunk", "exclusive-trunk")], self._h_mode, "port mode"),
            (("iface",), [_lit("switchport"), _lit("trunk"), _lit("native"), _lit("vlan"), _int("vid")], self._h_native, "trunk native VLAN"),
            (("iface",), [_lit("no"), _lit("switchport"), _lit("trunk"), _lit("native"), _lit("vlan")], self._h_no_native, "reset native to VLAN 1"),
            (("iface",), [_lit("switchport"), _lit("trunk"), _lit("allowed"), _lit("vlan"), _kw("op", "add", "remove"), _rest("list", "<vlan-list>")], self._h_allowed, "edit trunk VLANs"),
            (("iface",), [_lit("switchport"), _lit("trunk"), _lit("allowed"), _lit("vlan"), _rest("list", "<vlan-list>")], self._h_allowed, "set trunk VLANs"),
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
                rows += [(c, HELP.get(c, "")) for c in spec[2] if c.startswith(partial)]
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
            print(f"  {tok:<24}{desc}")

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
        for p in self.ctx["ports"]:
            self.sw.set_port_admin(p, False)
        self.dirty = True

    def _h_no_shutdown(self, a):
        for p in self.ctx["ports"]:
            self.sw.set_port_admin(p, True)
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
        out.write("?\n")
        rows = cli.help_rows(completed, partial) if completed is not None else []
        if rows:
            for tok, desc in rows:
                out.write(f"  {tok:<24}{desc}\n")
        else:
            out.write("  % No matching options\n")
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
                out.write("\n  " + "  ".join(cands) + "\n")
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
    print("IOS-like CLI: '?' shows options live, <Tab> completes, abbreviations work (e.g. 'sh vl', 'tw1').")
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

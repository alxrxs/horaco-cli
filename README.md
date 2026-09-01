# horaco-cli

A Cisco IOS-like CLI for **Horaco / ZX-SWTG124AS** web-managed switches — the cheap
AliExpress 2.5G/10G switches (Realtek-based, firmware ~V1.9) that expose **only** an
HTTP UI on port 80 (no SSH, telnet, or SNMP) and whose VLAN page is a clumsy per-VLAN
port-membership matrix plus a separate per-port PVID form.

`horaco.py` wraps that web UI so you can manage VLANs and trunk/access ports with
familiar commands — abbreviation, `?` help, and Tab completion included — instead of
clicking radio buttons.

```
switch# show interfaces
Interface  Cap   Link      Speed   Admin   PVID  Mode      Description     VLANs (u=untag,t=tag)
Tw1        2.5G  Link Up   2500M   Enable  10    ex-trunk  AP uplink       11t,42t,43t
Tw2        2.5G  Link Up   1000M   Enable  42    access    -               42u
Te1        10G   Link Up   10G     Enable  1     trunk     -               10t,11t,12t
```

Only dependency: `requests` (plus PyYAML). Python 3.8+.

## Setup

Create an inventory file named `switches.yml` (plaintext) or `switches.sops.yaml`
(SOPS-encrypted) — see [`switches.example.yml`](switches.example.yml). horaco.py looks
for it, in order: `$HORACO_SWITCHES`, the current directory, then this script's
directory and its parents (handy when the tool is a git submodule and the inventory
lives in the parent repo).

```yaml
switches:
  myswitch:
    host: 192.168.1.10
    user: admin
    password: changeme
    ports: 6
```

```bash
pip install requests pyyaml
python horaco.py --list            # list configured switches
python horaco.py myswitch          # interactive IOS-style shell
python horaco.py myswitch -c "show vlan"
python horaco.py myswitch -n -c "..."   # --dry-run: print the HTTP POSTs, change nothing
```

## IOS feel: abbreviation, `?`, Tab

Every keyword can be abbreviated to any unambiguous prefix, exactly like IOS —
`sh vl`, `conf t`, `sw tr allow vlan add 44`. Ambiguous prefixes are rejected with the
candidates (`sh v` → "Ambiguous: show vlan, show version").

- **`?`** instantly (no Enter) lists what's valid at the cursor, each with a
  description — `?`, `show ?`, `interface ?`, `switchport trunk ?`. Works mid-word
  (`sh?`); your line is preserved underneath.
- **`<Tab>`** completes the current word (unique → fills in + space; ambiguous →
  common prefix + lists candidates).
- Arrow keys, Home/End, Backspace/Delete, Ctrl-A/E/U and command history all work.

The interactive shell uses a small built-in raw-mode line editor — no extra
dependencies. When stdin isn't a TTY it falls back to plain line input, and `?`/Tab
still work via typing `show ?` + Enter.

Interfaces use Cisco media-type names: **`TwoGigabitEthernet1`-`4`** (the 2.5G ports,
abbrev `tw1`) and **`TenGigabitEthernet1`-`2`** (the 10G ports, abbrev `te1`). Ranges
work: `interface range tw1-4`, `interface te1,te2`.

## Commands

```
show vlan                         # VLAN table: name, untagged/tagged ports
show interfaces [status]          # link, negotiated speed, admin, PVID, mode, membership
show running-config               # reconstructed IOS-style config
show version                      # model / firmware / MAC / IP

configure terminal
    vlan 50                       # create / enter a VLAN
        name Guest
    no vlan 50                    # delete a VLAN
    interface TwoGigabitEthernet2     # abbrev "tw2"; 10G ports are TenGigabitEthernet1-2 ("te1")
        description Server rack    # local-only label (see below); 'no description' clears
        switchport access vlan 42         # untagged member + PVID 42, accept untagged
        switchport mode access            # strip tags -> untagged in the access/PVID VLAN
        switchport mode trunk             # allow ALL VLANs, accept all frames (keeps native/PVID)
        switchport mode exclusive-trunk   # allow ALL VLANs, accept TAGGED ONLY (native retained but ignored)
        switchport trunk native vlan 5    # native = untagged member + PVID (default is 1)
        no switchport trunk native vlan   # reset native back to VLAN 1
        switchport trunk allowed vlan 10,12,777   # narrow the trunk to this tagged set
        switchport trunk allowed vlan add 44      # add to tagged set (leaves native/PVID alone)
        switchport trunk allowed vlan remove 44
        shutdown / no shutdown
    end
write memory                      # persist to flash (the web UI's "Save")
```

### Full settings coverage

Every page in the switch's web UI is mapped to an IOS-flavoured command. Read-only
`show` commands are safe to run live; write commands honour `-n/--dry-run`, and the
truly disruptive ones prompt `[y/N]` before acting.

**Show / monitoring (read-only):**

```
show ip                           # mgmt IP / netmask / gateway / DHCP state
show mac address-table            # learned (dynamic) MAC table
show mac address-table static     # static MAC entries
show spanning-tree                # loop protocol + STP global + STP per-port
show qos                          # port-to-queue + queue scheduler
show storm-control                # per-port storm filters
show ip igmp snooping             # IGMP snooping state + entries
show etherchannel summary         # port-channel (link-aggregation) groups
show monitor                      # port mirroring session
show isolation                    # port isolation matrix
show jumbo-frame                  # configured jumbo MTU
show eee                          # Energy Efficient Ethernet state
show rate-limit                   # per-port bandwidth limits
show port-security                # per-port MAC-count limits
```

**Global config (`configure terminal`):**

```
jumbo-frame {1522|1536|1552|9216|16383}      # max frame size
ip igmp snooping / no ip igmp snooping       # IGMP snooping
energy-efficient-ethernet / no ...           # 802.3az EEE
# The switch has ONE global loop mechanism. Pick a non-STP mode here, or pick STP
# below — they are mutually exclusive (the same hardware "Loop function" selector).
loop-protection {off|loop-detection|loop-prevention}   # global loop mechanism
no loop-protection                           # mechanism off
spanning-tree mode {stp|rstp}                # set loop mechanism = Spanning Tree, + version
no spanning-tree                             # loop mechanism off
spanning-tree priority <0-61440 step 4096>   # bridge priority
spanning-tree max-age <sec> / hello-time <sec> / forward-time <sec>
qos scheduler strict <queue>                 # queue 1-8 -> strict priority
qos scheduler wrr <queue> <weight>           # queue 1-8, weight 1-15 (WRR)
port-channel <1-2> mode {on|active|passive} interface <iface-range>   # create/modify LAG
no port-channel <1-2>                        # delete the group
monitor session source <iface> {rx|tx|both} destination <iface>   # port mirror
no monitor session                           # delete mirror
mac address-table static <MAC> vlan <vid> interface <iface>       # static MAC
no mac address-table static <index>          # delete static MAC by row index
clear mac address-table dynamic              # flush learned MACs (exec/config)
```

**System (DANGEROUS — gated, dry-run encouraged):**

```
ip address <A.B.C.D> <mask> <gw>             # set management IP (confirm)
ip address dhcp                              # management IP via DHCP (confirm)
username <name> password <pass>              # change admin account (confirm)
```

**Interface config (`interface ...` then):**

```
speed {auto|10|100|1000|2500|10g}            # 10G ports reject 10/100M
duplex {auto|half|full}                      # half only valid for 10/100M
flowcontrol {on|off}                         # 802.3x flow control
qos priority <1-8>                           # port default priority queue
rate-limit {ingress|egress} <kbps> / no rate-limit {ingress|egress}
storm-control {broadcast|multicast|unknown-unicast|unknown-multicast} level <kbps>
no storm-control {broadcast|multicast|unknown-unicast|unknown-multicast}
isolation <iface-range> / no isolation       # block forwarding to listed ports
channel-group <1-2> mode {on|active|passive} # add this port to a port-channel
loop-protection / no loop-protection         # apply/exempt the global loop mechanism here
port-security maximum <n> / no port-security # per-port learned-MAC limit
spanning-tree cost <0-200000000>             # 0 = auto
spanning-tree port-priority <0-240 step 16>
spanning-tree link-type {point-to-point|shared|auto}
spanning-tree portfast / no spanning-tree portfast   # edge port
```

**Tools (exec; DANGEROUS ones gated):**

```
copy running-config backup <path>            # download config blob (safe GET)
copy backup running-config <path>            # restore config blob (confirm)
boot system                                  # enter firmware-upgrade mode (confirm)
reload                                        # reboot the switch (confirm)
erase startup-config  /  factory-reset       # restore factory defaults (confirm)
```

## How it maps to the hardware

- The switch stores VLAN membership **per VLAN** (each VLAN lists which ports are
  untagged / tagged / not-member). horaco.py reads the whole table, flips the ports you
  named, and re-POSTs only the VLANs that changed — so `switchport access vlan N` on one
  port doesn't disturb the others.
- **PVID** (port default VLAN) is a separate form; access/native commands set it,
  `allowed vlan add/remove` deliberately does **not** — preserving a common
  PVID→empty-VLAN "drop untagged" trunk trick.
- The three accepted-frame-types map to modes: `access` = Untag-only, `trunk` = All,
  `exclusive-trunk` = Tag-only.
- **Auth**: the login form sets cookie `admin=md5(user+password)`. The switch keeps a
  single server-side admin session, so horaco.py POSTs the login form once on connect
  before reading (the cookie alone returns blank data on a cold session).

## Port descriptions (local only)

The ZX-SWTG124AS firmware has **no port-name/description field** on the device, so
`description ...` is stored locally in a `descriptions.yml` sidecar (next to your
inventory), keyed by switch name → port. It shows in `show interfaces` /
`show running-config` but is never written to the switch. Port capability (2.5G vs 10G)
is fixed by hardware and shown in the `Cap` column.

## Caveats

- Only **one** admin session at a time — close the web UI before using the CLI, or
  reads may come back empty / contended.
- One-shot (`-c`) runs **auto-save to flash** when anything changed; the interactive
  shell prompts before saving on exit. Use `-n`/`--dry-run` to preview POSTs.
- These are dumb L2 switches: no LACP, no L3. Trunk = tagged member of the allowed
  VLANs; access = untagged member + matching PVID.

## Compatibility

Developed against **ZX-SWTG124AS** (6-port, firmware V1.9, Jan 2024). Other Horaco /
rebadged Realtek models that share the same `vlan.cgi` / `port.cgi` web UI are likely
to work; set `ports:` to your port count. PRs welcome.

## License

GNU Affero General Public License v3.0 — see [LICENSE](LICENSE).

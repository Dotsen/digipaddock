"""The security baseline: trusted addresses and the state of the servers.

The file baseline.json is changed only by the auto-enrolment of identities and by applying monitor_trust decisions
the owner signed in the cabinet (the tick — accept and trust, `kd-monitor check-now` — baseline); `kd-monitor
accept|baseline|trust` only propose them.
Pure logic, no SSH; the write is atomic, mode 0600 (the owner's addresses and a map of the servers' weak spots).
A broken file — nobody has a baseline, the file is left alone."""
import ipaddress
import json
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

REMOVED = "удалено"  # the value of a sec:* observation for an object that is gone (accept removes it from the baseline)
WEAK = ("@password", "@key ?")  # identities without a key: never enrolled in the baseline
MIN_PREFIX = {4: 16, 6: 112}  # a trusted subnet is no wider than /16 (v6 /112): 0.0.0.0/0 would silence every login


def stage_ip() -> str:
    """The factory host's address (home of the inventory); "" when unreadable — then nothing is the stage."""
    from sysadmin import inventory

    try:
        return inventory.home()[1]
    except inventory.InventoryError:
        return ""


def is_weak(identity: str) -> bool:
    return identity.endswith(WEAK)


def _net(text: str):
    return ipaddress.ip_network((text or "").strip(), strict=False)  # ValueError goes out: the CLI prints the reason


def _key(net) -> str:
    return str(net.network_address) if net.num_addresses == 1 else str(net)


def _empty() -> dict:
    return {"trusted": {stage_ip(): "stage"}, "servers": {}} if stage_ip() else {"trusted": {}, "servers": {}}


def _server() -> dict:
    return {"taken_at": None, "identities": {}, "ports": {}, "wgpeers": [], "snapshot": {}}


def _valid_server(srv: dict) -> bool:
    """The types of a server's fields: `identities: null` or a string in place of a dict would crash the tick."""
    def strmap(v):
        return isinstance(v, dict) and all(isinstance(k, str) and isinstance(x, str) for k, x in v.items())
    return (strmap(srv["identities"]) and strmap(srv["ports"]) and strmap(srv["snapshot"])
            and isinstance(srv["wgpeers"], list) and all(isinstance(p, str) for p in srv["wgpeers"])
            and (srv["taken_at"] is None or isinstance(srv["taken_at"], str)))


def _valid_trusted(key, label) -> bool:
    """A trusted address from the file: an address/subnet string no wider than MIN_PREFIX and a string label."""
    if not isinstance(key, str) or not isinstance(label, str):
        return False
    try:
        net = _net(key)
    except ValueError:
        return False
    return net.prefixlen >= MIN_PREFIX[net.version]


class Baseline:
    def __init__(self, data: dict, broken: bool = False):
        self.data = data
        self.broken = broken
        self.dirty = False
        self.records: List[Tuple[str, str, str]] = []

    @classmethod
    def empty(cls) -> "Baseline":
        return cls(_empty())

    @classmethod
    def load(cls, path) -> "Baseline":
        path = Path(path)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls.empty()
        except (OSError, UnicodeDecodeError) as exc:
            print(f"kd-monitor: эталон повреждён, проверки по эталону выключены: {path}: {exc}", file=sys.stderr)
            return cls(_empty(), broken=True)
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("servers"), dict) \
                or not all(isinstance(v, dict) for v in data["servers"].values()) \
                or not isinstance(data.setdefault("trusted", {}), dict):
            print(f"kd-monitor: эталон повреждён, проверки по эталону выключены: {path}", file=sys.stderr)
            return cls(_empty(), broken=True)
        for srv in data["servers"].values():
            for k, v in _server().items():
                srv.setdefault(k, v)
        if not all(_valid_server(srv) for srv in data["servers"].values()):
            print(f"kd-monitor: эталон повреждён (типы полей серверов), проверки по эталону выключены: {path}",
                  file=sys.stderr)
            return cls(_empty(), broken=True)
        bad = [k for k, v in data["trusted"].items() if not _valid_trusted(k, v)]
        for k in bad:  # broken or wider than /16 (/112) — not trusted: 0.0.0.0/0 would silence every login
            del data["trusted"][k]
        if bad:
            print(f"kd-monitor: эталон: отброшены доверенные адреса (битые или шире /{MIN_PREFIX[4]}): "
                  + ", ".join(str(k) for k in bad), file=sys.stderr)
        if stage_ip():
            data["trusted"][stage_ip()] = "stage"  # the file may have been edited by hand: the label stage is reserved
        return cls(data)

    def save(self, path) -> None:
        if self.broken:
            raise RuntimeError("эталон повреждён — не перезаписываю baseline.json")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(str(tmp), 0o600)
        os.replace(tmp, path)
        self.dirty = False

    # --- trusted addresses -------------------------------------------------------

    @staticmethod
    def trust_key(cidr: str) -> str:
        """The key of a trusted address; ValueError — not an address, wider than /16 (/112) or the factory host's IP."""
        net = _net(cidr)
        if net.prefixlen < MIN_PREFIX[net.version]:
            raise ValueError(f"слишком широкая подсеть {net}: не шире /{MIN_PREFIX[net.version]}")
        key = _key(net)
        if key == stage_ip():
            raise ValueError("IP stage доверен всегда и только для agent-ro и exec-ключа — метку менять нельзя")
        return key

    def trust(self, cidr: str, label: str) -> str:
        key = self.trust_key(cidr)
        self.data["trusted"][key] = label
        self.dirty = True
        return key

    def untrust(self, cidr: str) -> Optional[str]:
        """Remove a trusted address; → the normalized key, or None when it was not there."""
        key = _key(_net(cidr))
        if key == stage_ip():
            raise ValueError("IP stage доверен всегда")
        if key not in self.data["trusted"]:
            return None
        del self.data["trusted"][key]
        self.dirty = True
        return key

    def is_trusted(self, ip: str) -> Optional[str]:
        """The label of the narrowest trusted subnet that contains ip; None — not trusted or not an address."""
        try:
            addr = ipaddress.ip_address((ip or "").strip())
        except ValueError:
            return None
        addr = getattr(addr, "ipv4_mapped", None) or addr  # ::ffff:192.0.2.1 (sshd's dual stack) is 192.0.2.1
        best = None
        for key, label in self.data["trusted"].items():
            try:
                net = _net(key)
            except ValueError:
                continue
            if addr.version == net.version and addr in net and (best is None or net.prefixlen > best[0]):
                best = (net.prefixlen, label)
        return best[1] if best else None

    # --- servers -----------------------------------------------------------------

    def has(self, server: str) -> bool:
        return not self.broken and server in self.data["servers"]

    def server(self, server: str) -> dict:
        return self.data["servers"][server]

    @staticmethod
    def make_server(identities: dict, ports: dict, wgpeers, snapshot: dict, now) -> dict:
        return {"taken_at": now.isoformat(), "identities": dict(identities), "ports": dict(ports),
                "wgpeers": sorted(set(wgpeers)), "snapshot": dict(snapshot)}

    def set_server(self, server: str, data: dict) -> None:
        self.data["servers"][server] = data
        self.dirty = True

    def merge_server(self, server: str, data: dict) -> None:
        """A server's new baseline; the identities of the previous one (accept, auto-enrolment) are kept with their label:
        the owner's label from accept --label wins over a trusted address's label from the new capture."""
        old = self.data["servers"].get(server) or {}
        identities = dict(data["identities"])
        identities.update(old.get("identities") or {})
        merged = dict(data)
        merged["identities"] = identities
        self.set_server(server, merged)

    def label_elsewhere(self, identity: str, server: str) -> Optional[Tuple[str, int]]:
        """(label, number of servers) where the identity is already accepted, other than `server`; None — nowhere."""
        labels = [srv["identities"][identity] for alias, srv in self.data["servers"].items()
                  if alias != server and identity in srv["identities"]]
        if not labels:
            return None
        return next((lb for lb in labels if lb), ""), len(labels)

    def record_identity(self, server: str, identity: str, label: str) -> None:
        self.data["servers"][server]["identities"][identity] = label
        self.records.append((server, identity, label))
        self.dirty = True

    def check_accept(self, check: str, server: str, subject: str) -> None:
        """Whether the object may enter the baseline; ValueError — why not (the baseline does not change)."""
        if check not in ("logins", "ports", "wgpeers") and not check.startswith("sec:"):
            raise ValueError(f"accept не применим к проверке {check}: используйте kd-monitor ack")
        if check == "logins" and subject == "gap":
            raise ValueError("logins gap — переполнение журнала входов, а не личность: используйте kd-monitor ack")
        if self.broken:
            raise ValueError("эталон повреждён (baseline.json не читается) — accept невозможен, сначала разобраться с файлом")
        if not self.has(server):
            raise ValueError(f"у {server} нет эталона — сначала kd-monitor baseline {server}")

    def accept(self, check: str, server: str, subject: str, value: str, label: str = "") -> str:
        """Enter a problem's object into the baseline; → what was written. ValueError — a check without a baseline or a server without one."""
        self.check_accept(check, server, subject)
        srv = self.server(server)
        if check == "logins":
            if is_weak(subject):
                return (f"{subject} на {server}: вход без ключа в эталон не заносится, проблема закрыта — "
                        f"отключите PasswordAuthentication")
            other = self.label_elsewhere(subject, server)
            name = label or (other[0] if other else "")
            srv["identities"][subject] = name
            text = f"личность {subject} принята на {server}" + (f" как «{name}»" if name else "")
        elif check == "ports":
            srv["ports"][subject] = value
            text = f"слушатель {subject} ({value}) принят на {server}"
        elif check == "wgpeers":
            srv["wgpeers"] = sorted(set(srv["wgpeers"]) | {subject})
            text = f"пир {subject} принят на {server}"
        elif check == "sec:units" and value.startswith("{"):
            # a unit's incident — every changed object of the unit (seccheck.units_value), None is removed
            for item, new in json.loads(value).items():
                if new is None:
                    srv["snapshot"].pop("units:" + item, None)
                else:
                    srv["snapshot"]["units:" + item] = new
            text = f"юнит systemd {subject} принят на {server}"
        else:
            obj = check[4:] + ":" + subject
            if value == REMOVED:
                srv["snapshot"].pop(obj, None)
                text = f"{obj}: удаление принято на {server}"
            else:
                srv["snapshot"][obj] = value
                text = f"{obj} принят на {server}"
        self.dirty = True
        return text


def apply_records(fresh: "Baseline", records) -> None:
    """Carry the auto-enrolled identities over to a fresh copy of the baseline (the tick's may be stale against the CLI's)."""
    for server, identity, label in records:
        if fresh.has(server) and identity not in fresh.server(server)["identities"]:
            fresh.server(server)["identities"][identity] = label
            fresh.dirty = True

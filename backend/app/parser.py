"""URI / YAML 解析器：把 ss/vless/vmess/trojan/hysteria2/hy2 URI 转 mihomo clash 格式。"""
from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import yaml

log = logging.getLogger(__name__)


class ParseError(ValueError):
    pass


# ---------- helpers ----------

def _b64decode(text: str) -> bytes:
    """容忍 url-safe 和缺 padding 的 base64。"""
    s = text.strip().replace("-", "+").replace("_", "/")
    pad = (-len(s)) % 4
    return base64.b64decode(s + "=" * pad)


def _qs(qs: str) -> dict[str, str]:
    """parse_qs 简单封装，返回首值。"""
    return {k: v[0] for k, v in parse_qs(qs, keep_blank_values=True).items()}


# ---------- 各协议解析 ----------

def _parse_vless(uri: str) -> dict[str, Any]:
    """vless://uuid@host:port?params#name"""
    u = urlsplit(uri)
    if u.scheme.lower() != "vless":
        raise ParseError("not vless")
    uuid = u.username
    host = u.hostname
    port = u.port
    if not (uuid and host and port):
        raise ParseError("vless uri 缺 uuid/host/port")
    q = _qs(u.query)
    name = unquote(u.fragment) if u.fragment else f"{host}:{port}"
    node: dict[str, Any] = {
        "name": name,
        "type": "vless",
        "server": host,
        "port": port,
        "uuid": uuid,
        "udp": True,
    }
    flow = q.get("flow", "")
    if flow:
        node["flow"] = flow
    security = q.get("security", "")
    if security == "reality":
        node["security"] = "reality"
        reality = {}
        for k in ("public-key", "pbk"):
            if k in q:
                reality["public-key"] = q[k]
        if "short-id" in q:
            reality["short-id"] = q["short-id"]
        elif "sid" in q:
            reality["short-id"] = q["sid"]
        if "sni" in q:
            reality["server-name"] = q["sni"]
        if "fp" in q:
            reality["fingerprint"] = q["fp"]
        node["reality-opts"] = reality
        node["tls"] = True  # reality 必须开 tls
    elif security == "tls":
        node["security"] = tls = {}
        if "sni" in q:
            tls["server-name"] = q["sni"]
        if "fp" in q:
            tls["fingerprint"] = q["fp"]
        if "alpn" in q:
            tls["alpn"] = q["alpn"].split(",")
        node["tls"] = True
    net = q.get("type", "tcp")
    if net == "ws":
        ws = {}
        if "path" in q:
            ws["path"] = q["path"]
        if "host" in q:
            ws["headers"] = {"Host": q["host"]}
        node["network"] = "ws"
        node["ws-opts"] = ws
    elif net == "grpc":
        node["network"] = "grpc"
        if "serviceName" in q:
            node["grpc-opts"] = {"grpc-service-name": q["serviceName"]}
        elif "path" in q:
            node["grpc-opts"] = {"grpc-service-name": q["path"]}
    return node


def _parse_trojan(uri: str) -> dict[str, Any]:
    """trojan://password@host:port?params#name"""
    u = urlsplit(uri)
    if u.scheme.lower() != "trojan":
        raise ParseError("not trojan")
    pw = unquote(u.username or "")
    host = u.hostname
    port = u.port
    if not (pw and host and port):
        raise ParseError("trojan uri 缺 password/host/port")
    q = _qs(u.query)
    name = unquote(u.fragment) if u.fragment else f"{host}:{port}"
    node = {
        "name": name,
        "type": "trojan",
        "server": host,
        "port": port,
        "password": pw,
        "udp": True,
    }
    if "sni" in q or "allowInsecure" in q:
        node["tls"] = True
        if "sni" in q:
            node["sni"] = q["sni"]
        if "allowInsecure" in q and q["allowInsecure"] in ("1", "true"):
            node["skip-cert-verify"] = True
    return node


def _parse_ss(uri: str) -> dict[str, Any]:
    """ss://base64(method:password)@host:port#name  或  ss://method:password@host:port"""
    u = urlsplit(uri)
    if u.scheme.lower() != "ss":
        raise ParseError("not ss")
    name = unquote(u.fragment) if u.fragment else f"{u.hostname}:{u.port}"

    # 新格式: ss://base64(method:password)@host:port
    # 旧格式: ss://base64(method:password@host:port)
    if u.username:
        try:
            userinfo = _b64decode(u.username).decode("utf-8", errors="replace")
        except Exception:
            userinfo = unquote(u.username)
    else:
        # 整个 netloc 是 base64
        b = _b64decode(u.netloc.split("@", 1)[-1].split("?", 1)[0]).decode("utf-8", errors="replace")
        # 实际可能是 base64(method:password@host:port)
        if "@" not in b:
            raise ParseError("ss uri 格式无法识别")
        mp, hp = b.rsplit("@", 1)
        method, password = mp.split(":", 1)
        host, port_s = hp.rsplit(":", 1)
        return {
            "name": name,
            "type": "ss",
            "server": host,
            "port": int(port_s),
            "cipher": method,
            "password": password,
            "udp": True,
        }

    method, password = userinfo.split(":", 1)
    host = u.hostname
    port = u.port
    if not (host and port):
        raise ParseError("ss uri 缺 host/port")
    return {
        "name": name,
        "type": "ss",
        "server": host,
        "port": port,
        "cipher": method,
        "password": password,
        "udp": True,
    }


def _parse_vmess(uri: str) -> dict[str, Any]:
    """vmess://base64(json)"""
    if not uri.startswith("vmess://"):
        raise ParseError("not vmess")
    raw = uri[len("vmess://"):]
    try:
        data = json.loads(_b64decode(raw))
    except Exception as e:
        raise ParseError(f"vmess base64 解码失败: {e}")
    host = data.get("add")
    port = data.get("port")
    if not (host and port):
        raise ParseError("vmess json 缺 add/port")
    node: dict[str, Any] = {
        "name": data.get("ps") or f"{host}:{port}",
        "type": "vmess",
        "server": host,
        "port": int(port),
        "uuid": data.get("id"),
        "alterId": int(data.get("aid") or 0),
        "cipher": data.get("scy") or "auto",
        "udp": True,
    }
    if data.get("tls") == "tls":
        node["tls"] = True
        if data.get("sni"):
            node["server-name"] = data["sni"]
    net = data.get("net", "tcp")
    if net == "ws":
        ws = {"path": data.get("path", "/")}
        if data.get("host"):
            ws["headers"] = {"Host": data["host"]}
        node["network"] = "ws"
        node["ws-opts"] = ws
    elif net == "grpc":
        node["network"] = "grpc"
        if data.get("path"):
            node["grpc-opts"] = {"grpc-service-name": data["path"]}
    return node


def _parse_hy2(uri: str) -> dict[str, Any]:
    """hysteria2://password@host:port?params#name  或  hy2://..."""
    u = urlsplit(uri)
    scheme = u.scheme.lower()
    if scheme not in ("hysteria2", "hy2"):
        raise ParseError("not hy2")
    pw = unquote(u.username or "")
    host = u.hostname
    port = u.port
    if not (host and port):
        raise ParseError("hy2 uri 缺 host/port")
    q = _qs(u.query)
    name = unquote(u.fragment) if u.fragment else f"{host}:{port}"
    node = {
        "name": name,
        "type": "hysteria2",
        "server": host,
        "port": port,
        "password": pw,
    }
    if "sni" in q:
        node["sni"] = q["sni"]
    if "obfs" in q:
        node["obfs"] = q["obfs"]
        if "obfs-password" in q:
            node["obfs-password"] = q["obfs-password"]
    return node


_PARSERS = [
    ("vless", _parse_vless),
    ("trojan", _parse_trojan),
    ("ss", _parse_ss),
    ("vmess", _parse_vmess),
    ("hysteria2", _parse_hy2),
    ("hy2", _parse_hy2),
]


def parse_uri(uri: str) -> dict[str, Any]:
    """URI → mihomo clash 格式 dict。支持 vless/trojan/ss/vmess/hy2。"""
    uri = uri.strip()
    if not uri:
        raise ParseError("空内容")
    sch = uri.split("://", 1)[0]
    for prefix, fn in _PARSERS:
        if sch.lower() == prefix:
            node = fn(uri)
            # 强制 name 不为空
            node.setdefault("name", node.get("server", "node"))
            return node
    raise ParseError(f"不支持的协议: {sch}")


def parse_yaml(text: str) -> list[dict[str, Any]]:
    """YAML → node list。"""
    data = yaml.safe_load(text)
    if isinstance(data, dict):
        # 单节点 dict 或含 proxies 段
        if "proxies" in data and isinstance(data["proxies"], list):
            nodes = data["proxies"]
        else:
            nodes = [data]
    elif isinstance(data, list):
        nodes = data
    else:
        raise ParseError("yaml 必须是 proxies 列表或单个节点 dict")
    out = []
    for n in nodes:
        if not isinstance(n, dict) or "type" not in n:
            continue
        if "name" not in n:
            n["name"] = f"{n.get('server','node')}:{n.get('port','')}"
        out.append(n)
    if not out:
        raise ParseError("yaml 里没解析到有效节点")
    return out


def to_yaml(node: dict[str, Any]) -> str:
    return yaml.safe_dump(node, allow_unicode=True, sort_keys=False)


# ---------- 订阅内容批量解析 ----------

class SubscriptionFormat:
    URI_LIST = "uri_list"        # 每行一个 URI，可选 base64 编码
    CLASH_YAML = "clash_yaml"    # mihomo/clash 配置，含 proxies:
    SING_BOX_JSON = "sing_box"   # sing-box json
    UNKNOWN = "unknown"


def detect_subscription_format(text: str) -> str:
    raw = text.lstrip()
    if not raw:
        return SubscriptionFormat.UNKNOWN

    # 1. yaml 优先：用 PyYAML 解析，看顶层 dict 有没有 proxies / proxy-providers
    #    这是最可靠的方式，能识别完整 mihomo 配置（含 mixed-port、allow-lan 等顶级字段）
    try:
        data = yaml.safe_load(raw)
        if isinstance(data, dict):
            if "proxies" in data or "proxy-providers" in data:
                return SubscriptionFormat.CLASH_YAML
            # yaml 但没有 proxies 段（可能是 proxy-groups 之类）—— 不算订阅
            if "mixed-port" in data or "port" in data:
                # 完整 mihomo 配置但没 proxies 段，跳过让其他检测接管
                pass
    except Exception:
        pass

    # 2. 文本里搜 proxies: / proxy-providers: / - name:（不限制头部位置）
    if (re.search(r"^\s*proxies:\s*$", raw, re.M)
            or re.search(r"^\s*proxy-providers:\s*$", raw, re.M)
            or re.search(r"^\s*-\s*name:\s", raw, re.M)):
        return SubscriptionFormat.CLASH_YAML

    # 3. JSON
    if raw.startswith("{") or raw.startswith("["):
        try:
            json.loads(raw)
            return SubscriptionFormat.SING_BOX_JSON
        except Exception:
            pass

    lines = [l.strip() for l in raw.splitlines() if l.strip()]

    # 4. URI 列表 - 直接（首行是 scheme://）
    if lines and re.match(r"^(vless|vmess|trojan|ss|hysteria2|hy2|snell|tuic)://", lines[0], re.I):
        return SubscriptionFormat.URI_LIST

    # 5. 整段 base64：去换行后解码
    joined = "".join(lines)
    try:
        decoded = _b64decode(joined).decode("utf-8", errors="replace")
        first_after = decoded.lstrip().splitlines()[0] if decoded.strip() else ""
        if re.match(r"^(vless|vmess|trojan|ss|hysteria2|hy2|snell|tuic)://", first_after, re.I):
            return SubscriptionFormat.URI_LIST
        # 解码后是 yaml
        try:
            d = yaml.safe_load(decoded)
            if isinstance(d, dict) and ("proxies" in d or "proxy-providers" in d):
                return SubscriptionFormat.CLASH_YAML
        except Exception:
            pass
        if re.search(r"^\s*-\s*name:\s", decoded, re.M):
            return SubscriptionFormat.CLASH_YAML
    except Exception:
        pass

    # 6. 每行 base64（部分机场的做法）
    if 1 < len(lines) <= 5000:
        all_b64 = True
        decoded_sample = []
        for ln in lines[:5]:
            try:
                d = _b64decode(ln).decode("utf-8", errors="strict").strip()
                decoded_sample.append(d)
            except Exception:
                all_b64 = False
                break
        if all_b64 and decoded_sample and all(re.match(r"^[a-z]+://", d, re.I) for d in decoded_sample):
            return SubscriptionFormat.URI_LIST

    return SubscriptionFormat.UNKNOWN


def _try_b64_decode_lines(text: str) -> str:
    """尝试把整段文本 base64 解码；不行就返回原文本。"""
    s = text.strip()
    if not s:
        return s
    # 跳过明显不是 base64 的字符
    if re.search(r"^proxies:|^\s*-?\s*name:", s, re.M):
        return s
    # 先尝试"去换行后整体 base64"
    joined = "".join(l.strip() for l in s.splitlines() if l.strip())
    try:
        decoded = _b64decode(joined).decode("utf-8", errors="strict")
        if re.search(r"^(vless|vmess|trojan|ss|hysteria2|hy2|snell|tuic)://|proxies:", decoded, re.M):
            return decoded
    except Exception:
        pass
    # 再尝试"每行 base64"
    out_lines = []
    for line in s.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = _b64decode(line).decode("utf-8", errors="strict").strip()
            if d:
                out_lines.append(d)
        except Exception:
            return s  # 没法解码，返回原文本
    return "\n".join(out_lines) if out_lines else s


def parse_subscription(text: str, fmt: str | None = None) -> list[dict[str, Any]]:
    """整段订阅内容 → node list。

    fmt:
      - None / "auto" : 自动检测
      - "uri_list"    : URI 列表（可能 base64 编码整体 / 每行 base64）
      - "clash_yaml"  : mihomo yaml
    """
    if fmt is None or fmt == "auto":
        fmt = detect_subscription_format(text)

    raw = text.strip()

    if fmt == SubscriptionFormat.CLASH_YAML:
        return parse_yaml(raw)

    # URI 列表：先尝试 base64 解码
    raw = _try_b64_decode_lines(raw)

    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 跳过 yaml 行混在 base64 里的情况（不应该有，但兜底）
        if line.startswith("proxies:") or (":" in line and not re.match(r"^[a-z]+://", line, re.I)):
            continue
        try:
            node = parse_uri(line)
            out.append(node)
        except ParseError:
            # 单行解析失败不抛错，跳过
            continue

    if not out:
        # 兜底：哪怕 detect 没识别，强制尝试 yaml 解析
        try:
            data = yaml.safe_load(text)
            if isinstance(data, dict) and isinstance(data.get("proxies"), list):
                return parse_yaml(text)
        except Exception:
            pass
        raise ParseError(f"未能解析出任何节点（format={fmt}）")
    return out


# ---------- 国家/分组启发式 ----------

_COUNTRY_HINTS = [
    ("香港", "HK"), ("HK", "HK"), ("HongKong", "HK"), ("Hong Kong", "HK"), ("Hongkong", "HK"),
    ("台湾", "TW"), ("TW", "TW"), ("Taiwan", "TW"),
    ("日本", "JP"), ("JP", "JP"), ("Japan", "JP"),
    ("美国", "US"), ("US", "US"), ("USA", "US"), ("UnitedStates", "US"),
    ("新加坡", "SG"), ("SG", "SG"), ("Singapore", "SG"),
    ("韩国", "KR"), ("KR", "KR"), ("Korea", "KR"),
    ("英国", "GB"), ("UK", "GB"), ("GB", "GB"),
    ("德国", "DE"), ("DE", "DE"), ("Germany", "DE"),
    ("法国", "FR"), ("FR", "FR"), ("France", "FR"),
    ("加拿大", "CA"), ("CA", "CA"), ("Canada", "CA"),
    ("澳大利亚", "AU"), ("AU", "AU"), ("Australia", "AU"),
    ("俄罗斯", "RU"), ("RU", "RU"), ("Russia", "RU"),
    ("印度", "IN"), ("IN", "IN"), ("India", "IN"),
    ("巴西", "BR"), ("BR", "BR"), ("Brazil", "BR"),
    ("马来西亚", "MY"), ("MY", "MY"), ("Malaysia", "MY"),
    ("泰国", "TH"), ("TH", "TH"), ("Thailand", "TH"),
    ("越南", "VN"), ("VN", "VN"), ("Vietnam", "VN"),
    ("菲律宾", "PH"), ("PH", "PH"), ("Philippines", "PH"),
    ("印尼", "ID"), ("ID", "ID"), ("Indonesia", "ID"),
    ("土耳其", "TR"), ("TR", "TR"), ("Turkey", "TR"),
    ("阿联酋", "AE"), ("AE", "AE"), ("UAE", "AE"),
    ("中国", "CN"), ("CN", "CN"), ("China", "CN"),
]


def guess_country(name: str) -> str | None:
    if not name:
        return None
    for hint, code in _COUNTRY_HINTS:
        if hint in name:
            return code
    return None
"""Country / region and residential detection from node names. Standard library only.

Shared by the node catalogue (display) and the auto-lock profile (routing), so the page and
the router always agree on what "a US residential node" is.
"""

import re

# (code, label, pattern). Order matters: a Taiwan node whose name also carries a 🇨🇳 flag
# is still Taiwan, so specific place names are tried before bare flags and two-letter codes.
REGIONS = (
    ("TW", "台湾", r"台湾|台灣|Taiwan|🇹🇼|台北|新北|桃园|台中|高雄|hinet|seednet|(?<![A-Za-z])TW(?![A-Za-z])"),
    ("HK", "香港", r"香港|Hong\s*Kong|🇭🇰|HKT|HKBN|(?<![A-Za-z])HK(?![A-Za-z])"),
    ("MO", "澳门", r"澳门|澳門|Macau|Macao|🇲🇴"),
    ("JP", "日本", r"日本|Japan|🇯🇵|东京|東京|大阪|Tokyo|Osaka|(?<![A-Za-z])JP(?![A-Za-z])"),
    ("KR", "韩国", r"韩国|韓國|Korea|🇰🇷|首尔|首爾|Seoul|(?<![A-Za-z])KR(?![A-Za-z])"),
    ("SG", "新加坡", r"新加坡|狮城|獅城|Singapore|🇸🇬|(?<![A-Za-z])SG(?![A-Za-z])"),
    ("US", "美国", r"美国|美國|United\s*States|USA|🇺🇸|洛杉矶|硅谷|西雅图|纽约|圣何塞|(?<![A-Za-z])US(?![A-Za-z])"),
    ("GB", "英国", r"英国|英國|United\s*Kingdom|🇬🇧|伦敦|London|(?<![A-Za-z])UK(?![A-Za-z])"),
    ("DE", "德国", r"德国|德國|Germany|🇩🇪|法兰克福|Frankfurt"),
    ("CA", "加拿大", r"加拿大|Canada|🇨🇦"),
    ("AU", "澳大利亚", r"澳大利亚|澳洲|Australia|🇦🇺"),
    ("IN", "印度", r"印度|India|🇮🇳"),
    ("MY", "马来西亚", r"马来西亚|馬來西亞|Malaysia|🇲🇾"),
    ("TH", "泰国", r"泰国|泰國|Thailand|🇹🇭"),
    ("VN", "越南", r"越南|Vietnam|🇻🇳"),
    ("PH", "菲律宾", r"菲律宾|Philippines|🇵🇭"),
    ("TR", "土耳其", r"土耳其|Turkey|Türkiye|🇹🇷"),
    ("AR", "阿根廷", r"阿根廷|Argentina|🇦🇷"),
    ("RU", "俄罗斯", r"俄罗斯|俄羅斯|Russia|🇷🇺"),
    ("NL", "荷兰", r"荷兰|荷蘭|Netherlands|🇳🇱"),
    ("FR", "法国", r"法国|法國|France|🇫🇷"),
)
OTHER_REGION = ("OT", "其他")
_REGION_RE = tuple((code, label, re.compile(pattern, re.I)) for code, label, pattern in REGIONS)
REGION_ORDER = {code: index for index, (code, _label, _pattern) in enumerate(REGIONS)}
REGION_ORDER[OTHER_REGION[0]] = len(REGIONS)

RESIDENTIAL_RE = re.compile(
    r"家宽|家寬|住宅|residential|home\s*broadband|(?<![A-Za-z])ISP(?![A-Za-z])|hinet|seednet|HKT|HKBN", re.I)
# Subscription "nodes" that are really notices: expiry date, remaining traffic, website...
INFO_RE = re.compile(r"到期|剩余|剩餘|流量|套餐|官网|官網|公告|客服|重置|倍率说明|expire|traffic|official", re.I)
LABELS = dict((code, label) for code, label, _pattern in REGIONS)
LABELS[OTHER_REGION[0]] = OTHER_REGION[1]


def region_of(name):
    for code, label, pattern in _REGION_RE:
        if pattern.search(name):
            return code, label
    return OTHER_REGION


def label(code):
    return LABELS.get(code, code)


def is_residential(name):
    return bool(isinstance(name, str) and RESIDENTIAL_RE.search(name))


def is_notice(name):
    """Subscription entries that are notices (expiry, traffic left, website), not nodes."""
    return bool(isinstance(name, str) and INFO_RE.search(name))

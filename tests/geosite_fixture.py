"""A minimal GeoSite.dat (v2ray GeoSiteList protobuf) so the real core needs no download in tests."""


def _varint(number):
    out = bytearray()
    while True:
        byte = number & 0x7F
        number >>= 7
        out.append(byte | (0x80 if number else 0))
        if not number:
            return bytes(out)


def _field(number, payload):
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def geosite_dat(lists):
    """lists: {"openai": ["openai.com", ...]} -> bytes. Every domain is a suffix match (type 2)."""
    out = b""
    for code, domains in lists.items():
        entry = _field(1, code.upper().encode("utf-8"))
        for domain in domains:
            entry += _field(2, _varint(1 << 3 | 0) + _varint(2) + _field(2, domain.encode("utf-8")))
        out += _field(1, entry)
    return out

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公交线路站点数据抓取脚本（Python 后端版，零第三方依赖，仅用标准库）

基于高德地图公交线路站点数据抓取方案整理成的高德抓取核心库（由统一入口 busline 调用）。原文：
  · 高德地图版：https://zhuanlan.zhihu.com/p/622391866  （核心对象 AMap.LineSearch）

================================================================================
重要说明（务必先读）
================================================================================
高德有官方 Web 服务 API「公交线路查询」/v3/bus/linename，能完整拿到
线路基础信息 + 途经站点（含坐标）+ 线路轨迹，可直接跑通。
- 保存 API 返回的全部字段（线路 + 站点）。
- 高德坐标是 GCJ-02；脚本保留原始 GCJ-02，同时计算 WGS84（EPSG:4326），
  并新增转换后的经纬度字段（线路 polyline_wgs84；站点 lng_wgs84/lat_wgs84）。
- GeoPackage 线/点要素的几何与 SRS 均为 WGS84（EPSG:4326）。

关于配额（公交线路关键字 API：免费额度 5000 次/日，并发上限 30 次/秒）：
- 支持多 key 轮换：--key 用逗号分隔传入多个 Key，脚本轮流使用以分摊每日配额；
  某个 key 失效或日配额用尽时会自动停用并切换到下一个，全部失效才报错。
- 内置限速：默认 QPS 上限 28（可通过 --max-qps 调整，硬上限 30），
  每次请求间隔自动节流，确保并发量不突破高德 30 次/秒的上限。

================================================================================
使用方法
================================================================================
本模块是高德公交线路抓取的核心库，不直接作为命令行脚本运行。
请使用统一入口 busline 调用（busline amap ...），参数与配置统一由 busline 管理：

  抓取全城市所有线路（默认，数字穷举）：
    busline amap --city 深圳 --max-num 1000

  只抓指定线路：
    busline amap --city 深圳 --lines "1路,地铁1号线,M1路"

  多 key 轮换（分摊配额、单 key 失效自动切换）：
    busline amap --city 深圳 --amap-key "key1,key2,key3" --gpkg

  key 也可在 configs/keys.txt 的 amap= 行中设置（与检索参数分开存放），
  或用命令行 --amap-key 传入（多个用逗号分隔）。常用参数见 busline amap --help 或 README。

================================================================================
输出文件
================================================================================
  busline_amap.txt          线路全部字段 + 转换后坐标串（GCJ-02 原始 + WGS84）
  line_stop_amap.txt        线路-站点关联（站点全部字段 + GCJ-02 + WGS84 两列）
  bus_amap.gpkg             线路线要素 + 站点点要素（同一 GeoPackage 内图层：
                            busline_amap 为 LineString、busstop_amap 为 Point，WGS84/EPSG:4326）
  busline_amap.geojson      线路线要素（GeoJSON，WGS84）
================================================================================
"""

import csv
import json
import math
import os
import re
import sqlite3
import struct
import sys
import time
import unicodedata
import urllib.parse
import urllib.request


def _sanitize_filename(s, max_len=20):
    """把城市/关键字等字段安全化为文件名片段：替换非法字符、截断。

    替换 Windows 非法字符（\\ / : * ? " < > |）、空格、中英文逗号为下划线，
    合并连续下划线、去首尾下划线，再按 max_len 截断。
    """
    s = str(s)
    for ch in '\\/:*?"<>| ,，':
        s = s.replace(ch, '_')
    while '__' in s:
        s = s.replace('__', '_')
    s = s.strip('_')
    if len(s) > max_len:
        s = s[:max_len].rstrip('_')
    return s


def build_run_subdir(root, provider, city, mode, keywords):
    """按命名规则生成本次运行输出子目录：<时间戳>_<provider>_<城市>_<模式>_<关键字>。

    时间戳零填充放最前（字典序 = 时间序）；关键字多值用下划线连接、安全化后截断；
    目录已存在则追加 _2/_3 后缀避免覆盖。返回完整路径（不创建目录）。
    """
    ts = time.strftime("%Y%m%d_%H%M%S")
    city_s = _sanitize_filename(city, 16) or "city"
    kw_s = _sanitize_filename("_".join(keywords), 20) if keywords else ""
    parts = [ts, provider, city_s, mode]
    if kw_s:
        parts.append(kw_s)
    name = "_".join(parts)
    sub = os.path.join(root, name)
    n = 2
    cand = sub
    while os.path.exists(cand):
        cand = "%s_%d" % (sub, n)
        n += 1
    return cand


# ---------------------------------------------------------------------------
# 坐标转换（GCJ-02 / WGS84），标准公开算法
# ---------------------------------------------------------------------------
PI = math.pi
A = 6378245.0                    # 克拉索夫斯基椭球长半轴
EE = 0.00669342162296594323      # 第一偏心率平方

AMAP_API = "https://restapi.amap.com/v3/bus/linename"

# GeoPackage 目标坐标系：WGS84（世界大地坐标系 1984，EPSG:4326）。
# 高德返回 GCJ-02，经 gcj02_to_wgs84 转换为 WGS84。
WGS84_SRS_ID = 4326
WGS84_WKT = ('GEOGCS["WGS 84",'
             'DATUM["WGS_1984",'
             'SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],'
             'AUTHORITY["EPSG","6326"]],'
             'PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
             'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
             'AUTHORITY["EPSG","4326"]]')


def _out_of_china(lng, lat):
    return not (73.66 < lng < 135.05 and 3.86 < lat < 53.55)


def _transform_lat(x, y):
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * PI) + 40.0 * math.sin(y / 3.0 * PI)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * PI) + 320.0 * math.sin(y * PI / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x, y):
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * PI) + 40.0 * math.sin(x / 3.0 * PI)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * PI) + 300.0 * math.sin(x / 30.0 * PI)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lng, lat):
    """WGS84 -> GCJ-02（火星坐标）"""
    if _out_of_china(lng, lat):
        return lng, lat
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * PI
    magic = math.sin(radlat)
    magic = 1 - EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((A * (1 - EE)) / (magic * sqrtmagic) * PI)
    dlng = (dlng * 180.0) / (A / sqrtmagic * math.cos(radlat) * PI)
    return lng + dlng, lat + dlat


def gcj02_to_wgs84(lng, lat):
    """GCJ-02 -> WGS84（迭代一次近似，米级精度，够用）"""
    if _out_of_china(lng, lat):
        return lng, lat
    glng, glat = wgs84_to_gcj02(lng, lat)
    return lng * 2 - glng, lat * 2 - glat


# ---------------------------------------------------------------------------
# HTTP 工具（纯标准库）
# ---------------------------------------------------------------------------
def http_get_json(url, params, timeout=15, retries=3):
    """GET 请求并解析 JSON，带简单重试。"""
    qs = urllib.parse.urlencode(params)
    full = url + "?" + qs
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(full, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw)
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"请求失败：{full}\n{last_err}")


# ---------------------------------------------------------------------------
# 高德 API 错误码分类（用于多 key 轮换判定）
# 公交线路关键字 API：免费额度 5000 次/日，并发上限 30 次/秒。
# ---------------------------------------------------------------------------
# key 本身无效/不匹配等永久性错误 → 停用该 key
AMAP_ERR_KEY_INVALID = {
    "10001", "10002", "10009", "10010", "10011", "10012", "10013", "10014",
}
# 日配额用尽 → 该 key 当天不可再用，停用
AMAP_ERR_QUOTA_EXHAUSTED = {"10003"}
# 访问过于频繁（QPS 超限）→ 临时性，等待后重试，不停用 key
AMAP_ERR_QPS_LIMIT = {"10004"}


class AmapClient:
    """高德 API 客户端：多 key 轮换 + 限速。

    - 多 key 轮换（round-robin）：请求轮流使用各 key 以分摊每日配额；
      当某个 key 失效（10001 等）或日配额用尽（10003）时自动停用该 key，
      后续请求自动切换到下一个可用 key；仅当所有 key 都不可用时才抛错。
    - 限速：每次请求前保证与上次请求间隔 >= 1/max_qps 秒，
      确保并发量（QPS）不突破上限（默认 28，硬上限 30）。
    """

    def __init__(self, keys, max_qps=28.0):
        # 去空、去重（保持传入顺序）
        self._keys = list(dict.fromkeys(k for k in keys if k))
        if not self._keys:
            raise ValueError("未提供任何高德 Key")
        self._max_qps = min(max(float(max_qps), 1.0), 30.0)  # 钳制在 [1, 30]
        self._min_interval = 1.0 / self._max_qps
        self._idx = 0           # 下一个待用 key 的下标（轮换指针）
        self._dead = set()      # 已停用的 key（失效 / 配额用尽）
        self._last_req = 0.0

    @property
    def key_count(self):
        return len(self._keys)

    @property
    def alive_count(self):
        return len([k for k in self._keys if k not in self._dead])

    @property
    def qps_cap(self):
        return self._max_qps

    def _alive(self):
        return [k for k in self._keys if k not in self._dead]

    def _throttle(self):
        """串行限速：确保距上次请求 >= 1/max_qps 秒，使 QPS 不突破上限。"""
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_req)
        if wait > 0:
            time.sleep(wait)
        self._last_req = time.monotonic()

    def _drop_key(self, key, reason):
        self._dead.add(key)
        print(f"  ⚠️ key 尾号 {key[-4:]} 已停用（{reason}），"
              f"剩余可用 {self.alive_count}/{self.key_count} 个")

    def get_json(self, city, keyword, page_size, page):
        """请求一次公交线路查询，失败时自动轮换 key 重试；返回保证 status=='1' 的 dict。"""
        alive = self._alive()
        if not alive:
            raise RuntimeError("所有高德 Key 均已失效或达日配额上限，无法继续抓取。")

        last_info = ""
        # 最多遍历存活 key 两遍（应对 QPS 临时限流这类可重试错误）
        attempts = max(len(alive) * 2, 2)
        for _ in range(attempts):
            alive = self._alive()
            if not alive:
                break
            key = alive[self._idx % len(alive)]
            self._idx = (self._idx + 1) % len(alive)  # 轮换指针前移
            params = {
                "key": key, "city": city, "keywords": keyword,
                "extensions": "all", "offset": page_size,
                "page": page, "output": "json",
            }
            self._throttle()
            try:
                data = http_get_json(AMAP_API, params, retries=2)
            except RuntimeError as e:
                # 网络异常：不一定是 key 问题，换下一个 key 再试
                last_info = f"网络异常：{e}"
                continue
            status = str(data.get("status", ""))
            if status == "1":
                return data
            infocode = str(data.get("infocode", ""))
            info = data.get("info", "")
            if infocode in AMAP_ERR_KEY_INVALID:
                self._drop_key(key, f"key 无效 {infocode} {info}")
                continue
            if infocode in AMAP_ERR_QUOTA_EXHAUSTED:
                self._drop_key(key, f"日配额用尽 {infocode}")
                continue
            if infocode in AMAP_ERR_QPS_LIMIT:
                last_info = f"QPS 超限 {infocode}，自动等待后继续"
                time.sleep(1.0)
                continue
            last_info = f"status={status} info={info} infocode={infocode}"
        raise RuntimeError(
            f"高德 API 请求失败（城市={city} 关键字={keyword}）：{last_info}")


# ---------------------------------------------------------------------------
# 高德抓取（官方 Web API，完整）
# ---------------------------------------------------------------------------
def amap_fetch_buslines(client, city, keyword, page_size=20, delay=0.0):
    """按关键字检索高德公交线路，翻页拿全量匹配线路（经 client 多 key 轮换 + 限速）。"""
    results = []
    page = 1
    while True:
        data = client.get_json(city, keyword, page_size, page)
        buslines = data.get("buslines") or []
        if not buslines:
            break
        results.extend(buslines)
        count = int(data.get("count", 0) or 0)
        if count and page * page_size >= count:
            break
        page += 1
        if delay:
            time.sleep(delay)
    return results


def _as_text(v):
    """把高德返回的字段值安全规范化为字符串。

    高德 Web API 对同一字段在不同线路上类型可能不一致（例如 company 一般是
    字符串，但个别线路返回空列表 []；start_time/end_time 也可能是 []），
    直接塞进 sqlite/CSV 会因 list 类型报错。这里统一兜底。
    """
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        return ",".join(str(x) for x in v) if v else ""
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _decode_timedesc(raw):
    """解码 timedesc 字段：URL 解码 + JSON 解析，重新序列化为可读 JSON 字符串。

    高德返回的 timedesc 是「URL 编码的 JSON 字符串」（如 {%22rule_group%22:...}），
    直接读不可读。这里做两步：urllib.parse.unquote 解码 → json.loads 解析，
    再 json.dumps(ensure_ascii=False) 序列化为含中文的可读紧凑 JSON。
    解码/解析失败（如空串、非 JSON）时原样返回，不抛异常。
    """
    raw = _as_text(raw)
    if not raw:
        return ""
    try:
        decoded = urllib.parse.unquote(raw)
        obj = json.loads(decoded)
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except (ValueError, json.JSONDecodeError):
        return raw


def _norm_line_key(name):
    """线路去重键规范化：统一全角/半角、去空白、英文小写。

    数字穷举时同一条线路可能被多个关键字重复命中，API 两次返回的 name
    可能有细微差异（全角/半角括号、全角空格、多余空白、大小写等）。这里把
    name 规范化为可比较的 key，保证去重时能识别为「同一条线路」。
    NFKC 归一化把全角字符（（）　，：等）转为半角，再去掉所有空白、英文小写。
    """
    n = unicodedata.normalize("NFKC", name or "")
    n = re.sub(r"\s+", "", n)
    return n.casefold()


def _dedup_lines(busline_rows, linestop_rows):
    """按规范化后的 route_name 去重（保留首个命中），并同步剔除重复站点。

    采集侧已有一道去重（seen），这里是输出前的兜底：无论采集侧是否漏网，
    写文件前再按规范化 route_name 去重一次，确保输出不重复。
    线路：按规范化 route_name 去重。
    站点：按「规范化 route_name + station_index + station_name」复合键独立去重
    ——重复线路的重复站点随之被剔除，且不会误伤不同线路/上下行的同名站点。
    """
    seen = set()
    kept = []
    removed = 0
    for l in busline_rows:
        k = _norm_line_key(l.get("route_name", ""))
        if k in seen:
            removed += 1
            continue
        seen.add(k)
        kept.append(l)
    if removed:
        print(f"  [去重] 输出前剔除重复线路 {removed} 条")

    seen_stop = set()
    kept_stops = []
    removed_stops = 0
    for s in linestop_rows:
        sk = (_norm_line_key(s.get("route_name", "")),
              str(s.get("station_index", "")).strip(),
              _norm_line_key(s.get("station_name", "")))
        if sk in seen_stop:
            removed_stops += 1
            continue
        seen_stop.add(sk)
        kept_stops.append(s)
    if removed_stops:
        print(f"  [去重] 输出前剔除重复站点 {removed_stops} 条")
    return kept, kept_stops


def amap_parse(busline):
    """把高德 busline 字典解析成 (线路记录, 站点记录列表)，保留 API 全部字段。

    线路记录：原样保留 busline 除 busstops 外的所有字段（值经 _as_text 规范化），
    另加派生字段 line_name / route_id / route_name / coord_arr / coord_arr_wgs84。
    站点记录：保留 id/location/name/sequence 原始字段 + 拆分后的 lng/lat（GCJ-02）
    与转换后的 lng_wgs84/lat_wgs84 占位列。
    """
    line_rec = {k: _as_text(v) for k, v in busline.items() if k != "busstops"}

    name = line_rec.get("name", "")
    line_rec["line_name"] = name.split("(")[0].strip()
    line_rec["route_id"] = line_rec.get("id", "")
    line_rec["route_name"] = name
    # route_id == id、route_name == name，剔除冗余的 id / name 字段，仅保留派生字段
    line_rec.pop("id", None)
    line_rec.pop("name", None)

    # timedesc 解码：URL 编码 JSON → 可读 JSON 字符串
    line_rec["timedesc"] = _decode_timedesc(line_rec.get("timedesc", ""))

    # 轨迹坐标数组（GCJ-02 原始）；coord_arr_wgs84 为占位，由 crawl_amap 填充
    polyline = line_rec.get("polyline", "")
    coord_arr = []
    if polyline:
        for pt in polyline.split(";"):
            pt = pt.strip()
            if not pt:
                continue
            try:
                lng_s, lat_s = pt.split(",")
                coord_arr.append((float(lng_s), float(lat_s)))
            except ValueError:
                continue
    line_rec["coord_arr"] = coord_arr
    line_rec["coord_arr_wgs84"] = []

    # 站点：Web API 字段名为 busstops（兼容 via_stops）
    stops = busline.get("busstops") or busline.get("via_stops") or []
    stop_records = []
    for st in stops:
        loc = _as_text(st.get("location"))
        lng_s = lat_s = ""
        if loc:
            parts = loc.split(",")
            if len(parts) == 2:
                lng_s, lat_s = parts[0].strip(), parts[1].strip()
        stop_records.append({
            "station_id": _as_text(st.get("id")),
            "station_name": _as_text(st.get("name")),
            "station_index": _as_text(st.get("sequence")),
            "location": loc,
            "lng": lng_s,              # GCJ-02 原始经度
            "lat": lat_s,              # GCJ-02 原始纬度
            "lng_wgs84": "",        # 转换后经度（占位，crawl_amap 填充）
            "lat_wgs84": "",        # 转换后纬度（占位，crawl_amap 填充）
        })

    return line_rec, stop_records


def build_num_keywords(max_num, prefixes=None, suffixes=None):
    """构造数字穷举关键字：前缀 × 数字 × 后缀。

    prefixes / suffixes 为字符串列表（可能含空串）。两者都为空时退化为纯数字
    1~max_num（保持向后兼容）；否则生成 p + str(i) + s 的笛卡尔积，例如
    prefixes=["浦东"]、suffixes=["路"]、max_num=30 → 浦东1路~浦东30路。
    """
    prefixes = [p for p in (prefixes or []) if p]
    suffixes = [s for s in (suffixes or []) if s]
    if not prefixes and not suffixes:
        return [str(i) for i in range(1, max_num + 1)]
    prefixes = prefixes or [""]
    suffixes = suffixes or [""]
    return [p + str(i) + s
            for p in prefixes for i in range(1, max_num + 1) for s in suffixes]


def crawl_amap(keys, city, max_num, extra_keywords, outdir,
               make_gpkg, make_geojson, delay,
               specified_lines=None, max_qps=28.0,
               num_prefixes=None, num_suffixes=None, exact=False):
    """高德完整抓取：线路 + 站点 + 轨迹。

    keys：高德 Key 列表（多 key 轮换，某个 key 失效/配额用尽自动切换）。
    specified_lines：指定线路名列表。非空时只抓这些线路（不做数字穷举）；
    为 None 或空列表时，按数字关键字穷举 + 特殊关键字补充抓取全城市。
    num_prefixes / num_suffixes：数字穷举模式下的前缀/后缀列表，用于生成
    「前缀+数字+后缀」关键字（如 浦东 + 1~N + 路）；均为空时退化为纯数字穷举。
    max_qps：每秒最大请求数（并发上限），默认 28（硬上限 30）。
    exact：精确检索开关，True 时只保留短名与关键字严格全等的线路（不做补路/补线回退）。
    坐标：保留高德原始 GCJ-02，同时计算 WGS84（GCJ-02→WGS84）。
    """
    client = AmapClient(keys, max_qps=max_qps)
    print(f"[高德] Key {client.key_count} 个（轮换使用），"
          f"限速 QPS ≤ {client.qps_cap:g} 次/秒（并发不突破 30）")

    if specified_lines:
        keywords = list(specified_lines)
        print(f"[高德] 城市={city} 指定线路 {len(keywords)} 条：{', '.join(keywords)}")
    else:
        num_kws = build_num_keywords(max_num, num_prefixes, num_suffixes)
        keywords = num_kws + list(extra_keywords)
        span = f"{num_kws[0]}~{num_kws[-1]}" if (num_prefixes or num_suffixes) else f"1~{max_num}"
        print(f"[高德] 城市={city} 数字关键字 {span} + 特殊关键字 {len(extra_keywords)} 个，"
              f"共 {len(keywords)} 个关键字")

    busline_rows = []   # 线路记录
    linestop_rows = []  # 站点记录
    seen = set()        # 去重（按规范化 route_name）

    for idx, kw in enumerate(keywords, 1):
        try:
            lines = amap_fetch_buslines(client, city, kw, delay=delay)
        except RuntimeError as e:
            print(f"  [{idx}/{len(keywords)}] 关键字「{kw}」请求失败：{e}")
            if client.alive_count == 0:
                print("  ⚠️ 所有 Key 均已不可用，提前停止抓取。")
                break
            continue

        for bl in lines:
            name = bl.get("name", "")
            if exact:
                # 精确检索：只保留短名与当前关键字严格全等的线路（不做补路/补线回退）
                short_name = name.split("(")[0].strip()
                if short_name != kw:
                    continue
            key = _norm_line_key(name)
            if not name or key in seen:
                continue
            seen.add(key)
            line_rec, stop_recs = amap_parse(bl)

            # GCJ-02 → WGS84
            line_rec["coord_arr_wgs84"] = [
                gcj02_to_wgs84(*c) for c in line_rec["coord_arr"]]
            line_rec["polyline_wgs84"] = ";".join(
                f"{lng},{lat}" for lng, lat in line_rec["coord_arr_wgs84"])
            for sr in stop_recs:
                if sr["lng"] and sr["lat"]:
                    wlng, wlat = gcj02_to_wgs84(float(sr["lng"]), float(sr["lat"]))
                    sr["lng_wgs84"] = f"{wlng:.6f}"
                    sr["lat_wgs84"] = f"{wlat:.6f}"

            busline_rows.append(line_rec)
            for sr in stop_recs:
                linestop_rows.append({
                    "line_name": line_rec["line_name"],
                    "route_id": line_rec["route_id"],
                    "route_name": line_rec["route_name"],
                    **sr,
                })
        print(f"  [{idx}/{len(keywords)}] 关键字「{kw}」→ 已累计线路 {len(busline_rows)} 条")
        if delay:
            time.sleep(delay)

    if not busline_rows:
        print("⚠️  未抓到任何线路数据，输出文件为空。请逐项排查：")
        print("   1. 高德 Key 是否有效、是否已达日配额上限（上方若打印了「API 返回错误」请对照 info/infocode）")
        print("   2. 网络是否可达 restapi.amap.com（是否被代理/防火墙拦截）")
        print("   3. --city / --lines 关键字是否正确（指定线路时请用线路名，如「406路」而非纯数字）")
    # 输出前统一去重兜底（按规范化 route_name），并同步剔除重复线路的站点
    busline_rows, linestop_rows = _dedup_lines(busline_rows, linestop_rows)
    _write_amap_txt(outdir, busline_rows, linestop_rows)
    if make_gpkg:
        _write_gpkg(outdir, "amap", busline_rows, linestop_rows)
    if make_geojson:
        _write_line_geojson(outdir, "amap", busline_rows)
    print(f"[高德] 完成：线路 {len(busline_rows)} 条，站点记录 {len(linestop_rows)} 条")


# 输出字段顺序（txt 与 gpkg 共用）
LINE_OUT_FIELDS = [
    "line_name", "route_id", "route_name", "type",
    "polyline", "polyline_wgs84", "citycode", "start_stop", "end_stop",
    "start_time", "end_time", "uicolor", "timedesc", "loop", "status",
    "direc", "company", "distance", "basic_price", "total_price", "bounds",
]
STOP_OUT_FIELDS = [
    "line_name", "route_id", "route_name", "station_id", "station_name",
    "station_index", "location", "lng", "lat", "lng_wgs84", "lat_wgs84",
]


def _write_amap_txt(outdir, busline_rows, linestop_rows):
    """写出高德版两个 txt（保留 API 全部字段 + 转换后坐标）。

    用 csv 模块写入：解码后的 timedesc 含逗号/引号，自动加引号转义，
    避免简单逗号拼接导致的列错位。
    """
    busline_path = os.path.join(outdir, "busline_amap.txt")
    linestop_path = os.path.join(outdir, "line_stop_amap.txt")
    with open(busline_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(LINE_OUT_FIELDS)
        for r in busline_rows:
            w.writerow([str(r.get(k, "")) for k in LINE_OUT_FIELDS])
    with open(linestop_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(STOP_OUT_FIELDS)
        for r in linestop_rows:
            w.writerow([str(r.get(k, "")) for k in STOP_OUT_FIELDS])
    print(f"[高德] 已写出：{busline_path}")
    print(f"[高德] 已写出：{linestop_path}")


# ---------------------------------------------------------------------------
# GeoJSON 导出（线要素）
# ---------------------------------------------------------------------------
def _write_line_geojson(outdir, provider, busline_rows):
    features = []
    for r in busline_rows:
        coords = [[lng, lat] for lng, lat in (r.get("coord_arr_wgs84") or [])]
        if len(coords) < 2:
            continue
        props = {k: v for k, v in r.items()
                 if k not in ("coord_arr", "coord_arr_wgs84")}
        features.append({
            "type": "Feature",
            "properties": props,
            "geometry": {"type": "LineString", "coordinates": coords},
        })
    geojson = {
        "type": "FeatureCollection",
        "crs": {"type": "name",
                "properties": {"name": "urn:ogc:def:crs:EPSG::4326"}},
        "features": features,
    }
    path = os.path.join(outdir, f"busline_{provider}.geojson")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(geojson, f, ensure_ascii=False)
    print(f"[{provider}] 已写出：{path}（{len(features)} 个线要素，WGS84）")


# ---------------------------------------------------------------------------
# GeoPackage 导出（线/点要素，纯标准库 sqlite3 + 手写 GPB/WKB 实现）
# ---------------------------------------------------------------------------
def _gpkg_linestring_blob(coords, srs_id=WGS84_SRS_ID):
    """把 [(lng, lat), ...] 编码为 GeoPackage 线要素 GPB + ISO WKB。

    结构 = GPB 头(magic "GP" + version + flags + srs_id + envelope) + ISO WKB。
    关键点（与 OGC Simple Features WKB 不同）：
      · ISO LineString 类型码为 1002（非 OGC 的 2）；
      · 坐标按 XYZ 三维编码（z=0）。
    """
    min_x = min(c[0] for c in coords)
    max_x = max(c[0] for c in coords)
    min_y = min(c[1] for c in coords)
    max_y = max(c[1] for c in coords)

    blob = bytearray()
    blob += b"GP"                        # magic "GP"
    blob += b"\x00"                      # version = 0
    blob += b"\x05"                      # flags: bit0=小端, E=2 → XYZ envelope
    blob += struct.pack("<i", srs_id)    # srs_id（小端）
    blob += struct.pack("<6d", min_x, max_x, min_y, max_y, 0.0, 0.0)

    blob += struct.pack("<B", 1)             # byte order: 1 = little endian
    blob += struct.pack("<I", 1002)          # ISO LineString 类型码
    blob += struct.pack("<I", len(coords))   # 顶点数
    for lng, lat in coords:
        blob += struct.pack("<3d", float(lng), float(lat), 0.0)
    return bytes(blob)


def _gpkg_point_blob(lng, lat, srs_id=WGS84_SRS_ID):
    """把单个 (lng, lat) 编码为 GeoPackage 点要素 GPB + ISO WKB（ISO Point=1001）。"""
    x, y = float(lng), float(lat)
    blob = bytearray()
    blob += b"GP"
    blob += b"\x00"
    blob += b"\x05"                                   # XYZ envelope
    blob += struct.pack("<i", srs_id)
    blob += struct.pack("<6d", x, x, y, y, 0.0, 0.0)  # 点：min=max
    blob += struct.pack("<B", 1)
    blob += struct.pack("<I", 1001)                   # ISO Point 类型码
    blob += struct.pack("<3d", x, y, 0.0)
    return bytes(blob)


def _init_gpkg(conn):
    """初始化 GeoPackage 元数据表（spatial_ref_sys / contents / geometry_columns / extensions）。"""
    cur = conn.cursor()
    cur.execute(f"PRAGMA application_id = {0x47504B47}")  # ASCII "GPKG"
    cur.execute("PRAGMA user_version = 10200")

    cur.execute("""CREATE TABLE gpkg_spatial_ref_sys (
        srs_name TEXT NOT NULL,
        srs_id INTEGER NOT NULL PRIMARY KEY,
        organization TEXT NOT NULL,
        organization_coordsys_id INTEGER NOT NULL,
        definition TEXT NOT NULL,
        description TEXT)""")
    cur.execute("INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)",
                ("WGS 84", WGS84_SRS_ID, "EPSG", WGS84_SRS_ID,
                 WGS84_WKT, "WGS 84（经纬度，度）"))

    cur.execute("""CREATE TABLE gpkg_contents (
        table_name TEXT NOT NULL PRIMARY KEY,
        data_type TEXT NOT NULL,
        identifier TEXT UNIQUE,
        description TEXT DEFAULT '',
        last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE,
        srs_id INTEGER,
        CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id)
            REFERENCES gpkg_spatial_ref_sys(srs_id))""")

    cur.execute("""CREATE TABLE gpkg_geometry_columns (
        table_name TEXT NOT NULL,
        column_name TEXT NOT NULL,
        geometry_type_name TEXT NOT NULL,
        srs_id INTEGER NOT NULL,
        z TINYINT NOT NULL,
        m TINYINT NOT NULL,
        CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name),
        CONSTRAINT fk_gc_tn FOREIGN KEY (table_name)
            REFERENCES gpkg_contents(table_name),
        CONSTRAINT fk_gc_srs FOREIGN KEY (srs_id)
            REFERENCES gpkg_spatial_ref_sys(srs_id))""")

    cur.execute("""CREATE TABLE gpkg_extensions (
        table_name TEXT,
        column_name TEXT,
        extension_name TEXT NOT NULL,
        definition TEXT NOT NULL,
        scope TEXT NOT NULL,
        CONSTRAINT ge_tce UNIQUE (table_name, column_name, extension_name))""")
    return cur


def _write_line_layer(cur, table, busline_rows):
    """在已初始化的 gpkg 连接上创建线路要素表（LINESTRING）并写入线要素。"""
    cols = ", ".join(f'"{f}" TEXT' for f in LINE_OUT_FIELDS)
    cur.execute(f'CREATE TABLE "{table}" ('
                f'fid INTEGER PRIMARY KEY AUTOINCREMENT, geom LINESTRING, {cols})')
    cur.execute("INSERT INTO gpkg_contents "
                "(table_name, data_type, identifier, description, srs_id) "
                "VALUES (?, 'features', ?, ?, ?)",
                (table, table, "公交线路", WGS84_SRS_ID))
    cur.execute("INSERT INTO gpkg_geometry_columns "
                "(table_name, column_name, geometry_type_name, srs_id, z, m) "
                "VALUES (?, 'geom', 'LINESTRING', ?, 0, 0)",
                (table, WGS84_SRS_ID))

    placeholders = ", ".join("?" for _ in LINE_OUT_FIELDS)
    min_x = min_y = max_x = max_y = None
    n = 0
    for r in busline_rows:
        coords = r.get("coord_arr_wgs84") or []
        if len(coords) < 2:
            continue
        blob = _gpkg_linestring_blob(coords)
        values = [r.get(f, "") for f in LINE_OUT_FIELDS]
        cur.execute(f'INSERT INTO "{table}" (geom, {", ".join(LINE_OUT_FIELDS)}) '
                    f'VALUES (?, {placeholders})', (blob, *values))
        for lng, lat in coords:
            lng, lat = float(lng), float(lat)
            if min_x is None or lng < min_x:
                min_x = lng
            if max_x is None or lng > max_x:
                max_x = lng
            if min_y is None or lat < min_y:
                min_y = lat
            if max_y is None or lat > max_y:
                max_y = lat
        n += 1

    cur.execute("UPDATE gpkg_contents SET min_x=?, min_y=?, max_x=?, max_y=? "
                "WHERE table_name=?", (min_x, min_y, max_x, max_y, table))
    return n


def _write_stop_layer(cur, table, linestop_rows):
    """在已初始化的 gpkg 连接上创建站点要素表（POINT）并写入点要素。"""
    cols = ", ".join(f'"{f}" TEXT' for f in STOP_OUT_FIELDS)
    cur.execute(f'CREATE TABLE "{table}" ('
                f'fid INTEGER PRIMARY KEY AUTOINCREMENT, geom POINT, {cols})')
    cur.execute("INSERT INTO gpkg_contents "
                "(table_name, data_type, identifier, description, srs_id) "
                "VALUES (?, 'features', ?, ?, ?)",
                (table, table, "公交站点", WGS84_SRS_ID))
    cur.execute("INSERT INTO gpkg_geometry_columns "
                "(table_name, column_name, geometry_type_name, srs_id, z, m) "
                "VALUES (?, 'geom', 'POINT', ?, 0, 0)",
                (table, WGS84_SRS_ID))

    placeholders = ", ".join("?" for _ in STOP_OUT_FIELDS)
    min_x = min_y = max_x = max_y = None
    n = 0
    for r in linestop_rows:
        lng = r.get("lng_wgs84") or r.get("lng") or ""
        lat = r.get("lat_wgs84") or r.get("lat") or ""
        if lng == "" or lat == "":
            continue
        lng, lat = float(lng), float(lat)
        blob = _gpkg_point_blob(lng, lat)
        values = [r.get(f, "") for f in STOP_OUT_FIELDS]
        cur.execute(f'INSERT INTO "{table}" (geom, {", ".join(STOP_OUT_FIELDS)}) '
                    f'VALUES (?, {placeholders})', (blob, *values))
        if min_x is None or lng < min_x:
            min_x = lng
        if max_x is None or lng > max_x:
            max_x = lng
        if min_y is None or lat < min_y:
            min_y = lat
        if max_y is None or lat > max_y:
            max_y = lat
        n += 1

    cur.execute("UPDATE gpkg_contents SET min_x=?, min_y=?, max_x=?, max_y=? "
                "WHERE table_name=?", (min_x, min_y, max_x, max_y, table))
    return n


def _write_gpkg(outdir, provider, busline_rows, linestop_rows):
    """把线路（LINESTRING）与站点（POINT）两个图层写入同一个 GeoPackage 文件。"""
    path = os.path.join(outdir, f"bus_{provider}.gpkg")
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    cur = _init_gpkg(conn)
    n_line = _write_line_layer(cur, f"busline_{provider}", busline_rows)
    n_stop = _write_stop_layer(cur, f"busstop_{provider}", linestop_rows)
    conn.commit()
    conn.close()
    print(f"[{provider}] 已写出：{path}"
          f"（线要素 {n_line} 条 + 点要素 {n_stop} 个，WGS84/EPSG:4326）")
    return n_line, n_stop


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def run(params):
    """由统一入口 busline 调用的编排函数（高德）。

    本模块不再作为命令行脚本独立运行，请使用 `busline amap ...`。
    params 为扁平参数字典，键名与 busline 子命令参数一致：
    amap_key / city / lines / prefix / suffix / extra / only / exact /
    max_num / max_qps / delay / gpkg / geojson / outdir。
    """
    key_str = params.get("amap_key", "") or os.environ.get("AMAP_KEY", "")
    # 支持中英文逗号分隔多 key，去空
    keys = [k.strip() for k in key_str.replace("，", ",").split(",") if k.strip()]
    if not keys:
        print("错误：未提供高德 Key（--amap-key / configs/keys.txt 的 AMAP_KEY / 系统环境变量 AMAP_KEY）",
              file=sys.stderr)
        sys.exit(2)

    city = params.get("city", "上海")
    max_num = int(params.get("max_num", 1000))
    exact = bool(params.get("exact", False))  # 精确检索开关

    # 特殊关键字：留空则不追加（各城市默认词不同，统一在配置文件 extra 字段维护）
    extra = [x.strip() for x in params.get("extra", "").replace("，", ",").split(",")
             if x.strip()]

    specified_lines = ([x.strip() for x in params.get("lines", "").replace("，", ",").split(",")
                        if x.strip()] if params.get("lines") else None)
    prefixes = ([x.strip() for x in params.get("prefix", "").replace("，", ",").split(",")
                 if x.strip()] if params.get("prefix") else [])
    suffixes = ([x.strip() for x in params.get("suffix", "").replace("，", ",").split(",")
                 if x.strip()] if params.get("suffix") else [])
    num_prefixes = []
    num_suffixes = []
    only = params.get("only", "")
    if only:
        # 只检索模式：不做数字穷举（模糊匹配，适合「机场」这类前缀词，
        # 可命中「机场七线」等所有含该词的线路）
        specified_lines = [x.strip() for x in only.replace("，", ",").split(",") if x.strip()]
    elif specified_lines:
        if prefixes:
            # 前缀 × 线路 笛卡尔积：生成「前缀+线路」组合关键字（如 浦东 + 102路 = 浦东102路）
            specified_lines = [p + l for p in prefixes for l in specified_lines]
        if suffixes:
            print("提示：--suffix 仅在数字穷举模式生效，指定 --lines 时已忽略。", file=sys.stderr)
    else:
        # 数字穷举模式：前缀 + 数字 + 后缀（如 浦东 + 1~N + 路 = 浦东1路~浦东N路）
        num_prefixes = prefixes
        num_suffixes = suffixes

    # 输出目录命名：runs/<时间戳>_<provider>_<城市>_<模式>_<关键字>/
    if only:
        mode = "only"
        kw_summary = [x.strip() for x in only.replace("，", ",").split(",") if x.strip()]
    elif specified_lines:
        mode = "lines"
        kw_summary = specified_lines
    else:
        mode = "full"
        if num_prefixes or num_suffixes:
            num_kws = build_num_keywords(max_num, num_prefixes, num_suffixes)
            kw_summary = ["%s~%s" % (num_kws[0], num_kws[-1])]
        else:
            kw_summary = ["1-%d" % max_num]
    run_dir = build_run_subdir(params.get("outdir", "run"), "amap", city, mode, kw_summary)
    os.makedirs(run_dir, exist_ok=True)
    print("[高德] 输出目录：%s" % run_dir)

    crawl_amap(keys, city, max_num, extra, run_dir,
               bool(params.get("gpkg", False)),
               bool(params.get("geojson", False)),
               float(params.get("delay", 0.0)),
               specified_lines=specified_lines,
               max_qps=float(params.get("max_qps", 28.0)),
               num_prefixes=num_prefixes, num_suffixes=num_suffixes,
               exact=exact)

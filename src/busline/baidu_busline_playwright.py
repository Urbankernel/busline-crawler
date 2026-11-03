#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百度地图公交线路站点抓取（自动化版，基于 Playwright 无头浏览器）

原理：百度官方 Web 服务 API 没有公交线路详情接口，唯一公开途径是
浏览器端 JS API 的 BMap.BusLineSearch。本脚本用 Playwright 驱动无头浏览器，
自动执行 JS 调用，把结果通过 window.__result 回传给 Python，再落盘为
JSON / CSV / GeoJSON。

依赖：pip install playwright && playwright install chromium

用法（本模块不直接运行，请用统一入口 `busline baidu ...`）：
  # 数字穷举全城市（默认）
  busline baidu --city 深圳 --max-num 1000 --outdir runs
  # 只抓指定线路
  busline baidu --city 深圳 --lines "406,406路大站车,M406" --outdir runs
"""

import csv
import json
import os
import sqlite3
import sys

# 复用高德库（busline_crawler.py）里已验证的 GeoPackage 底层实现：
# GPB 头 + ISO WKB 编码 + 元数据表 + WGS84(EPSG:4326) 常量。
# 两个模块同位于 src/busline/ 包内，用相对导入。
from . import busline_crawler

# 可选：引入 Playwright（未安装时给出友好提示）
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


# ==================== 内嵌 JS 抓取逻辑（跑在无头浏览器里） ====================
# 逻辑与 baidu_busline.html 一致，区别是：完成后把结果写入 window.__result / window.__done，
# 由 Python 侧 wait_for_function 读取，而不是浏览器端下载文件。
INJECT_JS = r"""
(function () {
  var CONFIG = window.__CONFIG;
  var DELAY = CONFIG.delay_ms || 1000;
  var waitCount = 0;

  // ---------- 坐标转换（BD-09 -> GCJ-02 -> WGS84） ----------
  var X_PI = Math.PI * 3000.0 / 180.0;
  var A = 6378245.0, EE = 0.00669342162296594323;
  function bd09ToGcj02(bdLon, bdLat) {
    var x = bdLon - 0.0065, y = bdLat - 0.006;
    var z = Math.sqrt(x * x + y * y) - 0.00002 * Math.sin(y * X_PI);
    var t = Math.atan2(y, x) - 0.000003 * Math.cos(x * X_PI);
    return [z * Math.cos(t), z * Math.sin(t)];
  }
  function outOfChina(lon, lat) {
    return lon < 72.004 || lon > 137.8347 || lat < 0.8293 || lat > 55.8271;
  }
  function tLat(x, y) {
    var r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
    r += (20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0 / 3.0;
    r += (20.0 * Math.sin(y * Math.PI) + 40.0 * Math.sin(y / 3.0 * Math.PI)) * 2.0 / 3.0;
    r += (160.0 * Math.sin(y / 12.0 * Math.PI) + 320.0 * Math.sin(y * Math.PI / 30.0)) * 2.0 / 3.0;
    return r;
  }
  function tLon(x, y) {
    var r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
    r += (20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0 / 3.0;
    r += (20.0 * Math.sin(x * Math.PI) + 40.0 * Math.sin(x / 3.0 * Math.PI)) * 2.0 / 3.0;
    r += (150.0 * Math.sin(x / 12.0 * Math.PI) + 300.0 * Math.sin(x / 30.0 * Math.PI)) * 2.0 / 3.0;
    return r;
  }
  function gcj02ToWgs84(lon, lat) {
    if (outOfChina(lon, lat)) return [lon, lat];
    var dLat = tLat(lon - 105.0, lat - 35.0);
    var dLon = tLon(lon - 105.0, lat - 35.0);
    var rad = lat / 180.0 * Math.PI;
    var magic = Math.sin(rad);
    magic = 1 - EE * magic * magic;
    var sm = Math.sqrt(magic);
    dLat = (dLat * 180.0) / ((A * (1 - EE)) / (magic * sm) * Math.PI);
    dLon = (dLon * 180.0) / (A / sm * Math.cos(rad) * Math.PI);
    return [lon - dLon, lat - dLat];
  }
  function bd09ToWgs84(lon, lat) {
    var g = bd09ToGcj02(lon, lat);
    return gcj02ToWgs84(g[0], g[1]);
  }

  // 线路去重键规范化：全角→半角、去空白、英文小写（与 Python 侧 _norm_line_key 对齐）
  function normKey(n) {
    return String(n || '')
      .replace(/[（]/g, '(').replace(/[）]/g, ')')
      .replace(/[，]/g, ',').replace(/[：]/g, ':').replace(/[－]/g, '-')
      .replace(/\s+/g, '').toLowerCase();
  }

  // ---------- 状态 ----------
  var buslines = [], stops = [], savedNames = {}, routeCounter = 0;
  var diag = [];   // 抓取诊断日志（每个关键字的检索与命中情况）

  // ---------- 抓取主逻辑（等 BMap 就绪后由 waitReady 调用） ----------
  function run() {
    try {
      var search = new BMap.BusLineSearch(CONFIG.city, {
        onGetBusListComplete: onList,
        onGetBusLineComplete: onLine
      });

      var keywordQueue = CONFIG.keywords.slice();
      var currentKeyword = null;
      var pending = 0;      // 未完成的异步回调数（>0 表示还有检索/线路在途）
      var finished = false;
      // 指定线路模式（--lines）时，用于精确过滤模糊检索混入的其他线路
      var exactFilter = CONFIG.exact_filter || null;

      function onList(r) {
        pending--;
        var num = 0;
        try { num = r.getNumBusList(); }
        catch (e) { diag.push('getNumBusList 异常: ' + (e && e.message ? e.message : e)); }
        diag.push('检索 "' + currentKeyword + '" -> 命中 ' + num + ' 条');
        if (num === 0 && /^\d+$/.test(currentKeyword)) {
          // 纯数字关键字 0 命中：追加「路」重试一次（部分城市检索需带"路"）
          currentKeyword = currentKeyword + '路';
          diag.push('  -> 纯数字 0 命中，追加"路"重试: ' + currentKeyword);
          pending++;
          try { search.getBusList(currentKeyword); }
          catch (e) { pending--; diag.push('  重试异常: ' + (e && e.message ? e.message : e)); }
        } else {
          for (var i = 0; i < num; i++) {
            pending++;
            try { search.getBusLine(r.getBusListItem(i)); }
            catch (e) { pending--; diag.push('  getBusLine 异常: ' + (e && e.message ? e.message : e)); }
          }
        }
        maybeFinish();
      }

      function onLine(busLine) {
        pending--;
        try {
          var lineKey = busLine ? normKey(busLine.name) : '';
          if (!busLine || !busLine.name || savedNames[lineKey]) {
            return;
          }
          savedNames[lineKey] = true;

          // ---------- 公交线路合法性判断（关键：过滤模糊检索混入的非公交 POI） ----------
          // getBusList 是模糊检索，会把「406」「406路」匹配到门牌号、小区、公交站名等
          // 非公交 POI（如「南大路406弄」「车站南路406弄小区」「公交406终点站」）。
          // 这些 POI 没有公交属性，调 getNumBusStations()/getPath() 会抛
          // "this.cj.xe is not a function"，或返回 0 站点/空轨迹。
          // 判定真公交线路的三要素：① 有站点 ② 有轨迹 ③ 有首末班时间或运营公司。
          var n = 0, path = null;
          try {
            n = busLine.getNumBusStations();
          } catch (e) {
            diag.push('  跳过非公交线路: ' + busLine.name + '（取站点数异常）');
            return;
          }
          try {
            path = busLine.getPath();
          } catch (e) {
            diag.push('  跳过非公交线路: ' + busLine.name + '（取轨迹异常）');
            return;
          }
          var hasTime = !!(busLine.startTime || busLine.endTime || busLine.company);
          if (n <= 0 || !path || path.length < 2 || !hasTime) {
            diag.push('  跳过非公交线路: ' + busLine.name +
                      '（站点=' + n + '，轨迹点=' + (path ? path.length : 0) +
                      '，首末班=' + (busLine.startTime || '') + '/' + (busLine.endTime || '') + '）');
            return;
          }

          // ---------- 精确检索（--exact 开关启用） ----------
          // getBusList 是模糊检索，「浦东102路」会同时命中「102路」「浦东107路」
          // 「浦东110路」等含相同字样的其他线路。--exact 开启时，只保留短名与
          // 关键字严格全等的线路（不做补路/补线回退：关键字 406 不会匹配 406路）。
          if (exactFilter && exactFilter.length > 0) {
            var flds0 = busLine.name.split('(');
            var sn = (flds0[0] || '').replace(/^\s+|\s+$/g, '');
            var matched = false;
            for (var f = 0; f < exactFilter.length; f++) {
              var k = (exactFilter[f] || '').replace(/^\s+|\s+$/g, '');
              if (!k) continue;
              if (sn === k) {
                matched = true;
                break;
              }
            }
            if (!matched) {
              diag.push('  跳过非目标线路: ' + busLine.name + '（短名 "' + sn + '" 不匹配关键字）');
              return;
            }
          }

          // ---------- 合法线路：入库 ----------
          routeCounter++;
          var rid = routeCounter;
          var flds = busLine.name.split('(');
          var shortName = flds[0];
          var first = '', last = '';
          for (var i = 0; i < n; i++) {
            var st = busLine.getBusStation(i);
            if (i === 0) first = st.name;
            if (i === n - 1) last = st.name;
            var lng = st.position.lng, lat = st.position.lat;
            var w = bd09ToWgs84(lng, lat);
            stops.push({
              route_id: rid, line_name: shortName, route_name: busLine.name,
              station_index: i + 1,
              station_id: Math.round(lng * 1000000) + '_' + Math.round(lat * 1000000),
              station_name: st.name, lng: lng, lat: lat,
              lng_wgs84: w[0], lat_wgs84: w[1]
            });
          }
          var ca = [], cw = [];
          for (var j = 0; j < path.length; j++) {
            ca.push([path[j].lng, path[j].lat]);
            cw.push(bd09ToWgs84(path[j].lng, path[j].lat));
          }
          buslines.push({
            route_id: rid, line_name: shortName, route_name: busLine.name,
            start_time: busLine.startTime || '', end_time: busLine.endTime || '',
            first_station: first, last_station: last, company: busLine.company || '',
            coord_arr: ca, coord_arr_wgs84: cw
          });
          diag.push('  线路: ' + busLine.name + ' (' + n + ' 站)');
        } catch (e) {
          diag.push('  线路解析异常: ' + (e && e.message ? e.message : e));
        } finally {
          maybeFinish();
        }
      }

      function maybeFinish() {
        if (finished) return;
        if (keywordQueue.length === 0 && pending <= 0) {
          finished = true;
          window.__result = { buslines: buslines, stops: stops, diag: diag };
          window.__done = true;
        }
      }

      // ---------- 节流串行驱动：每隔 DELAY 发一个关键字 ----------
      var timer = setInterval(function () {
        if (keywordQueue.length === 0) {
          clearInterval(timer);
          maybeFinish();
          return;
        }
        currentKeyword = keywordQueue.shift();
        pending++;
        try { search.getBusList(currentKeyword); }
        catch (e) { pending--; diag.push('检索异常 "' + currentKeyword + '": ' + (e && e.message ? e.message : e)); }
      }, DELAY);

      // 兜底超时：即使回调丢失也最终返回（超时值由 Python 侧按关键字数动态计算），避免永久卡死
      setTimeout(function () {
        if (!finished) {
          diag.push('警告：等待回调超时（仍有 pending=' + pending + ' 个在途），强制返回');
          window.__result = { buslines: buslines, stops: stops, diag: diag };
          window.__done = true;
        }
      }, CONFIG.timeout_ms || 120000);
    } catch (e) {
      window.__error = 'JS 抓取异常：' + (e && e.message ? e.message : String(e));
      window.__done = true;
    }
  }

  // 百度 JS API v2.0 是异步加载的：<script src=...api?v=2.0> 先返回一个 loader，
  // BMap 要等 loader 异步拉完核心 API 才定义。这里轮询等待 BMap 就绪后再启动抓取，
  // 避免同步执行时 BMap 尚未定义。
  (function waitReady() {
    if (typeof BMap !== 'undefined' && BMap.BusLineSearch) {
      run();
    } else if (waitCount++ < 300) {   // 最多等 30s（100ms × 300）
      setTimeout(waitReady, 100);
    } else {
      window.__error = '百度 JS API 加载超时（30s 内 BMap 未就绪），请检查 AK 是否正确、网络是否可达';
      window.__done = true;
    }
  })();
})();
"""


def build_keywords(max_num, extra, prefixes=None, suffixes=None):
    """构造关键字队列：数字 1~max_num（可加前缀/后缀） + 特殊关键字。"""
    keywords = busline_crawler.build_num_keywords(max_num, prefixes, suffixes)
    keywords += list(extra or [])
    return keywords


def crawl(city, ak, keywords, delay_ms=1000, headless=True, timeout_s=0,
          exact_filter=None):
    """驱动无头浏览器执行抓取，返回 (buslines, stops)。

    CONFIG 直接内联进 HTML（不再用 add_init_script 注入——它在 set_content 场景下
    时机不可靠，会导致 window.__CONFIG 为 undefined、JS 抛异常后静默卡死）。

    timeout_s: 整个抓取流程的最大等待秒数。0（默认）按关键字数自动计算兜底超时
               （(关键字数 + 缓冲) × 间隔 + 固定余量），保证「全量穷举(1000+ 关键字)」
               不会被固定超时提前截断；显式传 >0 时作为兜底上限（用户可自行控制，
               可能因等待不足而提前返回部分结果）。

    exact_filter: 精确检索关键字列表（--exact 开启时传入）；非空时只保留
                  短名与之严格全等的线路，过滤模糊检索混入的其他线路。
    """
    if sync_playwright is None:
        raise RuntimeError(
            "未安装 Playwright，请先执行：pip install playwright && playwright install chromium")

    # 自动兜底超时：内嵌 JS 每隔 delay_ms 发送一个关键字，纯节流就需
    # len(keywords)*delay_ms 毫秒；固定 600s 会在「全量穷举」时于正常完成前
    # 触发兜底、静默截断数据。这里按「节流发送时长 + 每关键字回调余量 +
    # 固定余量」动态计算，用户显式指定 --timeout 时优先用用户值。
    auto_timeout_ms = (len(keywords) + 20) * delay_ms + 60000
    if timeout_s and timeout_s > 0:
        effective_timeout_ms = int(timeout_s * 1000)
    else:
        effective_timeout_ms = auto_timeout_ms

    config_js = "window.__CONFIG = " + json.dumps(
        {"city": city, "keywords": keywords, "delay_ms": delay_ms,
         "timeout_ms": effective_timeout_ms, "exact_filter": exact_filter or []},
        ensure_ascii=False) + ";"

    page_html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
        "<script src='http://api.map.baidu.com/api?v=2.0&ak=" + ak + "'></script>"
        "<script>" + config_js + INJECT_JS + "</script>"
        "</body></html>"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        page_errors = []
        console_msgs = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("console", lambda msg: console_msgs.append("[%s] %s" % (msg.type, msg.text)))

        # 关键：不能用 page.set_content（其页面 origin 是 about:blank，opaque origin），
        # 百度 JS API 内部会访问 localStorage，在 about:blank 下抛
        # "Access is denied for this document" 导致脚本中断、BMap 未定义。
        # 这里用 page.route 拦截一个 http 地址作为主文档，给页面一个真实 origin，
        # localStorage 即可正常读写，百度脚本才能完整加载。
        doc_url = "http://127.0.0.1:9/"
        page.route(doc_url, lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=page_html))
        page.goto(doc_url)

        try:
            # 等待 JS 抓取完成（window.__done === true；出错时也会置 done 并写 __error）。
            # wait_for_function 超时略大于 JS 侧兜底超时，保证 JS 兜底能先触发并
            # 回传已抓数据，而不是由 Python 侧直接抛错丢弃数据。
            page.wait_for_function("window.__done === true",
                                   timeout=effective_timeout_ms + 10000)
        except Exception as exc:
            detail = "; ".join(page_errors) if page_errors else "无（可能百度 API 一直未回调）"
            raise RuntimeError(
                "抓取超时/失败（%gs，%s）。JS 侧错误：%s"
                % (effective_timeout_ms / 1000.0, exc, detail))

        err = page.evaluate("window.__error || null")
        result = page.evaluate("window.__result")
        browser.close()

    if err:
        extra = "; ".join(x for x in (page_errors + console_msgs) if x)
        raise RuntimeError("抓取失败：%s%s" % (err, ("；浏览器侧：%s" % extra) if extra else ""))
    if not result or "buslines" not in result:
        raise RuntimeError("未获得抓取结果（window.__result 为空），请检查 AK / 城市 / 线路关键字。")
    return (result["buslines"], result["stops"],
            result.get("diag", []), console_msgs)


# ==================== GPKG 输出（与高德版本一致：单文件双图层，EPSG:4326） ====================
BAIDU_LINE_FIELDS = [
    "line_name", "route_id", "route_name", "start_time", "end_time",
    "first_station", "last_station", "company",
]
BAIDU_STOP_FIELDS = [
    "route_id", "line_name", "route_name", "station_index",
    "station_id", "station_name", "lng", "lat", "lng_wgs84", "lat_wgs84",
]


def _write_baidu_line_layer(cur, table, buslines):
    """在已初始化的 gpkg 连接上创建线路要素表（LINESTRING）并写入。"""
    cols = ", ".join(f'"{f}" TEXT' for f in BAIDU_LINE_FIELDS)
    cur.execute(f'CREATE TABLE "{table}" ('
                f'fid INTEGER PRIMARY KEY AUTOINCREMENT, geom LINESTRING, {cols})')
    cur.execute("INSERT INTO gpkg_contents "
                "(table_name, data_type, identifier, description, srs_id) "
                "VALUES (?, 'features', ?, ?, ?)",
                (table, table, "公交线路（百度）", busline_crawler.WGS84_SRS_ID))
    cur.execute("INSERT INTO gpkg_geometry_columns "
                "(table_name, column_name, geometry_type_name, srs_id, z, m) "
                "VALUES (?, 'geom', 'LINESTRING', ?, 0, 0)",
                (table, busline_crawler.WGS84_SRS_ID))

    placeholders = ", ".join("?" for _ in BAIDU_LINE_FIELDS)
    min_x = min_y = max_x = max_y = None
    n = 0
    for r in buslines:
        coords = r.get("coord_arr_wgs84") or []
        if len(coords) < 2:
            continue
        blob = busline_crawler._gpkg_linestring_blob(coords, busline_crawler.WGS84_SRS_ID)
        values = [str(r.get(f, "")) for f in BAIDU_LINE_FIELDS]
        cur.execute(f'INSERT INTO "{table}" (geom, {", ".join(BAIDU_LINE_FIELDS)}) '
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


def _write_baidu_stop_layer(cur, table, stops):
    """在已初始化的 gpkg 连接上创建站点要素表（POINT）并写入。"""
    cols = ", ".join(f'"{f}" TEXT' for f in BAIDU_STOP_FIELDS)
    cur.execute(f'CREATE TABLE "{table}" ('
                f'fid INTEGER PRIMARY KEY AUTOINCREMENT, geom POINT, {cols})')
    cur.execute("INSERT INTO gpkg_contents "
                "(table_name, data_type, identifier, description, srs_id) "
                "VALUES (?, 'features', ?, ?, ?)",
                (table, table, "公交站点（百度）", busline_crawler.WGS84_SRS_ID))
    cur.execute("INSERT INTO gpkg_geometry_columns "
                "(table_name, column_name, geometry_type_name, srs_id, z, m) "
                "VALUES (?, 'geom', 'POINT', ?, 0, 0)",
                (table, busline_crawler.WGS84_SRS_ID))

    placeholders = ", ".join("?" for _ in BAIDU_STOP_FIELDS)
    min_x = min_y = max_x = max_y = None
    n = 0
    for r in stops:
        lng = r.get("lng_wgs84") or r.get("lng") or ""
        lat = r.get("lat_wgs84") or r.get("lat") or ""
        if lng == "" or lat == "":
            continue
        lng, lat = float(lng), float(lat)
        blob = busline_crawler._gpkg_point_blob(lng, lat, busline_crawler.WGS84_SRS_ID)
        values = [str(r.get(f, "")) for f in BAIDU_STOP_FIELDS]
        cur.execute(f'INSERT INTO "{table}" (geom, {", ".join(BAIDU_STOP_FIELDS)}) '
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


def _write_gpkg_baidu(outdir, buslines, stops):
    """把线路（LINESTRING）与站点（POINT）两个图层写入同一个 GeoPackage 文件。"""
    path = os.path.join(outdir, "bus_baidu.gpkg")
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    cur = busline_crawler._init_gpkg(conn)
    n_line = _write_baidu_line_layer(cur, "busline_baidu", buslines)
    n_stop = _write_baidu_stop_layer(cur, "busstop_baidu", stops)
    conn.commit()
    conn.close()
    print(f"[百度] 已写出：{path}"
          f"（线要素 {n_line} 条 + 点要素 {n_stop} 个，WGS84/EPSG:4326）")
    return n_line, n_stop


def write_outputs(outdir, buslines, stops, make_gpkg=False):
    """把结果落盘为 JSON / CSV / GeoJSON，可选输出 GPKG。"""
    os.makedirs(outdir, exist_ok=True)

    # 输出前统一去重兜底（按规范化 route_name），并同步剔除重复线路的站点
    buslines, stops = busline_crawler._dedup_lines(buslines, stops)

    with open(os.path.join(outdir, "busline.json"), "w", encoding="utf-8") as f:
        json.dump(buslines, f, ensure_ascii=False, indent=2)

    with open(os.path.join(outdir, "busstop.json"), "w", encoding="utf-8") as f:
        json.dump(stops, f, ensure_ascii=False, indent=2)

    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"route_id": l["route_id"], "line_name": l["line_name"],
                               "route_name": l["route_name"]},
                "geometry": {"type": "LineString", "coordinates": l["coord_arr_wgs84"]},
            }
            for l in buslines
        ],
    }
    with open(os.path.join(outdir, "busline.geojson"), "w", encoding="utf-8") as f:
        json.dump(geojson, f, ensure_ascii=False, indent=2)

    # CSV（兼容文章原文格式，坐标用 BD-09 原始值）
    with open(os.path.join(outdir, "busline.txt"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["route_id", "line_name", "route_name", "start_time", "end_time",
                    "first_station", "last_station", "company", "coord_arr"])
        for l in buslines:
            coord_str = "|".join("%s_%s" % (c[0], c[1]) for c in l["coord_arr"])
            w.writerow([l["route_id"], l["line_name"], l["route_name"], l["start_time"],
                        l["end_time"], l["first_station"], l["last_station"], l["company"],
                        coord_str])

    with open(os.path.join(outdir, "line_stop.txt"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["route_id", "line_name", "route_name", "station_index",
                    "station_id", "station_name", "lng", "lat"])
        for s in stops:
            w.writerow([s["route_id"], s["line_name"], s["route_name"], s["station_index"],
                        s["station_id"], s["station_name"], s["lng"], s["lat"]])

    if make_gpkg:
        _write_gpkg_baidu(outdir, buslines, stops)


def run(params):
    """由统一入口 busline 调用的编排函数（百度）。

    本模块不再作为命令行脚本独立运行，请使用 `busline baidu ...`。
    params 为扁平参数字典，键名与 busline 子命令参数一致：
    baidu_ak / city / lines / prefix / suffix / extra / only / exact /
    max_num / delay_ms / timeout / headed / gpkg / outdir。
    """
    ak = params.get("baidu_ak", "") or os.environ.get("BAIDU_AK", "")
    if not ak:
        print("错误：未提供百度 AK（--baidu-ak / configs/keys.txt 的 BAIDU_AK / 系统环境变量 BAIDU_AK）",
              file=sys.stderr)
        sys.exit(2)

    city = params.get("city", "上海")
    max_num = int(params.get("max_num", 1000))
    only = params.get("only", "")
    lines = params.get("lines", "")
    prefixes = [k.strip() for k in params.get("prefix", "").replace("，", ",").split(",")
                if k.strip()]
    suffixes = [k.strip() for k in params.get("suffix", "").replace("，", ",").split(",")
                if k.strip()]
    exact = bool(params.get("exact", False))  # 精确检索开关

    if only:
        # 只检索模式：不做数字穷举；默认模糊匹配（适合「机场」这类前缀词，
        # 能命中「机场七线」等所有含该词的线路）。加 --exact 才按短名全等过滤。
        keywords = [k.strip() for k in only.replace("，", ",").split(",") if k.strip()]
        print("只检索关键字 %d 个：%s，开始抓取..." % (len(keywords), ", ".join(keywords)))
    elif lines:
        line_list = [k.strip() for k in lines.replace("，", ",").split(",") if k.strip()]
        if suffixes:
            print("提示：--suffix 仅在数字穷举模式生效，指定 --lines 时已忽略。", file=sys.stderr)
        if prefixes:
            # 前缀 × 线路 笛卡尔积：生成「前缀+线路」组合关键字（如 浦东 + 102路 = 浦东102路）
            keywords = [p + l for p in prefixes for l in line_list]
            print("指定线路 %d 条 × 前缀 %d 个 = %d 个关键字：%s，开始抓取..."
                  % (len(line_list), len(prefixes), len(keywords), ", ".join(keywords)))
        else:
            keywords = line_list
            print("指定线路 %d 条：%s，开始抓取..." % (len(keywords), ", ".join(keywords)))
    else:
        extra = [k.strip() for k in params.get("extra", "").replace("，", ",").split(",")
                 if k.strip()]
        keywords = build_keywords(max_num, extra, prefixes, suffixes)
        print("关键字总数：%d，开始抓取..." % len(keywords))

    # 精确检索开关：开启后按短名严格全等过滤（三种模式统一生效），否则模糊检索
    exact_filter = keywords if exact else None

    # 输出目录命名：runs/<时间戳>_<provider>_<城市>_<模式>_<关键字>/
    if only:
        mode = "only"
    elif lines:
        mode = "lines"
    else:
        mode = "full"
    if mode == "full":
        if prefixes or suffixes:
            num_kws = busline_crawler.build_num_keywords(max_num, prefixes, suffixes)
            kw_summary = ["%s~%s" % (num_kws[0], num_kws[-1])]
        else:
            kw_summary = ["1-%d" % max_num]
    else:
        kw_summary = keywords
    run_dir = busline_crawler.build_run_subdir(params.get("outdir", "run"), "baidu",
                                              city, mode, kw_summary)
    os.makedirs(run_dir, exist_ok=True)
    print("[百度] 输出目录：%s" % run_dir)

    try:
        buslines, stops, diag, console_msgs = crawl(
            city=city, ak=ak, keywords=keywords,
            delay_ms=int(params.get("delay_ms", 1000)),
            headless=not bool(params.get("headed", False)),
            timeout_s=int(params.get("timeout", 0)),
            exact_filter=exact_filter)
    except RuntimeError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        sys.exit(1)

    write_outputs(run_dir, buslines, stops, make_gpkg=bool(params.get("gpkg", False)))

    # 输出诊断信息（抓取结果为 0 时尤其关键）
    if not buslines:
        print("\n--- 抓取诊断（本次命中 0 条，可能原因见下）---")
        for d in diag:
            print("  " + d)
        if console_msgs:
            print("  [浏览器 console]")
            for c in console_msgs:
                print("    " + c)
        print("提示：若诊断显示所有关键字均『命中 0 条』，通常原因：\n"
              "  ① 关键字格式不对（可尝试 --lines \"406路\" 带『路』）；\n"
              "  ② 城市名不被识别（可尝试 --city \"上海市\"）；\n"
              "  ③ 浏览器端 AK 的 Referer 白名单未放行 127.0.0.1，导致检索被拒但脚本已加载。")

    print("完成：线路 %d 条，站点 %d 个，已写入 %s"
          % (len(buslines), len(stops), params.get("outdir", "run")))

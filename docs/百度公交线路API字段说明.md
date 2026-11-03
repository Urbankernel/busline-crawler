# 百度公交线路查询 API 返回字段说明

> 适用接口：百度地图 JavaScript API「公交线路查询」`BMap.BusLineSearch`
> 本文档解释 `BusLineSearch` 返回的**检索结果、线路对象、站点对象、轨迹坐标**的字段含义。
> 说明：百度官方 Web 服务 API（REST）**不提供公交线路详情接口**，线路 + 站点 + 轨迹只能通过 JS API 获取，运行环境为浏览器（或无头浏览器）。

---

## 1. 返回结构总览

`BusLineSearch` 通过**回调函数**异步返回数据，分三层：

```
new BMap.BusLineSearch(城市, {
    onGetBusListComplete: ... ,   // 回调1：检索结果
    onGetBusLineComplete: ...      // 回调2：单条线路详情
})
   └─ getBusList(keyword)         按关键字检索线路
        └─ 回调1 检索结果对象（BusListResult）
             ├── getNumBusList()     命中线路条数
             └── getBusListItem(i)   第 i 条线路项（继续传给 getBusLine）
                  └─ getBusLine(线路项)  逐条取线路详情
                       └─ 回调2 线路对象（BusLine）
                            ├── 线路属性（name / startTime / endTime / company）
                            ├── getBusStation(i)  站点对象（name / position.lng / position.lat）
                            └── getPath()         轨迹坐标数组（[{lng, lat}, ...]）
```

---

## 2. 检索结果对象（回调1：BusListResult）

| 方法 | 返回类型 | 含义 |
|---|---|---|
| `getNumBusList()` | number | 本次关键字命中的线路条数 |
| `getBusListItem(i)` | object | 第 i 条线路项（作为参数传给 `getBusLine` 取详情） |

---

## 3. 线路对象（回调2：BusLine）

| 字段 / 方法 | 类型 | 含义 | 示例 |
|---|---|---|---|
| `name` | string | 线路名（含起终点括号） | `"406路(XX--XX)"` |
| `startTime` | string | 首班时间 ⚠️可能为 undefined | `"06:00"` |
| `endTime` | string | 末班时间 ⚠️可能为 undefined | `"22:00"` |
| `company` | string | 所属公交公司 ⚠️可能为 undefined | `"深圳巴士集团"` |
| `hC` / `fC` | — | 结果合法性判断字段 ⚠️不稳定（详见 §6.4），不建议依赖 | — |
| `getNumBusStations()` | number | 途经站点总数 | `25` |
| `getBusStation(i)` | object | 第 i 个站点对象（详见 §4） | `{name, position}` |
| `getPath()` | array | 线路轨迹坐标数组（详见 §5） | `[{lng, lat}, ...]` |

---

## 4. 站点对象（`getBusStation(i)` 返回）

| 字段 | 类型 | 含义 | 示例 |
|---|---|---|---|
| `name` | string | 站点名称 | `"国定路政立路"` |
| `position.lng` | number | 站点经度（BD-09） | `121.503928` |
| `position.lat` | number | 站点纬度（BD-09） | `31.306354` |

> 说明：百度站点对象**没有官方站点 ID**。脚本用「坐标拼接」生成全局唯一 ID（详见 §7），依据是百度同一物理站点在不同线路上返回的经纬度完全一致。

---

## 5. 轨迹坐标点（`getPath()` 返回的数组元素）

| 字段 | 类型 | 含义 |
|---|---|---|
| `lng` | number | 轨迹点经度（BD-09） |
| `lat` | number | 轨迹点纬度（BD-09） |

---

## 6. 脚本输出字段对照

抓取脚本 `baidu_busline_playwright.py` 在提取 JS 对象字段的基础上做了以下处理：

| 处理 | 说明 |
|---|---|
| 坐标转换 | 保留原始 BD-09，同时新增 **WGS84（EPSG:4326）** 坐标 |
| 站点 ID 生成 | 站点对象无官方 ID，用 `Math.round(lng*1e6) + '_' + Math.round(lat*1e6)` 坐标拼接生成 |
| 线路简称提取 | `line_name` = `name` 去掉括号部分（`split('(')`） |
| 去重 | 线路按 `name` 去重、站点按坐标 ID 去重 |
| 字段兜底 | `startTime`/`endTime`/`company` 等空值统一兜底为空串 |

线路输出字段：

| 字段 | 含义 |
|---|---|
| `route_id` | 脚本自增序号（从 1 起） |
| `line_name` | 线路简称（`name` 去掉括号部分） |
| `route_name` | 线路全名（= 原 `name`） |
| `start_time` / `end_time` | 首末班时间 |
| `first_station` / `last_station` | 首末站名（由 `getBusStation` 提取） |
| `company` | 公交公司 |
| `coord_arr` | 轨迹坐标数组（BD-09） |
| `coord_arr_wgs84` | 轨迹坐标数组（WGS84） |

站点输出字段：

| 字段 | 含义 |
|---|---|
| `route_id` / `line_name` / `route_name` | 所属线路 |
| `station_index` | 站点序号（从 1 起） |
| `station_id` | 站点 ID（坐标拼接，全局唯一） |
| `station_name` | 站点名称 |
| `lng` / `lat` | 站点原始坐标（BD-09） |
| `lng_wgs84` / `lat_wgs84` | 站点转换后坐标（WGS84） |

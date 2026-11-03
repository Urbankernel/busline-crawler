# 高德公交线路查询 API 返回字段说明

> 适用接口：高德地图 Web 服务 API「公交线路查询」
> `GET https://restapi.amap.com/v3/bus/linename`
> 本文档解释 `extensions=all` 时返回结果的完整字段含义。

---

## 1. 返回结构总览

返回的 JSON 分三层：

```
顶层响应字段
└── buslines[]        公交线路数组（每条线路一个对象）
    ├── 线路基础字段
    └── busstops[]    该线路途经站点数组（每个站点一个对象）
```

---

## 2. 顶层响应字段

| 字段 | 类型 | 含义 | 示例 |
|---|---|---|---|
| `status` | string | 请求状态码，`1` = 成功，`0` = 失败 | `"1"` |
| `info` | string | 状态说明（失败时含错误描述） | `"OK"` |
| `infocode` | string | 详细状态码，`10000` = 正常；`10003` = 日配额用尽；`10001` = key 无效 | `"10000"` |
| `count` | string | 返回的线路条数 | `"4"` |
| `suggestion` | object | 无匹配结果时的检索建议 | `{...}` |
| `buslines` | array | 公交线路数组 | `[...]` |

`suggestion` 子字段（一般均为空）：

| 字段 | 类型 | 含义 |
|---|---|---|
| `keywords` | array | 关键字建议 |
| `cities` | array | 城市建议 |

---

## 3. buslines[] —— 线路字段

| 字段 | 类型 | 含义 | 示例 |
|---|---|---|---|
| `id` | string | 线路唯一 ID | `"310100015970"` |
| `type` | string | 线路类型 | `"普通公交"` |
| `name` | string | 线路名称（含起终点） | `"406路(国定路政立路--东靖路金京路)"` |
| `polyline` | string | 线路轨迹坐标串，`lng,lat;lng,lat;...`（分号分隔，GCJ-02） | `"121.503928,31.306354;..."` |
| `citycode` | string | 城市区号 | `"021"` |
| `start_stop` | string | 起点站名 | `"国定路政立路"` |
| `end_stop` | string | 终点站名 | `"东靖路金京路"` |
| `start_time` | string / [] | 首班车时间 ⚠️类型不稳定 | `[]` 或 `"2130"` |
| `end_time` | string / [] | 末班车时间 ⚠️类型不稳定 | `[]` 或 `"2230"` |
| `uicolor` | array | 线路主题色（通常为空） | `[]` |
| `timedesc` | string | 发车时刻描述（URL 编码的 JSON，详见 §5；部分线路如 DZ 定制公交为空） | `"{%22rule_group%22:...}"` |
| `loop` | string | 是否环线，`0` = 否，`1` = 是 | `"0"` |
| `status` | string | 线路运营状态 | `"1"` |
| `direc` | string | 反向（对向）线路的 id | `"310100015969"` |
| `company` | string / [] | 所属公交公司 ⚠️类型不稳定 | `"上海巴士集团一公司"` 或 `[]` |
| `distance` | string | 线路长度（公里） | `"19.021000"` |
| `basic_price` | string | 起步价（元） | `"2"` |
| `total_price` | string | 全程票价（元） | `"2"` |
| `bounds` | string | 线路外接矩形，`左下lng,lat;右上lng,lat` | `"121.503006,31.279996;121.617037,31.307971"` |
| `busstops` | array | 途经站点数组（详见 §4） | `[...]` |

---

## 4. busstops[] —— 站点字段

| 字段 | 类型 | 含义 | 示例 |
|---|---|---|---|
| `id` | string | 站点唯一 ID | `"BV10028366"` |
| `location` | string | 站点坐标，`lng,lat`（GCJ-02） | `"121.503928,31.306354"` |
| `name` | string | 站点名称 | `"国定路政立路"` |
| `sequence` | string | 站点在线路中的序号（从 1 起） | `"1"` |

> ⚠️ **高德站点数据已知问题**：高德对同名站点返回的是**同一个站点 `id`**，但同一物理站点却对应**多个邻近坐标值**（`location` 坐标不一致），实测坐标漂移可达数十米（约 30~50 米）。这一点上高德不如百度数据规整，不过百度无官方站点 ID、其站点 ID 由坐标拼接生成，ID 与坐标天然一一对应、自洽。
>
> 此外，高德数据**没有显式的「上行/下行」方向字段**：同一条线路的上下行各返回一条独立线路记录（独立 `id`，`name` 含起终点方向、`direc` 互指对向线路），站点通过所属线路的 `id` 关联到具体方向。本工具**原样保存高德返回的站点数据、不做任何聚合**，如需按物理站牌或上下行归并，可基于脚本输出 `line_stop_amap.txt` 中的 `route_id` / `route_name` / `station_id` 与坐标自行处理。

---

## 5. timedesc —— 发车时刻表详解

`timedesc` 是一个 **URL 编码的 JSON 字符串**，解码后是一套完整发车时刻表。

解码方式（Python）：

```python
import urllib.parse, json
timedesc = busline["timedesc"]
schedule = json.loads(urllib.parse.unquote(timedesc))
```

解码后的结构：

```json
{
  "rule_group": [
    {
      "date": {
        "day_type": ["all"],       // 适用日期类型：all / weekday / weekend 等
        "day_month": [],           // 适用日（月内第几天）
        "month": [],               // 适用月份
        "day_range": "",           // 日期范围
        "month_type": ["all"],     // 月份类型
        "day_week": [],            // 适用星期（1~7）
        "month_range": ""          // 月份范围
      },
      "time_group": [
        {
          "start_time": "05:30:00",  // 首班
          "end_time": "22:30:00",    // 末班
          "interval_time": "",       // 发车间隔（此组为空时看高峰/平峰组）
          "noon_type": ""            // 午间类型
        }
      ],
      "remark": "05:30-22:30 高峰20分钟/趟 平峰30分钟/趟",  // 可读描述
      "special_case": ""             // 特殊说明（节假日加班、临时调整等，通常为空）
    }
  ],
  "peak_time_group": [              // 高峰时段发车间隔
    {"start_time": "07:00:00", "end_time": "10:00:00", "interval_time": "00:20:00"},
    {"start_time": "17:00:00", "end_time": "19:00:00", "interval_time": "00:20:00"}
  ],
  "low_time_group": [],             // 低峰时段发车间隔（可能为空）
  "flat_time_group": [              // 平峰时段发车间隔
    {"start_time": "10:00:01", "end_time": "16:59:59", "interval_time": "00:30:00"}
  ],
  "allRemark": "05:30-22:30 高峰20分钟/趟 平峰30分钟/趟"   // 全天备注
}
```

各字段含义（按层级展开）：

**① 顶层字段（解码后 JSON 的 5 个键）**

| 字段 | 类型 | 含义 |
|---|---|---|
| `rule_group` | array | **班次规则组（核心）**，每个元素描述"某类日期 + 某时段"的运营规则 |
| `peak_time_group` | array | 高峰时段的发车间隔明细（非必填，定点线路为空） |
| `low_time_group` | array | 低谷时段的发车间隔明细（非必填，可能为空） |
| `flat_time_group` | array | 平峰时段的发车间隔明细（非必填，定点线路为空） |
| `allRemark` | string | 全天发车总备注（人类可读摘要） |

**② rule_group[] 元素字段（4 个）**

| 字段 | 类型 | 含义 |
|---|---|---|
| `date` | object | 该规则适用的**日期条件**（决定"哪天跑"） |
| `time_group` | array | 该日期条件下的**运营时段**（首末班 + 间隔） |
| `remark` | string | 该规则组的人类可读发车描述 |
| `special_case` | string | 特殊情况说明（节假日加班、临时调整等，通常为空） |

**③ date 对象字段（7 个）**

| 字段 | 类型 | 含义 |
|---|---|---|
| `day_type` | array | 日期类型：`weekday`=工作日、`weekend`=周末、`all`=全部 |
| `day_month` | array | 指定月份内的具体日期（如 `["1","15"]`），空 = 不限定 |
| `month` | array | 指定月份（如 `["1","2"]` 表示 1、2 月），空 = 不限定 |
| `day_range` | string | 日期范围（如 `"20261001-20261007"` 假期），空 = 不限 |
| `month_type` | array | 月份类型：`all` = 全年 |
| `day_week` | array | 星期几，**`"1"`~`"7"` 对应周一~周日** |
| `month_range` | string | 月份范围（如 `"6-9"` 表示 6~9 月），空 = 不限 |

**④ time_group[] 元素字段（4 个）**

| 字段 | 类型 | 含义 |
|---|---|---|
| `start_time` | string | 时段开始时间（HH:MM:SS） |
| `end_time` | string | 时段结束时间（HH:MM:SS），**与 start_time 相同 = 单班定点发车** |
| `interval_time` | string | 发车间隔（HH:MM:SS），空 = 定点 / 不按固定间隔 |
| `noon_type` | string | 午间类型标记（通常为空） |

**⑤ peak / low / flat_time_group[] 元素字段（3 个，三组结构一致）**

| 字段 | 类型 | 含义 |
|---|---|---|
| `start_time` | string | 该时段开始时间 |
| `end_time` | string | 该时段结束时间 |
| `interval_time` | string | 该时段发车间隔（如 `00:20:00` = 20 分钟/趟） |

> 判断逻辑：**日期规则 `date` 决定「哪天跑」→ 时段规则 `time_group` 决定「什么时间运营」→ 高峰/平峰/低谷组决定「间隔多久一班」**，三层叠加即完整时刻表。

### 三种运营模式对比（406路 / 406路大站车 / DZ406路）

| 维度 | 406路（常规线） | 406路大站车（快线） | DZ406路（定制线） |
|---|---|---|---|
| `timedesc` | ✅ 有值 | ✅ 有值 | ❌ 空（`""`） |
| `date.day_type` | `["all"]` | `["weekday"]` | — |
| `date.day_week` | `[]`（每天） | `["1"~"5"]`（周一~周五） | — |
| `time_group` 起止 | `05:30:00` ~ `22:30:00` | `07:21:00` ~ `07:21:00`（单班） | — |
| `peak_time_group` | 早 07:00–10:00、晚 17:00–19:00，各 20 分钟/趟 | 空 | — |
| `flat_time_group` | 10:00:01–16:59:59，30 分钟/趟 | 空 | — |
| `allRemark` | 05:30-22:30 高峰20分钟/趟 平峰30分钟/趟 | 工作日 定点发车07:21 | 空 |
| **运营模式** | 全天分时发车 | 工作日定点一班 | 无固定班次（预约/按需） |

> ⚠️ **timedesc 可能为空**：并非所有线路都有 `timedesc`，尤其 DZ（定制）公交等线路，高德返回的字段值可能为空字符串或两个引号字符 `""`。下游做「发车频率」分析时，必须先判断非空再 `json.loads()`，否则会误判或报错。

> 抓取脚本已对 `timedesc` **自动完成「URL 解码 + JSON 解析」**，输出的是可直接读的 JSON 字符串（含中文、不再有 `%22` 等转义）。若需做「发车频率」分析，直接对该字段 `json.loads()` 即可。

---

## 6. 脚本输出字段对照

抓取脚本 `busline_crawler.py` 在保留 API 原始字段的基础上做了以下处理：

| 处理 | 说明 |
|---|---|
| 坐标转换 | 保留原始 GCJ-02，同时新增 **WGS84（EPSG:4326）** 坐标 |
| 字段去重 | `route_id` = `id`、`route_name` = `name`（值相同），脚本仅保留 `route_id` / `route_name` |
| 类型兜底 | `start_time`/`end_time`/`company` 等不稳定字段统一规范为文本 |
| timedesc 解码 | `timedesc` 自动「URL 解码 + JSON 解析」，保存为可读 JSON 字符串（含中文） |

线路输出新增字段：

| 字段 | 含义 |
|---|---|
| `line_name` | 线路简称（`name` 去掉括号部分） |
| `route_id` | 线路唯一 ID（= 原 `id`） |
| `route_name` | 线路全名（= 原 `name`） |
| `polyline_wgs84` | 轨迹坐标串（WGS84） |

站点输出新增字段：

| 字段 | 含义 |
|---|---|
| `station_id` / `station_name` / `station_index` | 站点 ID / 名称 / 序号（对应原 `id`/`name`/`sequence`） |
| `lng` / `lat` | 站点原始坐标（GCJ-02） |
| `lng_wgs84` / `lat_wgs84` | 站点转换后坐标（WGS84） |


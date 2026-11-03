#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公交线路抓取统一入口：一条命令跑高德或百度。

用法：
  busline amap   --city 上海 --lines "406路" --gpkg
  busline baidu  --city 上海 --lines "406路" --gpkg
  busline        # 不指定数据源时，从配置文件 configs/config.yaml 读取 provider 等参数

所有参数都可在配置文件 configs/config.yaml（模板见 configs/example.yaml）中设置，
命令行参数优先级高于配置文件、高于内置默认值（详见 README「配置文件」一节）。
可用 --config 指定配置文件路径，默认自动查找 configs/config.yaml，其次 configs/example.yaml。

密钥与检索参数分开存放：密钥放在 configs/keys.txt（amap=xxx / baidu=yyy，模板见
configs/keys.txt.example），也可用命令行 --amap-key / --baidu-ak 或系统环境变量覆盖：
  AMAP_KEY  高德 Key（多个逗号分隔，支持轮换）
  BAIDU_AK  百度浏览器端 AK
"""
import argparse
import os
import sys

from . import busline_crawler  # 高德核心库（零依赖）；百度库依赖 Playwright，延迟导入

BASE = os.path.dirname(os.path.abspath(__file__))  # 包目录 src/busline/


def _load_keys_file():
    """零依赖加载密钥文件 configs/keys.txt（「字段=值」行）到 os.environ。

    查找：从包目录向上逐级查找 configs/keys.txt；
    支持 amap=xxx（高德 key，多 key 逗号分隔）、baidu=yyy（百度浏览器端 AK）；
    # 开头为注释；已存在的环境变量优先（文件不覆盖命令行/export 已设的值）。
    """
    d = BASE
    path = None
    for _ in range(6):
        fp = os.path.join(d, "configs", "keys.txt")
        if os.path.isfile(fp):
            path = fp
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    if not path:
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip().lower()
                v = v.strip().strip('"').strip("'")
                env_key = {"amap": "AMAP_KEY", "baidu": "BAIDU_AK"}.get(k)
                if env_key and v and env_key not in os.environ:
                    os.environ[env_key] = v
    except OSError:
        pass


def _yaml_scalar(v):
    """把 YAML 标量字符串解析为 Python 值（str/int/float/bool/None）。"""
    v = v.strip()
    if not v:
        return None
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1]
    low = v.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", "~"):
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def _strip_inline_comment(line):
    """去掉行尾的 '# 注释'（忽略引号内的 #，且 # 前须为空白或行首）。"""
    in_s = in_d = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif ch == "#" and not in_s and not in_d and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def _load_config_yaml(path):
    """零依赖读取极简 YAML 配置（两层：顶层标量 + 缩进子段），返回 dict。

    仅覆盖本项目 configs/example.yaml 所用的结构：`key: value` 标量、两空格缩进的
    `section:` 子键；值为字符串/整数/浮点/布尔（多值请用逗号分隔字符串）。
    解析失败或文件不存在返回 {}，等价于全走内置默认值。
    """
    if not path or not os.path.isfile(path):
        return {}
    data = {}
    section = None
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.rstrip("\n")
                body = _strip_inline_comment(line)
                stripped = body.lstrip(" ")
                if not stripped or stripped.startswith("#"):
                    continue
                indent = len(body) - len(stripped)
                if ":" not in stripped:
                    continue
                key, _, val = stripped.partition(":")
                key = key.strip()
                if not key:
                    continue
                if indent == 0:
                    if val.strip() == "":
                        section = key
                        data.setdefault(key, {})
                    else:
                        section = None
                        data[key] = _yaml_scalar(val)
                else:
                    target = data.setdefault(section, {}) if section else data
                    if isinstance(target, dict):
                        target[key] = _yaml_scalar(val)
    except OSError:
        return {}
    return data


def _resolve_config_path(argv):
    """确定配置文件路径：--config/-c 显式指定优先，否则向上查找默认文件。

    默认查找顺序（在 configs/ 目录下）：config.yaml → config.yml → example.yaml。
    返回 (path, explicit)；explicit=True 表示用户显式传了 --config。
    """
    explicit_path = None
    for i, a in enumerate(argv):
        if a in ("--config", "-c"):
            if i + 1 < len(argv):
                explicit_path = argv[i + 1]
        elif a.startswith("--config="):
            explicit_path = a.split("=", 1)[1]
        elif a.startswith("-c="):
            explicit_path = a.split("=", 1)[1]
    if explicit_path:
        return explicit_path, True
    d = BASE
    for _ in range(6):
        for name in ("config.yaml", "config.yml", "example.yaml"):
            fp = os.path.join(d, "configs", name)
            if os.path.isfile(fp):
                return fp, False
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None, False


def _flat_cfg(cfg, provider):
    """把配置合并成该 provider 的扁平参数字典（key 即 argparse dest 名）。

    通用参数（city/outdir/密钥）在顶层，专属参数在 amap:/baidu: 段。
    值为 None 的键剔除，避免覆盖 argparse 内置默认值。
    """
    key_name = {"amap": "amap_key", "baidu": "baidu_ak"}[provider]
    merged = {
        "city": cfg.get("city"),
        "outdir": cfg.get("outdir"),
        key_name: cfg.get(key_name),
    }
    sect = cfg.get(provider) or {}
    if isinstance(sect, dict):
        merged.update(sect)
    return {k: v for k, v in merged.items() if v is not None}


def main():
    cfg_path, cfg_explicit = _resolve_config_path(sys.argv[1:])
    if cfg_explicit and not os.path.isfile(cfg_path):
        sys.exit("错误：配置文件不存在：%s" % cfg_path)
    cfg = _load_config_yaml(cfg_path)
    if cfg_path and cfg:
        print(">> 读取配置：%s" % cfg_path)

    ap = argparse.ArgumentParser(
        prog="busline",
        description="公交线路抓取统一入口（高德/百度）。参数默认值来自配置文件"
                    "（configs/config.yaml / configs/example.yaml，可用 --config 指定），命令行参数可覆盖。")
    ap.add_argument("-c", "--config", default="",
                    help="配置文件路径（默认自动查找 configs/config.yaml，其次 configs/example.yaml）")
    sub = ap.add_subparsers(dest="provider")

    ac = _flat_cfg(cfg, "amap")
    bc = _flat_cfg(cfg, "baidu")

    # ---------- 高德子命令 ----------
    pa = sub.add_parser("amap", help="高德地图（零依赖）")
    pa.add_argument("--city", default=ac.get("city", "上海"), help="城市名")
    pa.add_argument("--amap-key", default=ac.get("amap_key", ""),
                    help="高德 Key，多 key 逗号分隔（默认读 configs/keys.txt 的 AMAP_KEY）")
    pa.add_argument("--lines", default=ac.get("lines", ""), help="指定线路，逗号分隔")
    pa.add_argument("--prefix", default=ac.get("prefix", ""),
                    help="线路名前缀，逗号分隔；与 --lines 组合（如 浦东 + 102路 = 浦东102路）")
    pa.add_argument("--suffix", default=ac.get("suffix", ""),
                    help="数字后缀，逗号分隔；数字穷举模式与 --prefix 组合（如 浦东 + N + 路）")
    pa.add_argument("--extra", default=ac.get("extra", ""),
                    help="额外特殊关键字，逗号分隔（留空则不追加，默认值在配置文件 extra 字段）")
    pa.add_argument("--only", default=ac.get("only", ""),
                    help="只检索指定关键字，逗号分隔（不做数字穷举）")
    pa.add_argument("--exact", action="store_true", default=ac.get("exact", False),
                    help="精确检索：只保留短名与关键字完全一致的线路（默认关闭=模糊检索）")
    pa.add_argument("--max-num", type=int, default=ac.get("max_num", 1000))
    pa.add_argument("--max-qps", type=float, default=ac.get("max_qps", 28.0))
    pa.add_argument("--delay", type=float, default=ac.get("delay", 0.0),
                    help="每次请求间隔秒数")
    pa.add_argument("--gpkg", action="store_true", default=ac.get("gpkg", False))
    pa.add_argument("--geojson", action="store_true", default=ac.get("geojson", False))
    pa.add_argument("--outdir", default=ac.get("outdir", "runs"),
                    help="根输出目录（数据写入其下按命名规则生成的子文件夹）")

    # ---------- 百度子命令 ----------
    pb = sub.add_parser("baidu", help="百度地图（需 Playwright）")
    pb.add_argument("--city", default=bc.get("city", "上海"), help="城市名")
    pb.add_argument("--baidu-ak", default=bc.get("baidu_ak", ""),
                    help="百度浏览器端 AK（默认读 configs/keys.txt 的 BAIDU_AK）")
    pb.add_argument("--lines", default=bc.get("lines", ""), help="指定线路，逗号分隔")
    pb.add_argument("--prefix", default=bc.get("prefix", ""),
                    help="线路名前缀，逗号分隔；与 --lines 组合（如 浦东 + 102路 = 浦东102路）")
    pb.add_argument("--suffix", default=bc.get("suffix", ""),
                    help="数字后缀，逗号分隔；数字穷举模式与 --prefix 组合（如 浦东 + N + 路）")
    pb.add_argument("--extra", default=bc.get("extra", ""),
                    help="额外特殊关键字，逗号分隔（留空则不追加，默认值在配置文件 extra 字段）")
    pb.add_argument("--only", default=bc.get("only", ""),
                    help="只检索指定关键字，逗号分隔（不做数字穷举）")
    pb.add_argument("--exact", action="store_true", default=bc.get("exact", False),
                    help="精确检索：只保留短名与关键字完全一致的线路（默认关闭=模糊检索）")
    pb.add_argument("--max-num", type=int, default=bc.get("max_num", 1000))
    pb.add_argument("--delay-ms", type=int, default=bc.get("delay_ms", 1000))
    pb.add_argument("--timeout", type=int, default=bc.get("timeout", 0),
                    help="抓取总超时秒数（0=自动按关键字数计算，保证全量穷举不被截断）")
    pb.add_argument("--headed", action="store_true", default=bc.get("headed", False),
                    help="显示浏览器窗口")
    pb.add_argument("--gpkg", action="store_true", default=bc.get("gpkg", False))
    pb.add_argument("--outdir", default=bc.get("outdir", "runs"),
                    help="根输出目录（数据写入其下按命名规则生成的子文件夹）")

    args = ap.parse_args()

    # 数据源：命令行子命令优先，其次配置文件 provider
    cli_provider = args.provider
    provider = cli_provider or cfg.get("provider")
    if not provider:
        sys.exit("错误：未指定数据源。请用 busline amap 或 busline baidu，"
                 "或在配置文件中设置 provider。")

    # 未传子命令时，参数全部取自配置文件（此时命令行仅 --config 可用）
    if cli_provider is None:
        params = _flat_cfg(cfg, provider)
    else:
        params = vars(args)

    _load_keys_file()  # 自动读取 configs/keys.txt 中的 AMAP_KEY / BAIDU_AK

    print(">> 分发到：%s" % provider)

    if provider == "amap":
        keys = params.get("amap_key", "") or os.environ.get("AMAP_KEY", "")
        if not keys:
            sys.exit("错误：未提供高德 Key（--amap-key / configs/keys.txt 的 AMAP_KEY / 系统环境变量 AMAP_KEY）")
        params["amap_key"] = keys  # 保证 run() 拿到完整 key 串（可能来自环境变量）
        busline_crawler.run(params)
    else:
        ak = params.get("baidu_ak", "") or os.environ.get("BAIDU_AK", "")
        if not ak:
            sys.exit("错误：未提供百度 AK（--baidu-ak / configs/keys.txt 的 BAIDU_AK / 系统环境变量 BAIDU_AK）")
        params["baidu_ak"] = ak
        from . import baidu_busline_playwright  # 延迟导入（依赖 Playwright，仅百度需要）
        baidu_busline_playwright.run(params)


if __name__ == "__main__":
    main()

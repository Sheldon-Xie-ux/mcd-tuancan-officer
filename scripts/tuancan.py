#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
麦团餐官 · 企业团餐方案引擎

一句话需求 → 一份可以直接发客户的团餐方案与报价单。

它解决的不是"查菜单"，而是团餐真正会卡住的四件事：
  1. 这家店能不能接、什么时段能接（团餐预约时段和麦乐送不一样）
  2. 要的助餐服务来不来得及（专人分餐/专人摆台需提前 4 小时）
  3. 人均预算怎么落到具体餐品上（官方配比规则）
  4. 三档助餐服务分别多少钱（档位直接改折扣，客户一定会问）

用法：
    export MCD_MCP_TOKEN="..."
    python3 scripts/tuancan.py stores  --address-id <id>
    python3 scripts/tuancan.py plan --address-id <id> --people 20 --per-head 40 \\
        --at "2026-10-26 12:00" --scene 会议
"""

import argparse
import datetime as dt
import itertools
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcd_client import MCPClient, MCPError  # noqa: E402

BE_TYPE_TUANCAN = 6
ORDER_TYPE_DELIVERY = 2

# 助餐服务档位。经验：档位不只换服务，还直接换折扣。
TIERS = [
    ("GMS001", "保鲜速达", "准时送达 + 餐品保温", "加班餐、下午茶、预算优先"),
    ("GMS002", "专人分餐", "准时送达 + 餐品保温 + 送餐到人/到桌", "会议、培训，不打断节奏"),
    ("GMS003", "专人摆台", "准时送达 + 餐品保温 + 餐品摆台 + 专人服务", "团建、发布会、有客户在场"),
]

SCENE_DEFAULT_TIER = {
    "会议": "GMS002",
    "培训": "GMS002",
    "团建": "GMS003",
    "发布会": "GMS003",
    "加班餐": "GMS001",
    "下午茶": "GMS001",
}

# 专人服务提前量（小时），由 query-meal-assistance 的 unusableReason 返回
LEAD_HOURS_PERSONNEL = 4

# 官方配比：人均预算 → 组合结构（来自 query-meals 工具说明）
def combo_structure(per_head):
    if per_head < 20:
        return {"小食": 1}
    if per_head < 30:
        return {"主食": 1, "小食": 1}
    if per_head < 40:
        return {"主食": 1, "小食": 1, "饮品": 1}
    if per_head < 50:
        return {"主食": 1, "小食": 2, "饮品": 1}
    return {"主食": 1, "小食": 2, "饮品": 2}


# 从最丰富到最精简的结构阶梯。
# 官方区间是按典型客单价写的，真实门店价格会漂移，
# 所以最终结构按"预算能撑住的最丰富一档"来定，而不是死抠区间。
STRUCTURE_LADDER = [
    ("丰盛双饮", {"主食": 1, "小食": 2, "饮品": 2}),
    ("加量单饮", {"主食": 1, "小食": 2, "饮品": 1}),
    ("标准三件", {"主食": 1, "小食": 1, "饮品": 1}),
    ("简餐两件", {"主食": 1, "小食": 1}),
    ("小食单点", {"小食": 1}),
]

# 下午茶不卖正餐结构。同一笔预算，"一份汉堡+一份苹果片"和
# "一份鸡翅+一杯咖啡"是两种东西，场景必须能改结构。
SCENE_LADDER = {
    "下午茶": [
        ("茶歇双饮", {"小食": 2, "饮品": 2}),
        ("茶歇加量", {"小食": 2, "饮品": 1}),
        ("茶歇标配", {"小食": 1, "饮品": 1}),
        ("小食双拼", {"小食": 2}),
        ("小食单点", {"小食": 1}),
    ],
}

# 蘸酱、加购、打包这类不能当"小食"算，必须从组合池里剔掉
EXCLUDE_IN_COMBO = ("酱", "蘸", "加购", "打包", "优惠", "升级", "小料", "餐具", "餐盒", "服务费")

# 团餐选品的"像人话"程度。排在前面的先选，价格只在同档内用来贴预算。
# 这张表就是纯价格算法和"懂团餐的人搭出来的单"之间的差别。
PREFERENCE = {
    "主食": ["巨无霸", "板烧", "麦辣鸡腿堡", "双牛堡", "麦香鸡", "麦香鱼",
             "安格斯", "吉士汉堡", "汉堡包", "鸡腿堡", "鸡排堡", "卷"],
    "小食": ["薯条", "麦辣鸡翅", "麦乐鸡", "那么大鸡排", "鸡块", "香芋派",
             "菠萝派", "麦旋风", "玉米杯", "苹果片"],
    "饮品": ["可口可乐", "雪碧", "纯悦", "冰红茶", "柠檬茶", "零度", "咖啡",
             "拿铁", "美汁源", "橙汁", "牛奶"],
}


def pref_rank(cat, name):
    """越小越优先；不认识的品类排在最后，仍然可参选。"""
    pool = PREFERENCE.get(cat) or []
    name = name or ""
    for i, token in enumerate(pool):
        if token in name:
            return i
    return len(pool)


def _clean_name(name):
    return (name or "").strip()


def yuan(fen):
    """接口金额单位是分。"""
    try:
        return round(float(fen) / 100.0, 2)
    except (TypeError, ValueError):
        return 0.0


def fmt_money(fen):
    return "¥%s" % ("%.2f" % yuan(fen))


class CateringError(RuntimeError):
    pass


class CateringOfficer(object):
    def __init__(self, client=None):
        self.mcp = client or MCPClient()

    # ---------------- 门店与时段 ----------------

    def addresses(self):
        data = self.mcp.call_tool_data("delivery-query-addresses", {})
        return (data.get("data") or data or {}).get("addresses", []) \
            if isinstance(data, dict) else []

    def stores(self, address_id, be_type=BE_TYPE_TUANCAN):
        data = self.mcp.call_tool_data(
            "delivery-query-stores", {"addressId": address_id, "beType": be_type}
        )
        inner = data.get("data") if isinstance(data, dict) else data
        return inner or []

    @staticmethod
    def pick_store(stores, keyword=None, store_code=None):
        if store_code:
            for s in stores:
                if str(s.get("storeCode")) == str(store_code):
                    return s
            raise CateringError("该地址下没有门店 %s" % store_code)
        if keyword:
            for s in stores:
                if keyword in (s.get("storeName") or ""):
                    return s
        if not stores:
            raise CateringError("该地址下没有可做企业团餐的门店")
        return stores[0]

    @staticmethod
    def reservation_check(store, when):
        """团餐只能约在门店开放团餐的时段内，返回命中的那个时段。"""
        day = when.strftime("%Y-%m-%d")
        hhmm = when.strftime("%H:%M")
        for opt in store.get("reservationTimeOptions") or []:
            if opt.get("date") != day:
                continue
            text = opt.get("reservationOptionText", "")
            for seg in text.split("，"):
                seg = seg.strip()
                if "(" not in seg:
                    continue
                label, span = seg.split("(", 1)
                span = span.rstrip(")")
                for part in span.split(","):
                    if "至" not in part:
                        continue
                    start, end = [x.strip() for x in part.split("至", 1)]
                    if start <= hhmm <= end:
                        return True, "%s %s %s" % (day, label, part)
            return False, "%s 当天只开放：%s" % (day, text)
        return False, "该门店当日不开放团餐预约，最近可约：%s" % ", ".join(
            o.get("date", "") for o in (store.get("reservationTimeOptions") or [])[:3]
        )

    # ---------------- 助餐服务 ----------------

    def assistance(self, store, when):
        args = {
            "storeCode": str(store["storeCode"]),
            "beType": BE_TYPE_TUANCAN,
            "orderType": ORDER_TYPE_DELIVERY,
            "beCode": store.get("beCode"),
            "reservationDate": when.strftime("%Y-%m-%d %H:%M"),
        }
        data = self.mcp.call_tool_data("query-meal-assistance", args)
        inner = data.get("data") if isinstance(data, dict) else data
        return (inner or {}).get("mealAssistanceItems", [])

    # ---------------- 菜单与价格 ----------------

    def menu_codes(self, store, when):
        args = {
            "storeCode": str(store["storeCode"]),
            "beType": BE_TYPE_TUANCAN,
            "orderType": ORDER_TYPE_DELIVERY,
            "beCode": store.get("beCode"),
            "reservationDate": when.strftime("%Y-%m-%d %H:%M"),
        }
        data = self.mcp.call_tool_data("query-meals", args)
        inner = data.get("data") if isinstance(data, dict) else data
        out = {}
        for cat in (inner or {}).get("categories", []):
            name = cat.get("name") or ""
            bucket = self._bucket(name)
            if not bucket:
                continue
            out.setdefault(bucket, [])
            for meal in cat.get("meals") or []:
                code = meal.get("code")
                if code and code not in out[bucket]:
                    out[bucket].append(code)
        return out

    @staticmethod
    def _bucket(category_name):
        n = category_name or ""
        if "主食" in n or "汉堡" in n or "卷" in n:
            return "主食"
        if "小食" in n or "甜品" in n or "炸鸡" in n or "薯" in n or "派" in n:
            return "小食"
        if "饮" in n or "咖啡" in n or "茶" in n:
            return "饮品"
        return None

    def probe_prices(self, store, when, codes, batch=12):
        """批量钓价：菜单接口不给价格，但算价接口会把每个商品的单价吐回来。

        一次最多传十几个商品，比逐个 query-meal-detail 省一个数量级的请求。
        """
        base = {
            "storeCode": str(store["storeCode"]),
            "beType": BE_TYPE_TUANCAN,
            "orderType": ORDER_TYPE_DELIVERY,
            "beCode": store.get("beCode"),
            "reservationDate": when.strftime("%Y-%m-%d %H:%M"),
            "gmServiceCode": "GMS001",
        }
        result = {}
        codes = [c for c in codes if c]
        for i in range(0, len(codes), batch):
            chunk = codes[i:i + batch]
            items = [{"productCode": c, "quantity": 1} for c in chunk]
            try:
                data = self.mcp.call_tool_data("calculate-price", dict(base, items=items))
            except MCPError as exc:
                if "429" in str(exc):
                    raise CateringError("触发限流（600 次/分钟），请降低频率") from None
                raise
            inner = data.get("data") if isinstance(data, dict) else data
            for p in (inner or {}).get("productList", []):
                code = str(p.get("productCode"))
                if code in chunk:
                    result[code] = {
                        "name": p.get("productName"),
                        "price": p.get("originalSubtotal") or 0,
                    }
        return result

    def quote(self, store, when, items, gm_service_code):
        args = {
            "storeCode": str(store["storeCode"]),
            "beType": BE_TYPE_TUANCAN,
            "orderType": ORDER_TYPE_DELIVERY,
            "beCode": store.get("beCode"),
            "reservationDate": when.strftime("%Y-%m-%d %H:%M"),
            "gmServiceCode": gm_service_code,
            "items": items,
        }
        data = self.mcp.call_tool_data("calculate-price", args)
        return data.get("data") if isinstance(data, dict) else data

    # ---------------- 组合挑选 ----------------

    @staticmethod
    def _group_pool(price_map):
        """把钓回来的价格按品类归集，剔掉蘸酱/加购这类非餐品，同名去重。"""
        groups = {}
        seen = set()
        for code, info in price_map.items():
            cat = info.get("category")
            name = _clean_name(info.get("name"))
            if not cat or not name:
                continue
            if any(k in name for k in EXCLUDE_IN_COMBO):
                continue
            key = (cat, name)
            if key in seen:
                continue
            seen.add(key)
            groups.setdefault(cat, []).append(
                {"productCode": code, "name": name, "unitPrice": info.get("price") or 0})
        for cat in groups:
            # 先按"像不像团餐该有的东西"排，同档内价格从高到低（好贴预算）
            groups[cat].sort(key=lambda x: (pref_rank(cat, x["name"]), -x["unitPrice"]))
        return groups

    @staticmethod
    def _min_cost(groups, structure):
        cost = 0
        for cat, n in structure.items():
            pool = groups.get(cat) or []
            if len(pool) < n:
                return None
            cost += sum(x["unitPrice"] for x in sorted(pool, key=lambda y: y["unitPrice"])[:n])
        return cost

    @classmethod
    def choose_structure(cls, groups, budget_fen, scene="会议"):
        """取预算能撑住的最丰富一档；撑不住就一路降级。"""
        ladder = SCENE_LADDER.get(scene) or STRUCTURE_LADDER
        for label, structure in ladder:
            cost = cls._min_cost(groups, structure)
            if cost is not None and cost <= budget_fen:
                return label, structure
        return ladder[-1][0], ladder[-1][1]

    @classmethod
    def build_combo(cls, budget_fen, groups, structure):
        """在人均预算内挑一套组合。

        结构最多 5 件、每类候选十几个，直接穷举反而比贪心更稳：
        先按"像不像团餐该点的东西"排序质量，同质量再比谁更贴预算。
        """
        need = [(cat, n) for cat, n in structure.items() if n > 0]
        if not need:
            return [], 0

        cand_lists = []
        for cat, n in need:
            pool = groups.get(cat) or []
            if len(pool) < n:
                return [], 0
            merged = {}
            keep = pool[:8] + sorted(pool, key=lambda y: y["unitPrice"])[:2]
            for x in keep:
                merged[x["productCode"]] = x
            cand = sorted(merged.values(),
                          key=lambda y: (pref_rank(cat, y["name"]), -y["unitPrice"]))[:10]
            cand_lists.append(list(itertools.combinations(cand, n)))

        best = None
        for choice in itertools.product(*cand_lists):
            items = []
            for (cat, _n), part in zip(need, choice):
                for x in part:
                    items.append(dict(x, category=cat))
            codes = [x["productCode"] for x in items]
            if len(set(codes)) != len(codes):
                continue
            total = sum(x["unitPrice"] for x in items)
            if total > budget_fen:
                continue
            quality = -sum(pref_rank(x["category"], x["name"]) for x in items)
            key = (quality, total)
            if best is None or key > best[0]:
                best = (key, items, total)

        if best is None:
            # 预算实在撑不住结构，退回最便宜的一套，并让上层据实提示
            items = []
            total = 0
            for cat, n in need:
                for x in sorted(groups.get(cat) or [], key=lambda y: y["unitPrice"])[:n]:
                    items.append(dict(x, category=cat))
                    total += x["unitPrice"]
            return items, total
        return best[1], best[2]

    # ---------------- 方案 ----------------

    def plan(self, address_id, when, people, per_head, scene="会议",
             keyword=None, store_code=None, tier=None):
        now = dt.datetime.now()
        lead = (when - now).total_seconds() / 3600.0

        stores = self.stores(address_id)
        store = self.pick_store(stores, keyword=keyword, store_code=store_code)
        ok_window, window_text = self.reservation_check(store, when)
        services = self.assistance(store, when)

        usable = {}
        for s in services:
            code = s.get("gmServiceCode")
            can = bool(s.get("enable"))
            reason = s.get("unusableReason") or ""
            if not can and code != "GMS001" and lead < LEAD_HOURS_PERSONNEL:
                reason = "距离开餐只剩 %.1f 小时，%s 需提前 %d 小时" % (
                    lead, s.get("gmServiceName"), LEAD_HOURS_PERSONNEL)
            usable[code] = {"name": s.get("gmServiceName"), "can": can,
                            "reason": reason, "items": s.get("serviceItems") or []}

        wanted = tier or SCENE_DEFAULT_TIER.get(scene, "GMS001")
        if wanted not in usable or not usable[wanted]["can"]:
            if not usable.get("GMS001", {}).get("can"):
                raise CateringError("该门店当前没有可用的助餐服务")
            fallback = "GMS001"
        else:
            fallback = wanted

        codes = self.menu_codes(store, when)
        pool = []
        for cat in ("主食", "小食", "饮品"):
            pool.extend((codes.get(cat) or [])[:16])
        price_map = self.probe_prices(store, when, pool)
        for cat in ("主食", "小食", "饮品"):
            for c in codes.get(cat) or []:
                if c in price_map:
                    price_map[c]["category"] = cat

        groups = self._group_pool(price_map)
        struct_label, structure = self.choose_structure(groups, per_head * 100, scene)
        picks, per_unit = self.build_combo(per_head * 100, groups, structure)

        items = [{"productCode": p["productCode"], "quantity": people} for p in picks]
        quotes = {}
        for code, _name, _desc, _use in TIERS:
            if not usable.get(code, {}).get("can") and code != fallback:
                quotes[code] = None
                continue
            try:
                q = self.quote(store, when, items, code)
                quotes[code] = q
            except MCPError:
                quotes[code] = None

        return {
            "generatedAt": now.strftime("%Y-%m-%d %H:%M"),
            "leadHours": round(lead, 1),
            "store": store,
            "windowOk": ok_window,
            "windowText": window_text,
            "services": usable,
            "scene": scene,
            "recommendedTier": fallback,
            "people": people,
            "perHeadBudget": per_head,
            "structure": structure,
            "structureLabel": struct_label,
            "picks": picks,
            "perUnitFen": per_unit,
            "quotes": quotes,
            "when": when.strftime("%Y-%m-%d %H:%M"),
        }

    # ---------------- 渲染 ----------------

    def render(self, plan):
        L = []
        a = L.append
        s = plan["store"]
        q_main = plan["quotes"].get(plan["recommendedTier"]) or {}

        a("# 团餐方案 · %s" % s.get("storeName", ""))
        a("")
        a("> 生成时间 %s ｜ 数据来源：麦当劳 MCP 实时查询" % plan["generatedAt"])
        a("")
        a("**需求**：%d 人 · 人均预算 %d 元 · %s 送达 · %s场景"
          % (plan["people"], plan["perHeadBudget"], plan["when"], plan["scene"]))
        a("")
        if q_main:
            a("**结论**：可以接。推荐「%s」档，%d 人合计 **%s**，人均 **¥%.2f**。"
              % (plan["services"][plan["recommendedTier"]]["name"], plan["people"],
                 fmt_money(q_main.get("price")),
                 yuan(q_main.get("price")) / max(plan["people"], 1)))
        else:
            a("**结论**：可以接，但推荐档位当前不可约，见下方替代方案。")
        a("")

        a("## 一、可行性")
        a("")
        a("- 门店：%s（storeCode %s）" % (s.get("storeName"), s.get("storeCode")))
        a("- 营业时间：%s—%s" % (s.get("businessStartTime", "?"), s.get("businessEndTime", "?")))
        a("- 团餐预约时段：%s" % ("✅ " + plan["windowText"] if plan["windowOk"] else "❌ " + plan["windowText"]))
        a("- 距离开餐：%.1f 小时" % plan["leadHours"])
        a("")

        a("## 二、助餐服务对比")
        a("")
        a("团餐的助餐服务不是加钱买服务，**档位本身会改变折扣**。同 20 人同菜品，三档价格差在这里：")
        a("")
        a("| 档位 | 含什么 | 应付 | 人均 | 与基础档差 | 可约 |")
        a("|---|---|---|---|---|---|")
        base_price = None
        for code, name, desc, _use in TIERS:
            q = plan["quotes"].get(code)
            info = plan["services"].get(code) or {}
            if q:
                total = q.get("price") or 0
                if base_price is None:
                    base_price = total
                diff = total - base_price
                diff_text = "—" if diff == 0 else "+%.2f 元（人均 +%.2f）" % (
                    yuan(diff), yuan(diff) / max(plan["people"], 1))
                a("| %s | %s | %s | ¥%.2f | %s | %s |" % (
                    name, desc, fmt_money(total),
                    yuan(total) / max(plan["people"], 1), diff_text,
                    "✅" if info.get("can") else "⚠️ " + (info.get("reason") or "不可约")))
            else:
                a("| %s | %s | 不可约 | — | — | ⚠️ %s |" % (
                    name, desc, info.get("reason") or "当前不可预约"))
        a("")

        a("## 三、推荐组合")
        a("")
        a("按预算能撑住的配比「%s」：%s。餐品**人均原价 ¥%.2f**，预算 ¥%d，占用 %.0f%%。" % (
            plan["structureLabel"],
            "，".join("%s×%d" % (k, v) for k, v in plan["structure"].items() if v),
            yuan(plan["perUnitFen"]), plan["perHeadBudget"],
            yuan(plan["perUnitFen"]) / max(plan["perHeadBudget"], 1) * 100))
        a("")
        a("| 餐品 | 单价 | 数量 | 小计 |")
        a("|---|---|---|---|")
        for p in plan["picks"]:
            a("| %s | ¥%.2f | %d | ¥%.2f |" % (
                p["name"], yuan(p["unitPrice"]), plan["people"],
                yuan(p["unitPrice"]) * plan["people"]))
        a("")

        if q_main:
            a("## 四、报价")
            a("")
            a("| 项目 | 金额 |")
            a("|---|---|")
            a("| 商品原价 | %s |" % fmt_money(q_main.get("productOriginalPrice")))
            a("| 助餐服务折扣 | -%s |" % fmt_money(
                (q_main.get("productOriginalPrice") or 0) - (q_main.get("productPrice") or 0)))
            a("| 打包服务费 | %s |" % fmt_money(q_main.get("packingPrice")))
            a("| 配送费 | %s |" % fmt_money(q_main.get("deliveryPrice")))
            a("| **应付合计** | **%s** |" % fmt_money(q_main.get("price")))
            a("")

            enjoyed = q_main.get("enjoyed") or {}
            able = q_main.get("enjoyable") or {}
            a("## 五、还能再省 / 再升级的点")
            a("")
            if enjoyed.get("realDiscount"):
                a("- 本单已享 **%s 折**（%s）" % (enjoyed["realDiscount"], enjoyed.get("promotionName") or ""))
            if able.get("realDiscount") and able.get("balance"):
                a("- 再点 **%s**，可升到 **%s 折**" % (fmt_money(able["balance"]), able["realDiscount"]))
            personnel = [c for c in ("GMS002", "GMS003") if plan["services"].get(c, {}).get("can")]
            if plan["leadHours"] < LEAD_HOURS_PERSONNEL:
                a("- ⚠️ **只剩下 %.1f 小时**，专人分餐/专人摆台需提前 %d 小时，今天只能走保鲜速达。"
                  % (plan["leadHours"], LEAD_HOURS_PERSONNEL))
            elif personnel and plan["recommendedTier"] == "GMS001":
                a("- 距开餐还有 %.1f 小时，若要升级专人分餐/摆台，现在改还来得及。"
                  % plan["leadHours"])
            a("")

        a("## 六、执行清单")
        a("")
        a("- [ ] 确认门店与送达时段：%s" % plan["when"])
        a("- [ ] 确认助餐档位：%s" % plan["services"][plan["recommendedTier"]]["name"])
        a("- [ ] 下单前再跑一次算价（优惠券/活动价会变）")
        a("- [ ] 企业结算：如需发票，提前和门店确认开票信息与抬头")
        a("- [ ] 到货签收：核对份数、保温状态，异常当场拍照")
        a("")
        a("---")
        a("")
        a("以上价格与库存为麦当劳 MCP 接口实时返回，最终以官方渠道下单页为准。")
        return "\n".join(L)


# ---------------- CLI ----------------

def parse_when(text):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%m-%d %H:%M", "%H:%M"):
        try:
            parsed = dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%m-%d %H:%M":
            parsed = parsed.replace(year=dt.date.today().year)
        elif fmt == "%H:%M":
            parsed = dt.datetime.combine(dt.date.today(), parsed.time())
            if parsed < dt.datetime.now():
                parsed += dt.timedelta(days=1)
        return parsed
    raise SystemExit("时间格式不对，用 '2026-10-26 12:00'")


def main(argv=None):
    p = argparse.ArgumentParser(description="麦团餐官 · 企业团餐方案引擎")
    sub = p.add_subparsers(dest="cmd")

    ps = sub.add_parser("stores", help="列出该地址可做企业团餐的门店")
    ps.add_argument("--address-id", required=True)

    pa = sub.add_parser("addresses", help="列出账号下的配送地址")

    pp = sub.add_parser("plan", help="生成团餐方案")
    pp.add_argument("--address-id", required=True)
    pp.add_argument("--people", type=int, required=True)
    pp.add_argument("--per-head", type=int, required=True, help="人均预算（元）")
    pp.add_argument("--at", required=True, help='送达时间，如 "2026-10-26 12:00"')
    pp.add_argument("--scene", default="会议", choices=sorted(SCENE_DEFAULT_TIER))
    pp.add_argument("--keyword", default=None, help="门店名关键词")
    pp.add_argument("--store-code", default=None)
    pp.add_argument("--tier", default=None, choices=[t[0] for t in TIERS])
    pp.add_argument("--out", default=None)

    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 2

    officer = CateringOfficer()
    try:
        if args.cmd == "addresses":
            for a in officer.addresses():
                print("%-34s %s" % (a.get("addressId"), a.get("fullAddress")))
            return 0

        if args.cmd == "stores":
            for s in officer.stores(args.address_id):
                print("%-9s %-9s %s" % (s.get("storeCode"), s.get("beCode"), s.get("storeName")))
            return 0

        plan = officer.plan(
            address_id=args.address_id,
            when=parse_when(args.at),
            people=args.people,
            per_head=args.per_head,
            scene=args.scene,
            keyword=args.keyword,
            store_code=args.store_code,
            tier=args.tier,
        )
        md = officer.render(plan)
        if args.out:
            path = args.out if os.path.isabs(args.out) else os.path.join(os.getcwd(), args.out)
            os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
            with open(path, "w", encoding="utf-8") as f:
                f.write(md)
            print("已写入 %s" % path)
        else:
            print(md)
        return 0
    except (MCPError, CateringError) as exc:
        print("出错了：%s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

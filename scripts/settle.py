#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
麦团餐官 · 结算与对账引擎

团餐的事前是报价，事后是结算。这个脚本管事后那一半：

  1. 金额核对：商品行加总、配送费、折扣、券、实付，五项对平
  2. 分摊：按部门或人头拆账，用最大余额法分尾差，合计与实付分毫不差
  3. 漏领检查：课后评价券、账户里快过期的券，能领没领的都揪出来
  4. 月度汇总：一个月点了多少、折扣率多少、钱花在哪几家店
  5. 异常登记：少送/迟到/替换，生成可直接发门店的索赔材料清单

用法：
    export MCD_MCP_TOKEN="..."
    python3 scripts/settle.py order --order-id <id>
    python3 scripts/settle.py order --order-id <id> --shares "研发:12,市场:5,行政:3"
    python3 scripts/settle.py month --month 2026-10
"""

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcd_client import MCPClient, MCPError  # noqa: E402


def money(value):
    """接口金额是字符串，统一转成两位小数的浮点。"""
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def fen(value):
    return int(round(money(value) * 100))


def yuan_str(cents):
    return "¥%.2f" % (cents / 100.0)


def parse_shares(text):
    """"研发:12,市场:5" -> [{"name": "研发", "weight": 12}, ...]"""
    out = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part or "：" in part:
            name, weight = part.replace("：", ":").split(":", 1)
            name = name.strip()
            weight = float(weight or 1)
        else:
            name, weight = part, 1.0
        if weight <= 0:
            raise SystemExit("分摊权重必须是正数：%s" % part)
        out.append({"name": name, "weight": weight})
    if not out:
        raise SystemExit("分摊格式不对，用 \"研发:12,市场:5\"")
    return out


def split_amount(total_cents, shares):
    """按权重分摊，用最大余额法分尾差。

    财务不认"四舍五入后加起来差一分钱"的账，所以尾差必须由某一方吃掉，
    而且吃尾差的规则要固定、可解释——这里按小数部分从大到小依次补 1 分。
    """
    weight_sum = sum(s["weight"] for s in shares)
    if weight_sum <= 0:
        raise SystemExit("分摊权重合计必须大于 0")

    exact = [total_cents * s["weight"] / weight_sum for s in shares]
    floors = [int(x // 1) for x in exact]
    remainder = total_cents - sum(floors)

    order = sorted(range(len(shares)), key=lambda i: (-(exact[i] - floors[i]), i))
    for i in order[:remainder]:
        floors[i] += 1

    return [{"name": s["name"], "weight": s["weight"],
             "cents": floors[i], "exact": exact[i]} for i, s in enumerate(shares)]


class SettleOfficer(object):
    def __init__(self, client=None):
        self.mcp = client or MCPClient()

    # ---------- 取数 ----------

    def order(self, order_id):
        data = self.mcp.call_tool_data("query-order", {"orderId": str(order_id)})
        return data.get("data") if isinstance(data, dict) else data

    def orders(self):
        data = self.mcp.call_tool_data("order-list", {})
        inner = data.get("data") if isinstance(data, dict) else data
        return (inner or {}).get("list") or []

    def survey(self, order_id):
        try:
            data = self.mcp.call_tool_data("query-survey-coupon", {"orderId": str(order_id)})
        except MCPError:
            return None
        return data.get("data") if isinstance(data, dict) else data

    def my_coupons(self):
        try:
            data = self.mcp.call_tool_data("query-my-coupons", {"page": 1, "pageSize": 50})
        except MCPError:
            return []
        inner = data.get("data") if isinstance(data, dict) else data
        if isinstance(inner, dict):
            for key in ("coupons", "list", "couponList"):
                if inner.get(key):
                    return inner[key]
            return []
        return inner or []

    # ---------- 核对 ----------

    @staticmethod
    def reconcile(detail):
        """五项对平：商品行、配送、折扣、券、实付。"""
        lines = []
        goods = 0
        for p in detail.get("orderProductList") or []:
            qty = int(p.get("quantity") or 1)
            unit = fen(p.get("price"))
            sub = unit * qty
            goods += sub
            lines.append({
                "name": p.get("productName"),
                "qty": qty,
                "unit": unit,
                "subtotal": sub,
                "combo": [c.get("itemName") for c in (p.get("comboItemList") or [])],
            })

        total_amount = fen(detail.get("totalAmount"))
        product_price = fen(detail.get("productPrice"))
        discount = fen(detail.get("totalDiscountAmount"))
        delivery = fen(detail.get("deliveryPrice"))
        real_delivery = fen(detail.get("realDeliveryPrice"))
        paid = fen(detail.get("realTotalAmount"))
        coupons = detail.get("couponList") or []
        coupon_amount = sum(fen(c.get("amount") or c.get("discount") or 0) for c in coupons)

        # 商品行合计与接口的商品金额应当一致；不一致要指出来，不能悄悄抹平
        base = product_price or total_amount or goods
        expected = base + delivery - discount - coupon_amount
        gap = expected - paid

        return {
            "lines": lines,
            "goodsSum": goods,
            "productPrice": base,
            "listPrice": total_amount,
            "discount": discount,
            "delivery": delivery,
            "realDelivery": real_delivery,
            "coupons": coupons,
            "couponAmount": coupon_amount,
            "paid": paid,
            "expected": expected,
            "gap": gap,
            "goodsMatch": goods == base if base else None,
        }

    # ---------- 月度汇总 ----------

    def month(self, month):
        rows = []
        for o in self.orders():
            created = o.get("createTime") or ""
            if month and not created.startswith(month):
                continue
            rows.append(o)
        rows.sort(key=lambda x: x.get("createTime") or "")
        return rows

    # ---------- 渲染 ----------

    def render_order(self, detail, shares=None):
        rec = self.reconcile(detail)
        L = []
        a = L.append

        a("# 结算单 · %s" % (detail.get("storeName") or "麦当劳"))
        a("")
        a("> 订单 `%s` ｜ 状态 %s ｜ 取餐方式 %s ｜ 下单 %s" % (
            detail.get("orderId"), detail.get("orderStatus") or "-",
            detail.get("takeWay") or "-", detail.get("createTime") or "-"))
        a("")

        a("## 一、金额核对")
        a("")
        a("| 项目 | 金额 |")
        a("|---|---|")
        a("| 商品行合计（%d 行） | %s |" % (len(rec["lines"]), yuan_str(rec["productPrice"])))
        a("| 配送费 | %s |" % yuan_str(rec["delivery"]))
        if rec["discount"]:
            a("| 折扣 | -%s |" % yuan_str(rec["discount"]))
        if rec["couponAmount"]:
            a("| 优惠券（%d 张） | -%s |" % (len(rec["coupons"]), yuan_str(rec["couponAmount"])))
        a("| **应付** | **%s** |" % yuan_str(rec["expected"]))
        a("| **实付** | **%s** |" % yuan_str(rec["paid"]))
        if rec["gap"] == 0:
            a("| 对账差额 | ✅ 0.00，对得上 |")
        else:
            a("| 对账差额 | ⚠️ %s，需要和门店核实 |" % yuan_str(rec["gap"]))
        a("")

        if rec["goodsMatch"] is False:
            a("> ⚠️ 商品行逐项加总 %s，与接口商品金额 %s 不一致，差额 %s。"
              % (yuan_str(rec["goodsSum"]), yuan_str(rec["productPrice"]),
                 yuan_str(rec["goodsSum"] - rec["productPrice"])))
            a("")

        a("## 二、商品明细")
        a("")
        a("| 餐品 | 单价 | 数量 | 小计 |")
        a("|---|---|---|---|")
        for line in rec["lines"]:
            a("| %s%s | ¥%.2f | %d | %s |" % (
                line["name"],
                "（%s）" % " + ".join([c for c in line["combo"] if c]) if line["combo"] else "",
                line["unit"] / 100.0, line["qty"], yuan_str(line["subtotal"])))
        a("")

        if shares:
            a("## 三、分摊")
            a("")
            parts = split_amount(rec["paid"], shares)
            a("按实付 %s 分摊，权重 %s。尾差按最大余额法处理，**分账合计与实付完全一致**。"
              % (yuan_str(rec["paid"]),
                 " / ".join("%s %g" % (p["name"], p["weight"]) for p in parts)))
            a("")
            a("| 部门 / 人 | 权重 | 应分摊 | 占比 |")
            a("|---|---|---|---|")
            for p in parts:
                share = p["cents"] * 100.0 / rec["paid"] if rec["paid"] else 0
                a("| %s | %g | %s | %.1f%% |" % (
                    p["name"], p["weight"], yuan_str(p["cents"]), share))
            a("| **合计** | **%g** | **%s** | 100.0%% |" % (
                sum(p["weight"] for p in parts), yuan_str(sum(p["cents"] for p in parts))))
            a("")

        a("## 四、事后检查")
        a("")
        sv = self.survey(detail.get("orderId"))
        if not sv:
            a("- 评价券：该单不支持或还没开始评价")
        else:
            star = sv.get("overallSatisfaction")
            a("- 评价：%s（%s）" % (
                (("%s 星" % star) if star else "未评价"),
                sv.get("finishTime") or "-"))
            if sv.get("satisfactionDescription"):
                a("- 评价内容：%s" % sv["satisfactionDescription"])
            title = sv.get("couponTitle")
            if title:
                a("- 评价券：**%s** ｜ 核销状态：%s ｜ 有效期 %s 至 %s" % (
                    title, sv.get("couponRedeemStatus") or "待确认",
                    sv.get("couponTradeStartTime") or "-",
                    sv.get("couponTradeEndTime") or "-"))
            else:
                a("- 评价券：这一单没有可领的评价券")

        coupons = self.my_coupons()
        if coupons:
            a("")
            a("账户可用券 %d 张。注意券有可用窗口，拿到不等于现在能用：" % len(coupons))
            a("")
            a("| 券 | 状态 | 可用窗口 |")
            a("|---|---|---|")
            for c in coupons[:5]:
                title = c.get("title") or c.get("couponName") or c.get("name") or "（未命名）"
                ins = c.get("instructions") or {}
                labels = [x.get("text") for x in (ins.get("labels") or []) if x.get("text")]
                windows = [x.get("text") for x in (ins.get("availableTime") or [])
                           if x.get("text")]
                a("| %s | %s | %s |" % (
                    title,
                    "、".join(labels) if labels else "可用",
                    " ｜ ".join(windows[:2]) if windows else "-"))
        a("")

        a("## 五、异常登记模板")
        a("")
        a("出现少送 / 迟到 / 替换时，照这个信息集发给门店，比在电话里描述快得多：")
        a("")
        a("- 订单号：%s" % detail.get("orderId"))
        a("- 门店：%s（%s）" % (detail.get("storeName") or "-",
                              detail.get("storeAddress") or "-"))
        a("- 约定送达：%s" % (detail.get("createTime") or "-"))
        a("- 异常类型：□ 少送 ___ 份　□ 迟到 ___ 分钟　□ 餐品替换")
        a("- 缺失/替换餐品：")
        a("- 现场证据：餐品全家福 + 包装 + 小票（三张，缺一张都容易被驳回）")
        a("- 诉求：□ 补送　□ 退款　□ 补偿券")
        a("")
        a("---")
        a("")
        a("金额以麦当劳接口返回为准；发票口径与索赔结果以门店答复为准。")
        return "\n".join(L)

    def render_month(self, rows, month):
        L = []
        a = L.append
        total = sum(fen(o.get("realTotalAmount")) for o in rows)
        stores = {}
        for o in rows:
            stores.setdefault(o.get("storeName") or "-", [0, 0])
            stores[o.get("storeName") or "-"][0] += 1
            stores[o.get("storeName") or "-"][1] += fen(o.get("realTotalAmount"))

        a("# 团餐月度结算 · %s" % (month or "全部"))
        a("")
        a("> %d 单 ｜ 实付合计 %s ｜ 覆盖 %d 家门店" % (
            len(rows), yuan_str(total), len(stores)))
        a("")
        a("## 逐单")
        a("")
        a("| 时间 | 门店 | 状态 | 实付 |")
        a("|---|---|---|---|")
        for o in rows:
            a("| %s | %s | %s | %s |" % (
                (o.get("createTime") or "")[:16], o.get("storeName") or "-",
                o.get("orderStatus") or "-", yuan_str(fen(o.get("realTotalAmount")))))
        a("")
        a("## 按门店")
        a("")
        a("| 门店 | 单数 | 实付合计 | 占比 |")
        a("|---|---|---|---|")
        for name, (n, amt) in sorted(stores.items(), key=lambda x: -x[1][1]):
            a("| %s | %d | %s | %.1f%% |" % (
                name, n, yuan_str(amt), (amt * 100.0 / total) if total else 0))
        a("")
        a("## 核对提示")
        a("")
        a("- 逐单的完整核对用 `settle.py order --order-id <id>`，会把差额算到分")
        a("- 团餐订单的 `beType` 是 6；到店取餐是 1，两种情况的对账口径不同")
        a("- 跨月订单以 `createTime` 归属，不以送达时间归属")
        return "\n".join(L)


def main(argv=None):
    p = argparse.ArgumentParser(description="麦团餐官 · 结算与对账")
    sub = p.add_subparsers(dest="cmd")

    po = sub.add_parser("order", help="单笔结算单")
    po.add_argument("--order-id", required=True)
    po.add_argument("--shares", default=None, help='分摊，如 "研发:12,市场:5"')
    po.add_argument("--out", default=None)

    pm = sub.add_parser("month", help="月度汇总")
    pm.add_argument("--month", default=None, help="如 2026-10；不填汇总全部")
    pm.add_argument("--out", default=None)

    pl = sub.add_parser("list", help="列出历史订单")

    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 2

    officer = SettleOfficer()
    try:
        if args.cmd == "list":
            for o in officer.orders():
                print("%-20s %-10s %-42s %s" % (
                    o.get("orderId"), (o.get("createTime") or "")[:16],
                    o.get("storeName") or "-", yuan_str(fen(o.get("realTotalAmount")))))
            return 0

        if args.cmd == "order":
            detail = officer.order(args.order_id)
            md = officer.render_order(detail, parse_shares(args.shares) if args.shares else None)
        else:
            rows = officer.month(args.month)
            if not rows:
                print("这个时间段没有订单", file=sys.stderr)
                return 1
            md = officer.render_month(rows, args.month)

        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(md)
            print("已写入 %s" % args.out)
        else:
            print(md)
        return 0
    except MCPError as exc:
        print("出错了：%s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

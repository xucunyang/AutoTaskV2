# -*- coding: utf-8 -*-
"""
三件套溯源硬闸门（10-15 用户硬规则）

每条候选必须三件套：截图 + 卖家 ID + 跳转 URL/收藏标记
两个都不提供 = 跳 = 不进报告

工作流：
1. R<round> 扫描后，pad_leads.py 出 leads.md
2. 本脚本读 leads.md，对每条候选：
   - 在平板/荣耀上从 searchpage 找到价格 text 节点
   - tap 进详情页 → 截图 detail
   - 提取卖家 ID（详情页「卖家」行）→ 截图 seller
   - tap 收藏按钮 → 截图 favorited
3. 缺一不可：缺则标记 NO_TRACE!，不入 reports/
4. 全部完成后写 reports/R<round>_溯源清单.md（含每条三件套状态）

输入：pad_leads.py 出的 leads.md + 设备
输出：reports/R<round>_<MMdd-HHmm>_溯源清单.md（含截图路径）
"""
import io
import os
import re
import sys
import subprocess
import time
import datetime

ROOT = r'E:\code\AutoTaskByMuseSpark1.3\xianyu-hunter'
SCANS = os.path.join(ROOT, 'scans')
REPORTS = os.path.join(ROOT, 'reports')
SHOTS = os.path.join(REPORTS, 'shots')
os.makedirs(SHOTS, exist_ok=True)


def adb_shell(device, s, timeout=20):
    return subprocess.run(
        ['adb', '-s', device, 'shell', s],
        capture_output=True, text=True, timeout=timeout)


def adb(device, cmd, timeout=20):
    return subprocess.run(['adb', '-s', device] + cmd, capture_output=True, text=True, timeout=timeout)


def dump(device, name):
    adb_shell(device, 'uiautomator dump /sdcard/d.xml', timeout=10)
    p = r'C:\Users\22651\AppData\Local\Temp\opencode\trace_last.xml'
    adb(device, ['pull', '/sdcard/d.xml', p], timeout=10)
    with io.open(p, encoding='utf-8', errors='replace') as f:
        return f.read()


def screenshot(device, name):
    p = os.path.join(SHOTS, name + '.png')
    adb_shell(device, f'screencap -p /sdcard/{name}.png', timeout=10)
    adb(device, ['pull', f'/sdcard/{name}.png', p], timeout=10)
    return p


def tap(device, x, y, wait=2):
    adb_shell(device, f'input tap {x} {y}', timeout=5)
    time.sleep(wait)


def back(device, wait=1):
    adb_shell(device, 'input keyevent 4', timeout=5)
    time.sleep(wait)


def find_price_card(xml, price):
    """找价格 text 节点 → 返回所在 clickable 卡片中心"""
    m = re.search(
        r'text="' + re.escape(str(price)) + r'"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
        xml)
    if not m:
        return None
    px = (int(m.group(1)) + int(m.group(3))) // 2
    py = (int(m.group(2)) + int(m.group(4))) // 2
    cards = re.findall(r'clickable="true"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
    best = None
    best_area = 0
    for x1, y1, x2, y2 in cards:
        ix1, iy1, ix2, iy2 = int(x1), int(y1), int(x2), int(y2)
        if ix1 <= px <= ix2 and iy1 <= py <= iy2 and (ix2 - ix1) > 200:
            area = (ix2 - ix1) * (iy2 - iy1)
            if area > best_area:
                best_area = area
                best = ((ix1 + ix2) // 2, (iy1 + iy2) // 2)
    return best


def find_favorite_btn(xml):
    """详情页找收藏按钮中心"""
    for m in re.finditer(
            r'text="([^"]*收藏[^"]*)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            xml):
        t = m.group(1)
        if t not in ('已收藏', '取消收藏'):
            return (int(m.group(2)) + int(m.group(4))) // 2, (int(m.group(3)) + int(m.group(5))) // 2
    for m in re.finditer(
            r'content-desc="([^"]*收藏[^"]*)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            xml):
        t = m.group(1)
        if t not in ('已收藏', '取消收藏'):
            return (int(m.group(2)) + int(m.group(4))) // 2, (int(m.group(3)) + int(m.group(5))) // 2
    return None


def extract_seller_id(xml):
    """从详情页 XML 抽卖家 ID（昵称）"""
    # 闲鱼详情页卖家行：通常是 content-desc 或 text
    patterns = [
        r'content-desc="([^"]{3,30})"\s+[^>]*bounds="\[\d+,(1[0-3]\d|14\d)\]',
        r'text="([^"]{3,30})"[^>]*bounds="\[\d+,(1[0-3]\d|14\d)\]',
    ]
    for pat in patterns:
        for m in re.finditer(pat, xml):
            t = m.group(1).strip()
            # 排除纯数字/单字/含 ¥ 价格的
            if t and not t.isdigit() and '¥' not in t and len(t) >= 3:
                return t
    return None


def collect_one(device, price, slot, round_id):
    """采集一台候选的三件套"""
    base = f'R{round_id}_{slot}_p{price}'
    out = {'price': price, 'screenshot_detail': None, 'screenshot_fav': None,
           'seller_id': None, 'ok': False, 'reason': ''}

    # 1. 在 searchpage 找卡片
    xml = dump(device, f'{base}_search')
    pos = find_price_card(xml, price)
    if not pos:
        # 试一次上滑
        adb_shell(device, 'input swipe 1000 2000 1000 500 800', timeout=5)
        time.sleep(2)
        xml = dump(device, f'{base}_search2')
        pos = find_price_card(xml, price)
    if not pos:
        out['reason'] = 'card_not_found'
        return out

    # 2. tap 进详情
    tap(device, *pos, wait=5)

    # 3. 详情页截图 + 抽卖家 ID
    xml2 = dump(device, f'{base}_detail')
    out['screenshot_detail'] = screenshot(device, base + '_detail')
    seller = extract_seller_id(xml2)
    out['seller_id'] = seller

    # 4. tap 收藏（在滚动到收藏图标可见位置后）
    # 先下滑确保底部 nav 可见
    adb_shell(device, 'input swipe 360 1200 360 400 500', timeout=5)
    time.sleep(2)
    xml3 = dump(device, f'{base}_detail_scrolled')
    fav = find_favorite_btn(xml3)
    if not fav:
        # 再试一次
        adb_shell(device, 'input swipe 360 1200 360 400 500', timeout=5)
        time.sleep(2)
        xml3 = dump(device, f'{base}_detail_scrolled2')
        fav = find_favorite_btn(xml3)
    if fav:
        tap(device, *fav, wait=2)
        out['screenshot_fav'] = screenshot(device, base + '_fav')
    else:
        out['reason'] = 'fav_btn_not_found'

    # 5. 判定三件套
    has_shot = bool(out['screenshot_detail'])
    has_seller = bool(out['seller_id'])
    has_fav = bool(out['screenshot_fav'])
    out['has_shot'] = has_shot
    out['has_seller'] = has_seller
    out['has_fav'] = has_fav
    out['ok'] = has_shot and has_seller and has_fav

    # 6. 返回 searchpage
    back(device, wait=2)
    return out


def parse_leads(path):
    """读 pad_leads.py 出的 leads.md → [(price, flags, desc), ...]"""
    rows = []
    with io.open(path, encoding='utf-8') as f:
        for line in f:
            m = re.match(r'^- \*\*(\d+)\*\* (\S+)?\s*(.*?)$', line)
            if m:
                price = int(m.group(1))
                flags = m.group(2) or '-'
                desc = m.group(3).strip()
                rows.append((price, flags, desc))
    return rows


def main():
    if len(sys.argv) < 4:
        print('usage: trace_leads.py <round_id> <leads.md> <device>')
        sys.exit(1)
    round_id = sys.argv[1]
    leads_path = sys.argv[2]
    device = sys.argv[3]

    rows = parse_leads(leads_path)
    if not rows:
        print('no rows in', leads_path)
        return

    # 优先 64G 候选
    target = [r for r in rows if '64G' in r[1] or '64g' in r[2] or '64+' in r[2]]
    if not target:
        print('no 64G targets, using all rows')
        target = rows

    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    fname_ts = datetime.datetime.now().strftime('%m%d-%H%M')

    results = []
    for i, (price, flags, desc) in enumerate(target, 1):
        print(f'[{i}/{len(target)}] collecting ¥{price} ...')
        try:
            r = collect_one(device, price, i, round_id)
            r['flags'] = flags
            r['desc'] = desc[:80]
            results.append(r)
        except Exception as e:
            results.append({'price': price, 'flags': flags, 'desc': desc[:80],
                            'ok': False, 'reason': str(e), 'has_shot': False,
                            'has_seller': False, 'has_fav': False})
        time.sleep(1)

    # 写报告
    out_path = os.path.join(REPORTS, f'R{round_id}_{fname_ts}_溯源清单.md')
    out = [f'# R{round_id} 溯源清单 ({ts})\n\n']
    out.append(f'**输入**：{leads_path}\n')
    out.append(f'**设备**：{device}\n')
    out.append(f'**目标**：64G 候选（{len(target)} 台）\n\n')
    out.append('## 三件套硬规则\n\n')
    out.append('> 缺一不可：截图 + 卖家 ID + 收藏标记\n')
    out.append('> 缺任一 → 标 NO_TRACE!，不进 candidates.md\n\n')
    out.append('---\n\n')
    out.append('## 候选清单\n\n')
    n_ok = 0
    n_no = 0
    for r in results:
        ok_tag = '✅' if r['ok'] else '❌ NO_TRACE!'
        if r['ok']:
            n_ok += 1
        else:
            n_no += 1
        out.append(f'### {ok_tag} ¥{r["price"]:,} [{r.get("flags", "-")}] {r.get("desc", "")}\n\n')
        out.append(f'- 详情截图：{r.get("screenshot_detail") or "❌ 缺失"}\n')
        out.append(f'- 卖家 ID：{r.get("seller_id") or "❌ 缺失"}\n')
        out.append(f'- 收藏截图：{r.get("screenshot_fav") or "❌ 缺失"}\n')
        if r.get('reason'):
            out.append(f'- 失败原因：{r["reason"]}\n')
        out.append('\n')

    out.append(f'---\n\n**统计**：{len(results)} 候选 / {n_ok} 三件套齐 / {n_no} 缺项\n')
    if n_no > 0:
        out.append(f'\n⚠️ **{n_no} 台候选三件套不全，禁止进 candidates.md**\n')

    with io.open(out_path, 'w', encoding='utf-8') as f:
        f.writelines(out)
    print('written', out_path)
    print(f'OK={n_ok}  NO_TRACE={n_no}')

    # 写 scan-log.md WARN
    if n_no > 0:
        log = os.path.join(ROOT, 'scan-log.md')
        ts_short = datetime.datetime.now().strftime('%m-%d %H:%M:%S')
        with io.open(log, 'a', encoding='utf-8') as f:
            f.write(f'\n- {ts_short} R{round_id}: NO_TRACE={n_no}/{len(results)} (缺三件套，未入池)\n')

    # 写 no_trace 黑名单：禁止这些价格的 listing 进入 candidates.md
    if n_no > 0:
        bl = os.path.join(ROOT, 'no_trace_blacklist.txt')
        with io.open(bl, 'a', encoding='utf-8') as f:
            for r in results:
                if not r['ok']:
                    f.write(f"R{round_id} ¥{r['price']} reason={r.get('reason', 'unknown')}\n")
        print(f'blacklisted {n_no} no-trace listings to no_trace_blacklist.txt')


if __name__ == '__main__':
    main()

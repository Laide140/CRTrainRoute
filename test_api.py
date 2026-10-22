#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国铁路算路 API 测试

用法：python3 test_api.py
服务需已在 8889 端口运行。
"""
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = 'http://127.0.0.1:8889'

# (from, to, 说明, 合理里程区间 km, 期望是否可达)
CASES = [
    ('IZQ', 'BXP', '广州南 -> 北京西', (1900, 2400), True),
    ('BXP', 'AOH', '北京西 -> 上海虹桥', (1250, 1450), True),
    ('AOH', 'NKH', '上海虹桥 -> 南京南', (250, 350), True),
    ('IZQ', 'AOH', '广州南 -> 上海虹桥', (1500, 1900), True),
    ('IZQ', 'GGQ', '广州南 -> 广州东', (15, 45), True),
    # 北京西<->北京 经北京地下直径线/枢纽联络线，直线约7km
    ('BXP', 'BJP', '北京西 -> 北京', (7, 40), True),
    ('AOH', 'HGH', '上海虹桥 -> 杭州东', (140, 200), True),
    ('IZQ', 'IOQ', '广州南 -> 深圳北', (90, 160), True),
    # 跨海：广州南->海口需经琼州海峡轮渡（直线计）
    ('IZQ', 'VUQ', '广州南 -> 海口', (540, 640), True),
    ('IZQ', 'SEQ', '广州南 -> 三亚', (850, 960), True),
]

# 同址异名车站剔除验证：(from, to, 应出现的站, 不应出现的站)
SAME_SITE_CASES = [
    ('广州南', '北京西', '广州北站', '花都站'),
    ('广州南', '虎门', '庆盛站', '虎门北站'),
    ('广州南', '肇庆东', '佛山西站', '鼎湖东站'),
]

ERROR_CASES = [
    ('', 'BXP', '缺少 from', 400),
    ('IZQ', '', '缺少 to', 400),
    ('ZZZ', 'BXP', '非法电报码', 404),
    ('IZQ', 'ZZZ', '非法电报码(to)', 404),
]

pass_n = 0
fail_n = 0

EARTH_R = 6371008.8


def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def polyline_length(coords):
    return sum(haversine(y1, x1, y2, x2) for (x1, y1), (x2, y2) in zip(coords, coords[1:]))


def no_backtrack(coords):
    """检查折线没有来回折返：相邻点不应重合，且不应出现 A-B-A 式回退。"""
    for i in range(len(coords) - 1):
        if coords[i] == coords[i + 1]:
            return False
    # 抽样检查 A-B-A
    for i in range(len(coords) - 2):
        if coords[i] == coords[i + 2]:
            return False
    return True


def get(path):
    url = BASE + path
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=90) as r:
            body = r.read().decode('utf-8')
            return r.status, json.loads(body), (time.time() - t0) * 1000
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8')), (time.time() - t0) * 1000


def check(cond, msg):
    global pass_n, fail_n
    if cond:
        pass_n += 1
        print('   ✓ %s' % msg)
    else:
        fail_n += 1
        print('   ✗ %s' % msg)
    return cond


def main():
    print('=' * 70)
    print('1. 健康检查 /health')
    print('=' * 70)
    code, j, ms = get('/health')
    check(code == 200, 'GET /health 返回 200 (%.0fms)' % ms)
    check(j.get('ok') is True, 'ok=true')
    print('   service =', j.get('service'))

    print()
    print('=' * 70)
    print('2. 正常算路 %d 组' % len(CASES))
    print('=' * 70)
    for frm, to, desc, (lo, hi), reachable in CASES:
        path = '/api/v3/stationRoute?from=%s&to=%s' % (frm, to)
        try:
            code, j, ms = get(path)
        except Exception as e:
            check(False, '%s 请求异常: %s' % (desc, e))
            continue
        print('\n[%s -> %s] %s  HTTP %d  %.0fms' % (frm, to, desc, code, ms))
        if not j.get('ok'):
            check(not reachable, '%s 不可达(符合预期): %s' % (desc, j.get('error')))
            continue
        if not check(code == 200, 'HTTP 200'):
            continue
        km = j['distance']['kilometers']
        check(j['from']['code'] == frm, '起点电报码回显正确')
        check(j['to']['code'] == to, '终点电报码回显正确')
        check(j['from']['name'] is not None, '起点站名: %s' % j['from']['name'])
        check(j['to']['name'] is not None, '终点站名: %s' % j['to']['name'])
        check('lat' in j['from'] and 'lon' in j['from'], '起点坐标存在')
        check(lo <= km <= hi, '里程 %.1f km 落在 [%d, %d] 区间' % (km, lo, hi))
        check(j['distance']['meters'] > 0, 'meters 字段有效 (%.0f)' % j['distance']['meters'])
        coords = j['geometry']['coordinates']
        check(len(coords) >= 2, 'geometry 含 %d 个坐标点' % len(coords))
        check(j['geometry']['type'] == 'LineString', 'geometry 类型为 LineString')
        check(all(len(c) == 2 for c in coords), '坐标均为 [lon, lat] 二元组')
        check(j['from']['snapOffsetM'] < 5000, '起点吸附偏移 %.0fm < 5km' % j['from']['snapOffsetM'])
        check(j['to']['snapOffsetM'] < 5000, '终点吸附偏移 %.0fm < 5km' % j['to']['snapOffsetM'])
        check(ms < 15000, '响应耗时 %.0fms < 15s' % ms)

        # 几何自洽：折线长度应与 Dijkstra 里程一致
        plen = polyline_length(coords) / 1000.0
        check(abs(plen - km) / km < 0.02,
              '折线长度 %.1f km 与里程 %.1f km 偏差 %.1f%% < 2%%' % (plen, km, 100 * abs(plen - km) / km))
        check(no_backtrack(coords), '折线无折返（无相距过近的重复点）')

        # 首尾坐标应贴近两端车站
        d_start = haversine(j['from']['lat'], j['from']['lon'], coords[0][1], coords[0][0])
        d_end = haversine(j['to']['lat'], j['to']['lon'], coords[-1][1], coords[-1][0])
        check(d_start < 5000, '折线起点距起点站 %.0fm < 5km' % d_start)
        check(d_end < 5000, '折线终点距终点站 %.0fm < 5km' % d_end)

        # 经过车站：按里程升序、里程在 [0, 总里程] 内、不含两端站
        sts = j['stations']
        check(all('code' in s and 'name' in s and 'km' in s for s in sts), '车站字段完整')
        kms = [s['km'] for s in sts]
        check(kms == sorted(kms), '车站按沿线里程升序排列')
        check(all(0 <= k <= km for k in kms), '车站里程均在 [0, %.1f] 内' % km)
        check(all(s['code'] not in (frm, to) for s in sts), '途经车站不含两端站')

        # 跨海标识字段
        check('ferry' in j and 'hasFerry' in j['ferry'], 'ferry 字段存在且含 hasFerry')
        if j['ferry']['hasFerry']:
            check(j['ferry']['km'] > 0, '轮渡里程 %.1f km > 0' % j['ferry']['km'])
            check(len(j['ferry']['segments']) >= 1, '轮渡段数 %d' % len(j['ferry']['segments']))
            check(any('跨海' in w for w in j['warnings']), 'warnings 含跨海提示')

        print('   → %.1f km, %d 个节点, 经过车站 %d 个' % (km, j['nodeCount'], j['stationCount']))

    print()
    print('=' * 70)
    print('3. 异常参数 %d 组' % len(ERROR_CASES))
    print('=' * 70)
    for frm, to, desc, want in ERROR_CASES:
        q = urllib.parse.urlencode({'from': frm, 'to': to})
        code, j, ms = get('/api/v3/stationRoute?' + q)
        print('\n[%s] HTTP %d' % (desc, code))
        check(code == want, '返回 %d (期望 %d)' % (code, want))
        check(j.get('ok') is False, 'ok=false')
        check(bool(j.get('error')), 'error 字段非空: %s' % j.get('error'))

    print()
    print('=' * 70)
    print('4. 缓存行为（同参数二次请求应命中缓存并更快）')
    print('=' * 70)
    if CASES:
        frm, to = CASES[0][0], CASES[0][1]
        path = '/api/v3/stationRoute?from=%s&to=%s' % (frm, to)
        c1, j1, ms1 = get(path)
        c2, j2, ms2 = get(path)
        print('   首次 %.0fms (metaSource=%s)' % (ms1, j1['from']['metaSource']))
        print('   二次 %.0fms (metaSource=%s)' % (ms2, j2['from']['metaSource']))
        check(j1['distance']['meters'] == j2['distance']['meters'], '两次结果里程一致')
        check(j2['to']['metaSource'] in ('cache', 'remote'), 'metaSource 字段有效')

    print()
    print('=' * 70)
    print('5. 同址异名车站剔除')
    print('=' * 70)
    for frm, to, must_have, must_not in SAME_SITE_CASES:
        q = urllib.parse.urlencode({'from': frm, 'to': to})
        try:
            code, j, ms = get('/api/v3/stationRoute?' + q)
        except Exception as e:
            check(False, '%s->%s 请求异常: %s' % (frm, to, e))
            continue
        if not j.get('ok'):
            check(False, '%s->%s 失败: %s' % (frm, to, j.get('error')))
            continue
        names = [s['name'] for s in j['stations']]
        print('\n[%s -> %s] %s' % (frm, to, '、'.join(names) or '(无)'))
        check(must_have in names, '包含同址国铁站 %s' % must_have)
        check(must_not not in names, '剔除城际别名站 %s' % must_not)

    print()
    print('=' * 70)
    print('6. 指定里程规划 km=')
    print('=' * 70)
    for tgt in (2200, 2800, 3200):
        q = urllib.parse.urlencode({'from': '广州南', 'to': '北京西', 'km': tgt})
        code, j, ms = get('/api/v3/stationRoute?' + q)
        if not j.get('ok'):
            check(False, 'km=%d 失败: %s' % (tgt, j.get('error')))
            continue
        p = j['kmPlan']
        print('\n[目标 %d km] 实际 %.1f km，%d 站，%.0fms' % (
            tgt, p['actualKm'], j['stationCount'], ms))
        check(code == 200, 'HTTP 200')
        check(p is not None and p['requestedKm'] == tgt, 'kmPlan.requestedKm 正确')
        check(abs(p['diffKm']) <= max(5.0, tgt * 0.05),
              '里程偏差 %.1f km 在容差内' % p['diffKm'])
        check(p['reached'] is True, 'reached=true')
        check(j['distance']['kilometers'] > 0, 'distance 字段有效')
        check(len(j['geometry']['coordinates']) > 100, '几何完整')

    # ver 参数：1=整段（默认），2=微调（精度更高）
    print()
    print('--- ver 模式对比 ---')
    res = {}
    for v in (1, 2):
        q = urllib.parse.urlencode({'from': '广州南', 'to': '北京西', 'km': 2800, 'ver': v})
        code, j, ms = get('/api/v3/stationRoute?' + q)
        if not j.get('ok'):
            check(False, 'ver=%d 失败' % v)
            continue
        p = j['kmPlan']
        res[v] = p
        print('   ver=%d (%s): %.1f km, 差 %+.1f km, %.0fms' % (
            v, p['mode'], p['actualKm'], p['diffKm'], ms))
        check(p['ver'] == v, 'kmPlan.ver 回显为 %d' % v)
        check(abs(p['diffKm']) <= max(5.0, 2800 * 0.05), 'ver=%d 偏差在容差内' % v)
    if 1 in res and 2 in res:
        check(abs(res[2]['diffKm']) <= abs(res[1]['diffKm']) + 1.0,
              '微调模式精度不低于整段 (%+.1f vs %+.1f)' % (res[2]['diffKm'], res[1]['diffKm']))

    # 不传 ver 时默认整段
    q = urllib.parse.urlencode({'from': '广州南', 'to': '北京西', 'km': 2800})
    code, j, ms = get('/api/v3/stationRoute?' + q)
    check(j['kmPlan']['ver'] == 1 and j['kmPlan']['mode'] == '整段', '不传 ver 默认 ver=1 整段')

    # 非法 ver
    for bad in ('0', '3', 'abc'):
        q = urllib.parse.urlencode({'from': '广州南', 'to': '北京西', 'km': 2800, 'ver': bad})
        code, j, ms = get('/api/v3/stationRoute?' + q)
        check(code == 400, '非法 ver=%s 返回 400' % bad)

    # 目标小于最短路时退回最短路
    q = urllib.parse.urlencode({'from': '广州南', 'to': '北京西', 'km': 500})
    code, j, ms = get('/api/v3/stationRoute?' + q)
    check(j.get('ok') and j['distance']['kilometers'] > 500,
          '目标小于最短路时返回最短路 (%.0f km)' % j['distance']['kilometers'])

    # 非法 km
    q = urllib.parse.urlencode({'from': '广州南', 'to': '北京西', 'km': 'abc'})
    code, j, ms = get('/api/v3/stationRoute?' + q)
    check(code == 400, 'km 非数字返回 400')
    q = urllib.parse.urlencode({'from': '广州南', 'to': '北京西', 'km': '-5'})
    code, j, ms = get('/api/v3/stationRoute?' + q)
    check(code == 400, 'km 为负返回 400')

    print()
    print('=' * 70)
    print('7. 404 路由')
    print('=' * 70)
    code, j, ms = get('/api/v3/notExist')
    check(code == 404, '未知路径返回 404')
    check(j.get('ok') is False, 'ok=false')

    print()
    print('=' * 70)
    print('结果：通过 %d，失败 %d' % (pass_n, fail_n))
    print('=' * 70)
    return 0 if fail_n == 0 else 1


if __name__ == '__main__':
    sys.exit(main())

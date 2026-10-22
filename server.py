#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国铁路算路 API

端点：GET /api/v3/stationRoute?from=IZQ&to=BXP
端口：8889

功能：计算两个站之间的最短走行线路、最短距离、经过车站。

数据来源：
  - 铁路线几何：next.laide.net.cn 的 OSM 铁路 geojson（本地构建为骨架图）
  - 车站坐标：查询时实时请求 12306 getStationAddress（stationCode = 电报码）
"""
import heapq
import json
import math
import os
import pickle
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, 'data')
GRAPH_PATH = os.path.join(DATA, 'rail_graph.pkl')
PORT = 8889

EARTH_R = 6371008.8
STATION_URL = 'https://mobile.12306.cn/wxxcx/wechat/bigScreen/getStationAddress'
UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148'

# ---------------------------------------------------------------- 几何工具


def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def seg_point_dist(lat, lon, alat, alon, blat, blon):
    """点到线段的最短距离（米），用局部等距投影近似。"""
    coslat = math.cos(math.radians(lat))
    kx = 111320.0 * coslat
    ky = 110540.0
    px, py = (lon - lon) * kx, (lat - lat) * ky
    ax, ay = (alon - lon) * kx, (alat - lat) * ky
    bx, by = (blon - lon) * kx, (blat - lat) * ky
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 <= 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / L2
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


# ---------------------------------------------------------------- 车站坐标（实时请求）

class StationClient:
    """带 TTL 缓存的 12306 车站坐标查询。"""

    def __init__(self, ttl=6 * 3600, timeout=12):
        self.ttl = ttl
        self.timeout = timeout
        self._cache = {}
        self._lock = threading.Lock()
        self._name_map = self._load_name_map()

    def _load_name_map(self):
        """电报码 -> 站名 的本地映射（仅用于展示，坐标仍实时请求）。"""
        path = os.path.join(DATA, 'station_names.json')
        if os.path.exists(path):
            try:
                return json.load(open(path, encoding='utf-8'))
            except Exception:
                return {}
        return {}

    def name_of(self, code):
        return self._name_map.get(code)

    def resolve(self, q):
        """把用户输入解析成电报码。

        支持三种写法：
          - 电报码：IZQ / izq
          - 中文站名：广州南 / 广州南站
          - 站名+后缀：北京西站

        返回 (code, None) 或 (None, 错误说明)。
        """
        q = (q or '').strip()
        if not q:
            return None, '参数为空'
        up = q.upper()

        # 1) 直接是电报码
        if up in self._name_map:
            return up, None
        # 电报码形态（2-4 位大写字母），但不在表中
        if up.isalpha() and up.isascii() and 2 <= len(up) <= 4:
            return None, '电报码 %s 不存在' % up

        # 2) 中文站名精确匹配（建反向表并缓存）
        rev = getattr(self, '_rev_map', None)
        if rev is None:
            rev = {}
            for code, name in self._name_map.items():
                for key in (name, name + '站'):
                    rev.setdefault(key, code)
            self._rev_map = rev

        for key in (q, q + '站'):
            if key in rev:
                return rev[key], None
        # 去掉「站」再试（避免「北京西站站」这类输入）
        if q.endswith('站') and q[:-1] in rev:
            return rev[q[:-1]], None

        # 3) 模糊包含匹配（唯一命中才接受，否则给出候选）
        hits = [(code, name) for code, name in self._name_map.items() if q in name]
        if len(hits) == 1:
            return hits[0][0], None
        if len(hits) > 1:
            cand = '、'.join('%s(%s)' % (n, c) for c, n in hits[:8])
            return None, '站名「%s」匹配到多个车站：%s' % (q, cand)
        return None, '未找到车站「%s」' % q

    def get(self, code):
        code = code.upper().strip()
        now = time.time()
        with self._lock:
            hit = self._cache.get(code)
            if hit and now - hit['ts'] < self.ttl:
                return hit['data'], 'cache'

        data = self._fetch(code)
        if data:
            with self._lock:
                self._cache[code] = {'ts': now, 'data': data}
        return data, 'remote'

    def _fetch(self, code):
        body = urllib.parse.urlencode({'stationCode': code}).encode()
        req = urllib.request.Request(
            STATION_URL, data=body,
            headers={'User-Agent': UA,
                     'Content-Type': 'application/x-www-form-urlencoded',
                     'Referer': 'https://servicewechat.com/'},
        )
        last_err = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    payload = json.loads(r.read().decode('utf-8'))
                if not payload.get('status') or not payload.get('data'):
                    return None
                d = payload['data']
                lat = float(d['latitude'])
                lon = float(d['longitute'])
                return {
                    'code': code,
                    'name': d.get('stationName') or self.name_of(code) or code,
                    'lat': lat,
                    'lon': lon,
                    'city': d.get('cityName'),
                    'address': d.get('stationAddress'),
                }
            except Exception as e:  # 网络抖动重试
                last_err = e
                time.sleep(0.4 * (attempt + 1))
        if last_err:
            print('[warn] 车站 %s 查询失败: %s' % (code, last_err))
        return None


# ---------------------------------------------------------------- 图与寻路

class RailGraph:
    def __init__(self, path):
        with open(path, 'rb') as f:
            g = pickle.load(f)
        self.lats = g['lats']
        self.lons = g['lons']
        self.adj = g['adj']
        self.station_nodes = g.get('station_nodes', {})
        self.stations = g.get('stations', {})
        self.comp = g.get('comp', {})
        self.big_comp = g.get('big_comp', 0)
        self.edge_geom = g.get('edge_geom', {})
        # 跨海轮渡段（建图时补入的边），用于在结果中标注
        self.ferry_edges = g.get('ferry_edges', [])
        self.ferry_set = set()
        for fe in self.ferry_edges:
            self.ferry_set.add((tuple(fe['from']), tuple(fe['to'])))
            self.ferry_set.add((tuple(fe['to']), tuple(fe['from'])))
        self._build_index()

    def _build_index(self):
        """构建查询索引。

        - grid：0.05 度网格分桶，加速最近节点查询
        - CSR：邻接表压成扁平数组（head/edge_to/edge_w），避免大量小字典
          带来的内存与构建开销；这是长距离寻路的主要性能瓶颈。
        """
        self.cell = 0.05
        self.grid = {}
        for nid in self.adj:
            key = (int(self.lats[nid] / self.cell), int(self.lons[nid] / self.cell))
            self.grid.setdefault(key, []).append(nid)

        # ---- CSR 构建 ----
        nodes = list(self.adj.keys())
        pos = {n: i for i, n in enumerate(nodes)}
        self.node_ids = nodes                     # 索引 -> 节点 id
        deg = [0] * len(nodes)
        for i, n in enumerate(nodes):
            deg[i] = len(self.adj[n])
        head = [0] * (len(nodes) + 1)
        for i in range(len(nodes)):
            head[i + 1] = head[i] + deg[i]
        edge_to = [0] * head[-1]
        edge_w = [0.0] * head[-1]
        fill = head[:]
        for i, n in enumerate(nodes):
            k = fill[i]
            for m, w in self.adj[n].items():
                edge_to[k] = pos[m]
                edge_w[k] = w
                k += 1
            fill[i] = k
        self.head = head
        self.edge_to = edge_to
        self.edge_w = edge_w
        self.pos = pos

    def snap(self, lat, lon, radius_deg=1.0):
        """把坐标吸附到最近的骨架节点。优先找边（点到线段），回退到最近节点。"""
        # 1) 沿边投影，取真正最近的点（边是长直线段，直接最近节点会偏差大）
        best = None
        ci, cj = int(lat / self.cell), int(lon / self.cell)
        steps = int(radius_deg / self.cell) + 1
        cand_nodes = []
        for di in range(-steps, steps + 1):
            for dj in range(-steps, steps + 1):
                cand_nodes.extend(self.grid.get((ci + di, cj + dj), ()))
        if not cand_nodes:
            return None, 1e18
        # 候选节点上挂的边
        head, edge_to, edge_w = self.head, self.edge_to, self.edge_w
        pos = self.pos
        node_ids = self.node_ids
        edge_seen = set()
        for n in cand_nodes:
            i = pos[n]
            for k in range(head[i], head[i + 1]):
                m = node_ids[edge_to[k]]
                ek = (n, m) if n < m else (m, n)
                if ek in edge_seen:
                    continue
                edge_seen.add(ek)
                d = seg_point_dist(lat, lon, self.lats[n], self.lons[n], self.lats[m], self.lons[m])
                if best is None or d < best[0]:
                    best = (d, n, m)
        if best is None:
            return None, 1e18
        return best, best[0]

    def nearest_node(self, lat, lon, radius_deg=0.6, main_only=True):
        """最近节点（用于车站吸附）。

        main_only=True 时只考虑最大连通分量内的节点。车站附近常存在
        站内孤岛（站线未与干线互通），吸附到孤岛会导致全程不可达，
        因此只吸附到主干网节点。若主网节点较远，由调用方通过
        snapOffsetM 提示用户。
        """
        def scan(main_filter):
            best, bd = None, 1e18
            ci, cj = int(lat / self.cell), int(lon / self.cell)
            steps = int(radius_deg / self.cell) + 1
            for di in range(-steps, steps + 1):
                for dj in range(-steps, steps + 1):
                    for n in self.grid.get((ci + di, cj + dj), ()):
                        if main_filter is not None and self.comp.get(n) != main_filter:
                            continue
                        d = haversine(lat, lon, self.lats[n], self.lons[n])
                        if d < bd:
                            best, bd = n, d
            if best is None:
                for n in self.adj:
                    if main_filter is not None and self.comp.get(n) != main_filter:
                        continue
                    d = haversine(lat, lon, self.lats[n], self.lons[n])
                    if d < bd:
                        best, bd = n, d
            return best, bd

        if main_only:
            best, bd = scan(self.big_comp)
            # 主网节点可能距车站很远（OSM 中该段线路断链），
            # 此时仍返回该节点，由调用方通过 snapOffsetM 提示可能不准。
            # 绝不回退到孤岛节点，否则会导致全程不可达。
            return best, bd
        return scan(None)

    def dijkstra(self, src, dst, penalty=None, factor=1.0):
        """A* 最短路搜索（启发式 = 到终点的大圆距离，可采纳且一致）。

        使用 CSR 扁平邻接数组 + 数组式状态，避免字典/元组迭代开销。
        penalty/factor：对指定有向边乘以绕行惩罚系数，用于「指定里程」规划。
        返回 (总长米, 节点 id 路径列表)，不可达返回 (None, None)。
        """
        if src == dst:
            return 0.0, [src]
        lats, lons = self.lats, self.lons
        head, edge_to, edge_w = self.head, self.edge_to, self.edge_w
        node_ids, pos = self.node_ids, self.pos
        N = len(node_ids)

        s_i = pos[src]
        t_i = pos[dst]
        tlat, tlon = lats[dst], lons[dst]
        hav = haversine
        INF = float('inf')

        dist = [INF] * N
        prev = [-1] * N
        done = bytearray(N)
        dist[s_i] = 0.0
        h0 = hav(lats[src], lons[src], tlat, tlon)
        pq = [(h0, 0.0, s_i)]

        while pq:
            f, d, i = heapq.heappop(pq)
            if done[i]:
                continue
            done[i] = 1
            if i == t_i:
                break
            li = lats[node_ids[i]]
            lo = lons[node_ids[i]]
            for k in range(head[i], head[i + 1]):
                j = edge_to[k]
                if done[j]:
                    continue
                w = edge_w[k]
                if penalty is not None and w and (i, j) in penalty:
                    w *= factor
                nd = d + w
                if nd < dist[j]:
                    dist[j] = nd
                    prev[j] = i
                    vlat = lats[node_ids[j]]
                    vlon = lons[node_ids[j]]
                    heapq.heappush(pq, (nd + hav(vlat, vlon, tlat, tlon), nd, j))

        if dist[t_i] == INF:
            return None, None
        path_i = [t_i]
        cur = t_i
        while cur != s_i:
            cur = prev[cur]
            path_i.append(cur)
        path_i.reverse()
        return dist[t_i], [node_ids[i] for i in path_i]

    def path_length(self, path):
        total = 0.0
        for a, b in zip(path, path[1:]):
            total += self.adj[a].get(b, haversine(self.lats[a], self.lons[a], self.lats[b], self.lons[b]))
        return total


# ---------------------------------------------------------------- 服务逻辑

class RailService:
    def __init__(self):
        t0 = time.time()
        print('加载铁路图...')
        self.graph = RailGraph(GRAPH_PATH)
        print('图就绪：节点 %d，边 %d，用时 %.1fs' % (
            len(self.graph.adj),
            sum(len(v) for v in self.graph.adj.values()) // 2,
            time.time() - t0))
        self.stations = StationClient()
        # 同址异名车站标注（如广州北/花都），用于途经车站去重
        self.station_lanes = self._load_station_lanes()
        # 算路结果缓存：铁路网是静态的，同一 (from,to) 的几何与途经车站可复用。
        # key = (from, to, 起点吸附节点, 终点吸附节点)
        self._route_cache = {}
        self._route_lock = threading.Lock()
        self._CACHE_MAX = 512

    def _load_station_lanes(self):
        """读取同址异名车站标注表 data/station_lanes.json。"""
        path = os.path.join(DATA, 'station_lanes.json')
        if not os.path.exists(path):
            return {}
        try:
            data = json.load(open(path, encoding='utf-8'))
            lanes = data.get('stations') or {}
            print('同址车站标注：%d 个（%s）' % (
                len(lanes), '、'.join(v.get('name', k) for k, v in lanes.items())))
            return lanes
        except Exception as e:
            print('[warn] 读取 station_lanes.json 失败: %s' % e)
            return {}

    def route(self, from_code, to_code, target_km=None, ver=1):
        t0 = time.time()
        # 支持电报码 / 中文站名，统一解析成电报码
        from_code, err1 = self.stations.resolve(from_code)
        if err1:
            return {'ok': False, 'code': 404, 'error': err1,
                    'hint': '可填电报码(如 IZQ)或站名(如 广州南)'}
        to_code, err2 = self.stations.resolve(to_code)
        if err2:
            return {'ok': False, 'code': 404, 'error': err2,
                    'hint': '可填电报码(如 BXP)或站名(如 北京西)'}

        s1, src_src = self.stations.get(from_code)
        s2, dst_src = self.stations.get(to_code)
        if not s1:
            return {'ok': False, 'code': 404, 'error': '车站 %s 暂无坐标' % from_code,
                    'hint': '12306 未提供该站坐标'}
        if not s2:
            return {'ok': False, 'code': 404, 'error': '车站 %s 暂无坐标' % to_code,
                    'hint': '12306 未提供该站坐标'}

        g = self.graph
        src_node, src_off = g.nearest_node(s1['lat'], s1['lon'])
        dst_node, dst_off = g.nearest_node(s2['lat'], s2['lon'])

        if src_node is None or dst_node is None:
            return {'ok': False, 'code': 422, 'error': '车站附近没有可用铁路网络'}

        # 指定里程规划：目标里程大于最短路时，寻找最接近该里程的绕行线路
        km_plan = None
        if target_km is not None:
            length, path, ok = self.plan_by_km(src_node, dst_node, target_km, ver)
            if path is None:
                return {'ok': False, 'code': 422,
                        'error': '两站之间在铁路网络中不可达',
                        'from': s1['name'], 'to': s2['name']}
            coords, ferries = self._path_coords(path)
            passed = self._stations_along(path, from_code.upper(), to_code.upper(), coords=coords)
            n_nodes = len(path)
            km_plan = {
                'requestedKm': round(float(target_km), 1),
                'actualKm': round(length / 1000.0, 2),
                'diffKm': round(length / 1000.0 - float(target_km), 2),
                'reached': bool(ok),
                'ver': ver,
                'mode': '整段' if ver == 1 else '微调',
            }
        else:
            # 计算结果缓存（铁路网静态，几何与途经车站可复用）
            ck = (src_node, dst_node)
            with self._route_lock:
                cached = self._route_cache.get(ck)
            if cached is not None:
                length, coords, passed, n_nodes, ferries = cached
            else:
                length, path = g.dijkstra(src_node, dst_node)
                if path is None:
                    return {'ok': False, 'code': 422,
                            'error': '两站之间在铁路网络中不可达',
                            'from': s1['name'], 'to': s2['name']}
                coords, ferries = self._path_coords(path)
                passed = self._stations_along(path, from_code.upper(), to_code.upper(), coords=coords)
                with self._route_lock:
                    if len(self._route_cache) >= self._CACHE_MAX:
                        self._route_cache.clear()
                    n_nodes = len(path)
                    self._route_cache[ck] = (length, coords, passed, n_nodes, ferries)

        warnings = []
        # 吸附过远说明 OSM 中该段线路可能断链，接口如实提示
        for label, nm, off in (('起点', s1['name'], src_off), ('终点', s2['name'], dst_off)):
            if off > 2000:
                warnings.append('%s%s距最近铁路网 %.1f km，该段线路数据可能不完整，'
                                '里程为近似值' % (label, nm, off / 1000.0))
        # 跨海轮渡段（琼州海峡）：OSM 无轮渡航线，按两端直线距离计入里程
        ferry_km = round(sum(f['km'] for f in (ferries or [])), 2)
        if ferries:
            warnings.append('线路含跨海轮渡段 %.1f km（按直线距离计），需乘船过海' % ferry_km)

        return {
            'ok': True,
            'from': {'code': from_code.upper(), 'name': s1['name'], 'lat': s1['lat'],
                     'lon': s1['lon'], 'city': s1['city'], 'address': s1['address'],
                     'snapOffsetM': round(src_off, 1), 'metaSource': src_src},
            'to': {'code': to_code.upper(), 'name': s2['name'], 'lat': s2['lat'],
                   'lon': s2['lon'], 'city': s2['city'], 'address': s2['address'],
                   'snapOffsetM': round(dst_off, 1), 'metaSource': dst_src},
            'distance': {
                'meters': round(length, 0),
                'kilometers': round(length / 1000.0, 3),
            },
            'stationCount': len(passed),
            'kmPlan': km_plan,
            'ferry': {
                'hasFerry': bool(ferries),
                'km': ferry_km,
                'segments': ferries or [],
            },
            'stations': passed,
            'warnings': warnings,
            'geometry': {
                'type': 'LineString',
                'coordinates': coords,
            },
            'nodeCount': n_nodes,
            'elapsedMs': int((time.time() - t0) * 1000),
        }

    def plan_by_km(self, src, dst, target_km, ver=1):
        """指定里程规划：找一条总里程尽量接近 target_km 的线路。

        两种模式：

        ver=1 整段（默认）
            只做「全局惩罚迭代」：每轮把上一轮路径的边加入惩罚集再求解，
            里程单调增大，在得到的候选序列里取最接近目标的一条。
            速度快，但候选之间可能有几十公里的跳跃。
            实测广州南→北京西：目标 2500km 得到 2570km（差 +70）。

        ver=2 微调
            在整段模式的基础上追加「局部微调」：把候选路径的若干区间替换成
            「避开原边」的次短路，增量通常只有几十公里，用来填补候选之间的
            空隙。慢一些，但精度显著更高。
            实测同上：目标 2200/2300/2800/3200 偏差均在 1km 内。

        返回 (实际里程米, 路径节点列表, 是否达标)。
        """
        g = self.graph
        target = target_km * 1000.0
        base_len, base_path = g.dijkstra(src, dst)
        if base_path is None:
            return None, None, False
        if target <= 0 or base_len >= target:
            return base_len, base_path, False

        def real_len(p):
            return sum(g.adj[a].get(b, 0.0) for a, b in zip(p, p[1:]))

        # 记录候选：(误差, 里程, 路径)
        best = [abs(base_len - target), base_len, base_path]

        def consider(p):
            if not p:
                return
            r = real_len(p)
            d = abs(r - target)
            if d < best[0]:
                best[:] = [d, r, p]

        # ---- 阶段1：全局惩罚迭代 ----
        candidates = [base_path]
        pen = set()
        cur = base_path
        for _ in range(12):
            for a, b in zip(cur, cur[1:]):
                pen.add((g.pos[a], g.pos[b]))
            _, p2 = g.dijkstra(src, dst, penalty=pen, factor=6.0)
            if not p2:
                break
            candidates.append(p2)
            consider(p2)
            cur = p2
            if abs(real_len(p2) - target) <= 1000.0:
                break

        # ---- ver=2：局部微调（在最优候选附近填补空隙）----
        # 只对里程最接近目标、且仍小于目标的候选做替换
        if ver != 2:
            _, real, p = best
            ok = abs(real - target) <= max(5000.0, target * 0.05)
            return real, p, ok
        near = sorted(candidates, key=lambda q: abs(real_len(q) - target))[:3]
        for cand in near:
            cur_len = real_len(cand)
            if cur_len >= target:
                continue
            if target - cur_len > 1500e3:   # 差距过大，局部替换填不满，跳过
                continue
            for seg in (30, 60, 120, 200):
                n = len(cand)
                if n <= seg + 2:
                    continue
                step = max(1, seg // 2)
                for i in range(1, n - seg - 1, step):
                    u, v = cand[i], cand[i + seg]
                    ban = set()
                    for x, y in zip(cand[i:i + seg], cand[i + 1:i + seg + 1]):
                        ban.add((g.pos[x], g.pos[y]))
                        ban.add((g.pos[y], g.pos[x]))
                    alt = self._shortest_avoiding(u, v, ban)
                    if not alt:
                        continue
                    newp = cand[:i + 1] + alt[1:] + cand[i + seg + 1:]
                    new_len = real_len(newp)
                    # 仅接受让里程更接近目标的替换
                    if abs(new_len - target) < best[0]:
                        consider(newp)
                        cand = newp
                        cur_len = new_len
                    if best[0] <= 500.0:
                        break
                if best[0] <= 500.0 or real_len(cand) >= target:
                    break

        _, real, p = best
        ok = abs(real - target) <= max(5000.0, target * 0.05)
        return real, p, ok

    def _shortest_avoiding(self, u, v, ban):
        """在避开指定有向边集合 ban 的前提下求 u->v 最短路。"""
        g = self.graph
        lats, lons = g.lats, g.lons
        head, edge_to, edge_w = g.head, g.edge_to, g.edge_w
        node_ids, pos = g.node_ids, g.pos
        N = len(node_ids)
        s_i, t_i = pos.get(u), pos.get(v)
        if s_i is None or t_i is None or s_i == t_i:
            return None
        tlat, tlon = lats[v], lons[v]
        INF = float('inf')
        dist = [INF] * N
        prev = [-1] * N
        done = bytearray(N)
        dist[s_i] = 0.0
        pq = [(haversine(lats[u], lons[u], tlat, tlon), 0.0, s_i)]
        hav = haversine
        while pq:
            _, d, i = heapq.heappop(pq)
            if done[i]:
                continue
            done[i] = 1
            if i == t_i:
                break
            for k in range(head[i], head[i + 1]):
                j = edge_to[k]
                if done[j]:
                    continue
                if (i, j) in ban:
                    continue
                nd = d + edge_w[k]
                if nd < dist[j]:
                    dist[j] = nd
                    prev[j] = i
                    heapq.heappush(pq, (nd + hav(lats[node_ids[j]], lons[node_ids[j]], tlat, tlon), nd, j))
        if dist[t_i] == INF:
            return None
        path_i = [t_i]
        c = t_i
        while c != s_i:
            c = prev[c]
            path_i.append(c)
        path_i.reverse()
        return [node_ids[i] for i in path_i]

    def _path_coords(self, path):
        """沿骨架路径拼接中间几何点。

        返回 (完整坐标 [[lon, lat], ...], 跨海轮渡段列表)。

        骨架图的节点是压缩后的岔点/端点，直接连直线会丢失线路走向，
        因此对每条边补上原始 OSM 折线的中间点。

        健壮性处理：
        - 链的方向以「端点离本条边的终点更近」为准（不依赖存储约定）；
        - 去掉与上一点重复的坐标，避免折返；
        - 没有几何链时退化为两节点直线。
        """
        g = self.graph
        lats, lons = g.lats, g.lons
        geom = g.edge_geom
        hav = haversine

        def to_coord(n):
            return [round(lons[n], 6), round(lats[n], 6)]

        out = [to_coord(path[0])]
        last = path[0]
        # 记录跨海轮渡段在坐标序列中的起止下标
        ferries = []
        for a, b in zip(path, path[1:]):
            is_ferry = (tuple(to_coord(a)), tuple(to_coord(b))) in g.ferry_set
            start_idx = len(out) - 1
            chain = geom.get((a, b))
            if not chain:
                seq = []
            else:
                # 判定方向：链的第一点应靠近本条边的起点 a
                d_first = hav(lats[a], lons[a], lats[chain[0]], lons[chain[0]])
                d_last = hav(lats[a], lons[a], lats[chain[-1]], lons[chain[-1]])
                seq = chain if d_first <= d_last else chain[::-1]
                # 若链首恰好是当前起点，跳过它
                if seq and seq[0] == a:
                    seq = seq[1:]
                # 若链尾不是本条边终点，补上终点
                if not seq or seq[-1] != b:
                    seq = list(seq) + [b]
            for n in seq:
                if n == last:
                    continue
                out.append(to_coord(n))
                last = n
            if is_ferry:
                ferries.append({
                    'from': out[start_idx],
                    'to': out[-1],
                    'km': round(hav(lats[a], lons[a], lats[b], lons[b]) / 1000.0, 2),
                })
        return out, ferries

    def _stations_along(self, path, from_code, to_code, tol_m=800.0, coords=None):
        """筛出线路上经过的车站，按沿线里程顺序返回。

        做法：用车站的真实坐标，沿路径逐段计算到铁路折线的距离，
        距离小于 tol_m 即视为线路经过该站，里程取投影点所在位置。

        阈值 tol_m 默认 800m：覆盖站场规模（大型客站到发线可达数百米），
        又能排除相邻并行线路（如高铁与普速并行区段约 1-3km）上的车站。
        """
        g = self.graph
        stations = g.stations
        if not stations:
            return []

        # 只考虑本次两端站以外的、有坐标的车站
        cand = []
        for code, meta in stations.items():
            if code in (from_code, to_code):
                continue
            lat, lon = meta.get('lat'), meta.get('lon')
            if lat is None or lon is None:
                continue
            cand.append((code, lat, lon, meta))
        if not cand:
            return []

        # 路径几何（完整折线，含中间点），已由调用方算好则复用
        if coords is None:
            coords, _ = self._path_coords(path)
        # 累计里程，与坐标一一对应
        cum = [0.0]
        for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
            cum.append(cum[-1] + haversine(y1, x1, y2, x2))

        # 粗筛：用路径节点的经纬度包围盒扩展后过滤，减少精确计算量
        lons_list = [c[0] for c in coords]
        lats_list = [c[1] for c in coords]
        pad = tol_m / 90000.0 + 0.02
        lo_lon, hi_lon = min(lons_list) - pad, max(lons_list) + pad
        lo_lat, hi_lat = min(lats_list) - pad, max(lats_list) + pad
        cand = [c for c in cand if lo_lon <= c[2] <= hi_lon and lo_lat <= c[1] <= hi_lat]

        found = []
        # 把路径折线按网格分桶，只对比车站附近的线段，避免 O(车站×顶点)
        GRID = 0.05  # 约 5km，大于 tol_m
        seg_grid = {}
        for i in range(len(coords) - 1):
            x1, y1 = coords[i]
            x2, y2 = coords[i + 1]
            gx0, gx1 = sorted((int(x1 / GRID), int(x2 / GRID)))
            gy0, gy1 = sorted((int(y1 / GRID), int(y2 / GRID)))
            # 限制单段跨越的桶数，超长段直接放入主线桶
            if gx1 - gx0 > 40 or gy1 - gy0 > 40:
                gx1, gy1 = gx0, gy0
            for gx in range(gx0, gx1 + 1):
                for gy in range(gy0, gy1 + 1):
                    seg_grid.setdefault((gx, gy), []).append(i)

        def near_segs(lat, lon):
            gx, gy = int(lon / GRID), int(lat / GRID)
            out = set()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    out.update(seg_grid.get((gx + dx, gy + dy), ()))
            return out

        for code, lat, lon, meta in cand:
            seg_ids = near_segs(lat, lon)
            if not seg_ids:
                continue
            best_d, best_km = 1e18, 0.0
            for i in seg_ids:
                x1, y1 = coords[i]
                x2, y2 = coords[i + 1]
                d = seg_point_dist(lat, lon, y1, x1, y2, x2)
                if d < best_d:
                    best_d = d
                    best_km = cum[i]
            if best_d <= tol_m:
                found.append({
                    'code': code,
                    'name': meta.get('name'),
                    'lat': round(lat, 6),
                    'lon': round(lon, 6),
                    'km': round(best_km / 1000.0, 2),
                    'offsetM': round(best_d, 0),
                })

        found.sort(key=lambda s: s['km'])
        # 同一位置可能重复（同名站的多电报码），保留偏移最小的
        dedup = {}
        for s in found:
            key = s['name']
            if key not in dedup or s['offsetM'] < dedup[key]['offsetM']:
                dedup[key] = s
        out = sorted(dedup.values(), key=lambda s: s['km'])

        # 同址异名车站去重：如广州北站(国铁)与花都站(城际)物理同站，
        # 两者的邻近线路完全相同，几何上无法区分，故用 station_lanes.json
        # 的人工标注：别名站(城际)在其 aliasOf 指向的国铁站同时出现时剔除。
        lanes = self.station_lanes
        if lanes:
            # 起点/终点站本身虽不列入途经列表，但若与某别名站同址，
            # 该别名站同样不应出现（否则会出现「到虎门站却途经虎门北站」）
            present = {s['code'] for s in out} | {from_code, to_code}
            out = [s for s in out
                   if not (s['code'] in lanes
                           and lanes[s['code']].get('aliasOf') in present)]
        return out


# ---------------------------------------------------------------- HTTP 层

class Handler(BaseHTTPRequestHandler):
    service = None
    server_version = 'RailRouteAPI/1.0'

    def log_message(self, fmt, *args):
        print('[%s] %s' % (self.log_date_time_string(), fmt % args))

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    # 允许托管的静态文件（限于页面所需）
    STATIC = {
        '/index.html': ('index.html', 'text/html; charset=utf-8'),
        '/config.js': ('config.js', 'application/javascript; charset=utf-8'),
    }

    def _send_static(self, path):
        """返回项目根目录下白名单内的静态文件。"""
        entry = self.STATIC.get(path)
        if not entry:
            return False
        fname, ctype = entry
        full = os.path.join(BASE, fname)
        if not os.path.isfile(full):
            self._send(404, {'ok': False, 'error': '文件不存在: %s' % fname})
            return True
        with open(full, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(data)
        return True

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip('/') or '/'
        # 明确按 UTF-8 解码参数，避免中文站名乱码
        qs = urllib.parse.parse_qs(parsed.query, encoding='utf-8', errors='replace')

        # 展示页面：/ 和 /index.html 都返回地图页面
        if path in ('/', '/index.html'):
            if self._send_static('/index.html'):
                return
        if path == '/config.js':
            if self._send_static('/config.js'):
                return

        if path in ('/api/v3', '/health', '/api/v3/health'):
            return self._send(200, {
                'ok': True,
                'service': '中国铁路算路 API',
                'version': 'v3',
                'pages': {'index': 'http://127.0.0.1:%d/' % PORT},
                'endpoints': {
                    'stationRoute': '/api/v3/stationRoute?from=IZQ&to=BXP',
                    'searchStation': '/api/v3/searchStation?q=广州',
                },
            })

        # 站名联想（页面下拉提示用）
        if path == '/api/v3/searchStation':
            q = (qs.get('q') or [''])[0].strip()
            limit = int((qs.get('limit') or ['10'])[0] or 10)
            name_map = self.service.stations._name_map
            if not q:
                return self._send(200, {'ok': True, 'stations': []})
            exact, prefix, fuzzy = [], [], []
            for code, name in name_map.items():
                if name == q or name == q + '站':
                    exact.append((code, name, 0))
                elif name.startswith(q):
                    prefix.append((code, name, 1))
                elif q in name:
                    fuzzy.append((code, name, 2))
            rows = (exact + prefix + fuzzy)[:max(1, min(limit, 50))]
            return self._send(200, {
                'ok': True,
                'stations': [{'code': c, 'name': n} for c, n, _ in rows],
            })

        if path == '/api/v3/stationRoute':
            frm = (qs.get('from') or [None])[0]
            to = (qs.get('to') or [None])[0]
            if not frm or not to:
                return self._send(400, {'ok': False, 'error': '缺少参数',
                                        'usage': '/api/v3/stationRoute?from=IZQ&to=BXP'})
            km_raw = (qs.get('km') or [None])[0]
            target_km = None
            if km_raw not in (None, ''):
                try:
                    target_km = float(km_raw)
                    if target_km <= 0:
                        raise ValueError
                except ValueError:
                    return self._send(400, {'ok': False,
                                            'error': 'km 参数应为正数（单位：公里）',
                                            'got': km_raw})
            # ver：1=整段（默认），2=微调
            ver_raw = (qs.get('ver') or [None])[0]
            ver = 1
            if ver_raw not in (None, ''):
                try:
                    ver = int(ver_raw)
                    if ver not in (1, 2):
                        raise ValueError
                except ValueError:
                    return self._send(400, {'ok': False,
                                            'error': 'ver 参数只能是 1（整段）或 2（微调）',
                                            'got': ver_raw})
            try:
                result = self.service.route(frm, to, target_km, ver)
            except Exception as e:
                print('[error]', e)
                return self._send(500, {'ok': False, 'error': '内部错误: %s' % e})
            return self._send(200 if result.get('ok') else result.get('code', 500), result)

        return self._send(404, {'ok': False, 'error': '接口不存在', 'path': path})


def main():
    if not os.path.exists(GRAPH_PATH):
        raise SystemExit('缺少 %s，请先运行 build_graph.py' % GRAPH_PATH)
    Handler.service = RailService()
    httpd = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print('服务已启动: http://127.0.0.1:%d/api/v3/stationRoute?from=IZQ&to=BXP' % PORT)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止')


if __name__ == '__main__':
    main()

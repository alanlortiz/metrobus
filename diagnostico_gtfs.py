"""
Diagnóstico de una sola vez del GTFS de SinopticoPlus (no envía Telegram ni modifica el repo).
Responde dos preguntas:
  1) ¿Por qué la cache de "paradas de L1" trae estaciones de L2 y L3?
  2) ¿El bearing del GPS indica bien el sentido (norte/sur) de los buses de L1?
Solo imprime en el log del workflow. No imprime credenciales.
"""
import os
import io
import csv
import math
import time
import zipfile
import collections
import requests
from google.transit import gtfs_realtime_pb2

USUARIO = os.environ["USUARIO_API_KEY"]
SENHA = os.environ["SENHA_API_KEY"]
LINEA = "1"
ESPERA_SEG = 45  # tiempo entre las dos fotos del GPS para medir el movimiento real


def titulo(t):
    print(f"\n{'=' * 70}\n{t}\n{'=' * 70}")


def leer_csv(z, nombre):
    with z.open(nombre) as f:
        return list(csv.DictReader(io.TextIOWrapper(f, "utf-8-sig")))


def distancia_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def rumbo(lat1, lon1, lat2, lon2):
    """Rumbo real del movimiento en grados (0 = norte, 90 = este), sentido horario."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def foto_gps(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(r.content)
    return feed


urls = requests.post("https://metrobus-gtfs.sinopticoplus.com/gtfs-api/partnerValidation",
                     json={"usuario": USUARIO, "senha": SENHA}, timeout=20).json()

# ---------------------------------------------------------------- ESTÁTICO
z = zipfile.ZipFile(io.BytesIO(requests.get(urls["urlStatic"], timeout=90).content))
titulo("1. ARCHIVOS EN EL ZIP ESTÁTICO")
for i in z.infolist():
    print(f"  {i.filename:<25} {i.file_size / 1024:>10.0f} KB")

routes = leer_csv(z, "routes.txt")
trips = leer_csv(z, "trips.txt")
stops = {s["stop_id"]: s for s in leer_csv(z, "stops.txt")}

titulo("2. RUTAS CON route_short_name = '1' (todas las columnas)")
print("  columnas routes.txt:", list(routes[0].keys()))
rutas_l1 = set()
for r in routes:
    if r.get("route_short_name", "").strip() == LINEA:
        rutas_l1.add(r["route_id"])
        print("  ", {k: v for k, v in r.items() if v})

titulo("3. trips.txt")
print("  columnas:", list(trips[0].keys()))
rutas_por_trip = collections.defaultdict(set)
for t in trips:
    rutas_por_trip[t["trip_id"]].add(t["route_id"])
repetidos = {k: v for k, v in rutas_por_trip.items() if len(v) > 1}
print(f"  trips: {len(trips)} | trip_id únicos: {len(rutas_por_trip)} | trip_id en VARIAS rutas: {len(repetidos)}")
for k, v in list(repetidos.items())[:5]:
    print(f"    trip_id {k} -> rutas {sorted(v)}")

trips_l1 = [t for t in trips if t["route_id"] in rutas_l1]
print(f"\n  Trips de L1: {len(trips_l1)}")
combo = collections.Counter((t["route_id"], t.get("direction_id", "-"), t.get("trip_headsign", "-")) for t in trips_l1)
print("  (route_id, direction_id, trip_headsign) -> número de trips")
for (rid, did, hs), n in sorted(combo.items()):
    print(f"    {rid:<8} dir={did:<3} '{hs}'  x{n}")

titulo("4. PARADAS QUE RECORRE CADA route_id DE L1 (para encontrar la contaminación)")
trip_a_ruta = {t["trip_id"]: t["route_id"] for t in trips_l1}
ids_l1_exclusivos = {k for k in trip_a_ruta if len(rutas_por_trip[k]) == 1}
paradas_por_ruta = collections.defaultdict(set)
paradas_exclusivas = set()
with z.open("stop_times.txt") as f:
    for fila in csv.DictReader(io.TextIOWrapper(f, "utf-8-sig")):
        tid = fila.get("trip_id")
        if tid in trip_a_ruta:
            paradas_por_ruta[trip_a_ruta[tid]].add(fila.get("stop_id"))
            if tid in ids_l1_exclusivos:
                paradas_exclusivas.add(fila.get("stop_id"))
for rid in sorted(paradas_por_ruta):
    nombres = sorted({stops[s]["stop_name"] for s in paradas_por_ruta[rid] if s in stops})
    print(f"  ruta {rid}: {len(nombres)} paradas -> {', '.join(nombres[:6])} ... {', '.join(nombres[-3:])}")
print(f"\n  Paradas usando solo trip_id exclusivos de L1: {len(paradas_exclusivas)}")

# ---------------------------------------------------------------- TIEMPO REAL
titulo(f"5. GPS: DOS FOTOS SEPARADAS {ESPERA_SEG}s PARA COMPARAR bearing VS MOVIMIENTO REAL")
f1 = foto_gps(urls["urlRealTime"])
time.sleep(ESPERA_SEG)
f2 = foto_gps(urls["urlRealTime"])

tipos = collections.Counter(
    "vehicle" if e.HasField("vehicle") else "trip_update" if e.HasField("trip_update") else "alert" if e.HasField("alert") else "otro"
    for e in f2.entity)
print("  Tipos de entidad en el feed:", dict(tipos))


def por_bus(feed):
    out = {}
    for e in feed.entity:
        if not e.HasField("vehicle"):
            continue
        v = e.vehicle
        if v.trip.route_id in rutas_l1:
            out[v.vehicle.id or e.id] = v
    return out


b1, b2 = por_bus(f1), por_bus(f2)
ahora = f2.header.timestamp or int(time.time())
print(f"  Buses L1 foto 1: {len(b1)} | foto 2: {len(b2)} | en ambas: {len(set(b1) & set(b2))}")

campos = collections.Counter()
for v in b2.values():
    for nombre, presente in (("bearing", v.position.HasField("bearing")), ("speed", v.position.HasField("speed")),
                             ("timestamp", v.HasField("timestamp")), ("trip.direction_id", v.trip.HasField("direction_id")),
                             ("trip.trip_id", bool(v.trip.trip_id))):
        campos[nombre] += presente
print("  Cuántos buses L1 traen cada campo:", dict(campos))

print(f"\n  {'bus':<10}{'route':<8}{'dir':<5}{'bearing':>8}{'rumbo real':>11}{'movió(m)':>10}{'km/h':>7}{'edad(s)':>8}  ¿coincide N/S?")
coincide = total = 0
por_ruta_mov = collections.defaultdict(collections.Counter)
for bid in sorted(set(b1) & set(b2)):
    v1, v2 = b1[bid], b2[bid]
    mov = distancia_m(v1.position.latitude, v1.position.longitude, v2.position.latitude, v2.position.longitude)
    real = rumbo(v1.position.latitude, v1.position.longitude, v2.position.latitude, v2.position.longitude)
    brg = v2.position.bearing if v2.position.HasField("bearing") else None
    did = v2.trip.direction_id if v2.trip.HasField("direction_id") else "-"
    edad = ahora - v2.timestamp if v2.HasField("timestamp") else -1
    vel = v2.position.speed * 3.6 if v2.position.HasField("speed") else -1
    marca = ""
    if mov >= 40:
        real_n = real < 90 or real > 270
        por_ruta_mov[v2.trip.route_id]["N" if real_n else "S"] += 1
        if brg is not None:
            total += 1
            ok = (brg < 90 or brg > 270) == real_n
            coincide += ok
            marca = "sí" if ok else "NO"
    brg_txt = f"{brg:.0f}" if brg is not None else "-"
    print(f"  {bid[:9]:<10}{v2.trip.route_id:<8}{str(did):<5}{brg_txt:>8}{real:>11.0f}{mov:>10.0f}{vel:>7.1f}{edad:>8}  {marca}")

titulo("6. RESUMEN")
if total:
    print(f"  bearing coincide con el movimiento real (N/S) en {coincide}/{total} buses ({coincide / total:.0%})")
else:
    print("  Ningún bus se movió lo suficiente para comparar (prueba en hora con tráfico).")
print("  Sentido real observado por route_id (buses que se movieron >= 40 m):")
for rid, c in sorted(por_ruta_mov.items()):
    print(f"    {rid}: {dict(c)}")
print("\nFin del diagnóstico.")

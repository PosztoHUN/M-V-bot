from urllib import response
from datetime import datetime, UTC
import discord
from discord.ext import commands, tasks
import aiohttp
import os
import sys
import io
import csv
import zipfile
import asyncio
import requests
from datetime import UTC, datetime, timedelta
from collections import defaultdict
from supabase import create_client
from google.transit import gtfs_realtime_pb2

# =======================
# BEÁLLÍTÁSOK
# =======================

TOKEN = os.getenv("TOKEN")

MAV_BASE = "https://mavplusz.hu"
JWT_URL = f"{MAV_BASE}/otp2-backend/otp/auth/get-jwt"
GRAPHQL_URL = f"{MAV_BASE}/otp2-backend/otp/routers/default/index/graphql"

REQ_TIMEOUT = aiohttp.ClientTimeout(total=25)
ACTIVE_STATUSES = {"IN_TRANSIT_TO", "STOPPED_AT", "IN_PROGRESS"}

GRAPHQL_QUERY = """
query VehiclePositions($swLat: Float!, $swLon: Float!, $neLat: Float!, $neLon: Float!) {
  vehiclePositions(swLat: $swLat, swLon: $swLon, neLat: $neLat, neLon: $neLon) {
    vehicleId
    lat
    lon
    heading
    vehicleModel
    label
    lastUpdated
    speed
    stopRelationship {
      status
      stop {
        gtfsId
        name
      }
      arrivalTime
      departureTime
    }
    trip {
      id
      gtfsId
      routeShortName
      tripHeadsign
      tripShortName
      route {
        mode
        shortName
        longName
        textColor
        color
      }
      pattern {
        id
      }
      serviceDate
    }
    nextStop {
      arrivalDelay
    }
    prevOrCurrentStop {
      scheduledArrival
      realtimeArrival
      arrivalDelay
      scheduledDeparture
      realtimeDeparture
      departureDelay
    }
  }
}
"""

COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": f"{MAV_BASE}/",
    "Origin": MAV_BASE,
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, /",
}

async def fetch_mav_vehicles():
    payload = {
        "query": GRAPHQL_QUERY,
        "variables": {
            "swLat": 47.2,
            "swLon": 18.7,
            "neLat": 47.75,
            "neLon": 19.6
        }
    }

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": MAV_BASE,
        "Referer": MAV_BASE + "/"
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(GRAPHQL_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    return []
                resp_json = await r.json()
                vehicles = resp_json.get("data", {}).get("vehiclePositions", [])
                return vehicles
        except Exception as e:
            print("Fetch hiba:", e)
            return []

LOCK_FILE = "/tmp/discord_bot.lock"
DISCORD_LIMIT = 1900

if os.path.exists(LOCK_FILE):
    print("A bot már fut, kilépés.")
    sys.exit(0)

active_today_villamos = {}
active_today_combino = {}
active_today_caf5 = {}
active_today_caf9 = {}
active_today_tatra = {}
today_data = {}

# =======================
# GTFS / HELYKITÖLTŐK
# =======================

GTFS_PATH = ""
TXT_URL = ""

TRIPS_META = {}
STOPS = {}
TRIP_START = {}
TRIP_STOPS = defaultdict(list)
SERVICE_DATES = defaultdict(dict)
ROUTES = defaultdict(lambda: defaultdict(list))

# =======================
# DISCORD INIT
# =======================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=".", intents=intents)

# ─────────────────────────────────────────────
# HTTP / FEED SEGÉD
# ─────────────────────────────────────────────

UA_HEADERS = {
    "User-Agent": "BKK-DiscordBot/1.0 (+https://discord.com)"
}

def _http_get(url: str, timeout: int = 15) -> requests.Response:
    r = requests.get(url, headers=UA_HEADERS, timeout=timeout)
    if r.status_code != 200:
        snippet = (r.text or "")[:200].replace("\n", " ").replace("\r", " ")
        raise RuntimeError(f"HTTP {r.status_code} {r.reason}. Válasz eleje: {snippet}")
    return r

def fetch_pb_feed() -> gtfs_realtime_pb2.FeedMessage:
    r = _http_get(PB_URL)
    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(r.content)
    except Exception as e:
        snippet = (r.content[:200] or b"").decode("utf-8", errors="replace").replace("\n", " ").replace("\r", " ")
        raise RuntimeError(f"PB parse hiba: {e}. Tartalom eleje: {snippet}")
    return feed

def fetch_txt_raw() -> str:
    r = _http_get(TXT_URL)
    return r.text or ""


# =======================
# SEGÉDFÜGGVÉNYEK
# =======================
def chunk_embeds(title_base, entries, color=0x003200, max_fields=20):
    embeds = []
    embed = discord.Embed(title=title_base, color=color)
    field_count = 0

    for reg, info in sorted(entries.items(), key=lambda x: x[0]):
        lat = info.get('lat')
        lon = info.get('lon')
        dest = info.get('dest', 'Ismeretlen')

        lat_str = f"{lat:.5f}" if lat is not None else "Ismeretlen"
        lon_str = f"{lon:.5f}" if lon is not None else "Ismeretlen"

        value = f"Cél: {dest}\nPozíció: {lat_str}, {lon_str}"

        if field_count >= max_fields:
            embeds.append(embed)
            embed = discord.Embed(title=f"{title_base} (folytatás)", color=color)
            field_count = 0

        embed.add_field(name=reg, value=value, inline=False)
        field_count += 1

    embeds.append(embed)
    return embeds

async def fetch_mav_vehicles():
    """Lekérdezi a MÁV járműveket a GraphQL API-ról Magyarország teljes területére."""
    query = """
    query VehiclePositions($swLat: Float!, $swLon: Float!, $neLat: Float!, $neLon: Float!) {
      vehiclePositions(swLat: $swLat, swLon: $swLon, neLat: $neLat, neLon: $neLon) {
        vehicleId
        lat
        lon
        heading
        vehicleModel
        label
        licensePlate
        uicCode
        lastUpdated
        speed
        stopRelationship {
          status
          stop {
            gtfsId
            name
          }
          arrivalTime
          departureTime
        }
        trip {
          id
          gtfsId
          routeShortName
          tripHeadsign
          tripShortName
          route {
            mode
            shortName
            longName
            textColor
            color
          }
          pattern {
            id
          }
          serviceDate
        }
        nextStop {
          arrivalDelay
          stop {
            name
          }
        }
        prevOrCurrentStop {
          scheduledArrival
          realtimeArrival
          arrivalDelay
          scheduledDeparture
          realtimeDeparture
          departureDelay
        }
      }
    }
    """
    variables = {
        "swLat": 45.7, "swLon": 16.0,  # Dél-nyugat
        "neLat": 48.6, "neLon": 22.9   # Észak-kelet
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(GRAPHQL_URL, json={"query": query, "variables": variables}) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json()
            vehicles = data.get("data", {}).get("vehiclePositions", [])
            result = {}
            for v in vehicles:
                vid = v.get("vehicleId")
                if not vid:
                    continue
                # Teljes nextStop objektum default értékkel, ha nincs
                result[vid] = {
                    "lat": v.get("lat"),
                    "lon": v.get("lon"),
                    "vehicleModel": v.get("vehicleModel"),
                    "speed": v.get("speed"),
                    "uicCode": v.get("uicCode"),
                    "tripHeadsign": v.get("trip", {}).get("tripHeadsign"),
                    "tripShortName": v.get("trip", {}).get("tripShortName"),
                    "mode": v.get("trip", {}).get("route", {}).get("mode"),
                    "nextStop": v.get("nextStop") or {"arrivalDelay": None, "stop": {"name": "Ismeretlen"}}
                }
            return result
        
        
# =======================
# Logger loop
# =======================

active_mav_vehicles = {}

active_mav_vehicles = {}

@tasks.loop(seconds=30)
async def logger_loop_mav():
    """Frissíti a MÁV járművek állapotát Magyarország teljes területére vonatkozóan."""
    try:
        vehicles = await fetch_mav_vehicles()  # a korábbi fetch függvény
    except Exception as e:
        print(f"Hiba a járművek lekérésekor: {e}")
        return

    now = datetime.now()
    active_mav_vehicles.clear()

    for vid, v in vehicles.items():
        lat = v.get("lat")
        lon = v.get("lon")
        dest = v.get("tripHeadsign") or "Ismeretlen"

        active_mav_vehicles[vid] = {
            "lat": lat,
            "lon": lon,
            "dest": dest,
            "vehicleModel": v.get("vehicleModel"),
            "speed": v.get("speed"),
            "uicCode": v.get("uicCode"),
            "tripShortName": v.get("tripShortName"),
            "mode": v.get("mode"),
            "nextStop": v.get("nextStop"),
            "last_seen": now
        }

# =======================
# PARANCSOK - Vonatok
# =======================

@bot.command()
async def vezerlokocsik(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés csak uicCode 6-8 karakter "815"
    kiss_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("uicCode") is not None
        and len(data["uicCode"]) >= 8
        and data["uicCode"][4:8] == "8005"
    ]

    if not kiss_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (uicCode 6-11 karakter)
    kiss_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in kiss_vehicles:
        uic = v.get("uicCode", "")
        if len(uic) >= 11:
            payaszam = uic[4:8] + " " + uic[8:11] + "-" + (uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚆 Aktív vezérlőkocsik",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚆 Aktív vezérlőkocsik",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)

@bot.command()
async def bz(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés csak uicCode 6-8 karakter "815"
    kiss_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("uicCode") is not None
        and len(data["uicCode"]) >= 8
        and (data["uicCode"][5:8] == "117" or data["uicCode"][5:8] == "127" or data["uicCode"][5:8] == "136")
    ]

    if not kiss_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (uicCode 6-11 karakter)
    kiss_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in kiss_vehicles:
        uic = v.get("uicCode", "")
        if len(uic) >= 11:
            payaszam = uic[5:8] + " " + uic[8:11] + "-" + (uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚆 Aktív Bzmot motorkocsik",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚆 Aktív Bzmot motorkocsik",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def vectron(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés csak uicCode 6-8 karakter "815"
    kiss_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("uicCode") is not None
        and len(data["uicCode"]) >= 8
        and (data["uicCode"][5:8] == "193" or data["uicCode"][5:8] == "471")
    ]

    if not kiss_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (uicCode 6-11 karakter)
    kiss_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in kiss_vehicles:
        uic = v.get("uicCode", "")
        if len(uic) >= 11:
            payaszam = uic[5:8] + " " + uic[8:11] + "-" + (uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚆 Aktív Vectron mozdonyok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚆 Aktív Vectron mozdonyok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)

@bot.command()
async def uzsgyi(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés csak uicCode 6-8 karakter "815"
    kiss_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("uicCode") is not None
        and len(data["uicCode"]) >= 8
        and data["uicCode"][5:8] == "416"
    ]

    if not kiss_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (uicCode 6-11 karakter)
    kiss_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in kiss_vehicles:
        uic = v.get("uicCode", "")
        if len(uic) >= 11:
            payaszam = uic[5:8] + " " + uic[8:11] + "-" + (uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚆 Aktív Uzsgyi motorvonatok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚆 Aktív Uzsgyi motorvonatok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def bdv(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés csak uicCode 6-8 karakter "815"
    kiss_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("uicCode") is not None
        and len(data["uicCode"]) >= 8
        and data["uicCode"][5:8] == "414"
    ]

    if not kiss_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (uicCode 6-11 karakter)
    kiss_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in kiss_vehicles:
        uic = v.get("uicCode", "")
        if len(uic) >= 11:
            payaszam = uic[5:8] + " " + uic[8:11] + "-" + (uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚆 Aktív BDVmot motorvonatok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚆 Aktív BDVmot motorvonatok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def flirt(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés csak uicCode 6-8 karakter "815"
    kiss_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("uicCode") is not None
        and len(data["uicCode"]) >= 8
        and data["uicCode"][5:8] == "415" or data["uicCode"][5:8] == "435"
    ]

    if not kiss_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (uicCode 6-11 karakter)
    kiss_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in kiss_vehicles:
        uic = v.get("uicCode", "")
        if len(uic) >= 11:
            payaszam = uic[5:8] + " " + uic[8:11] + "-" + (uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚆 Aktív STADLER FLIRT motorvonatok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚆 Aktív STADLER FLIRT motorvonatok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def m40(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés csak uicCode 6-8 karakter "815"
    kiss_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("uicCode") is not None
        and len(data["uicCode"]) >= 8
        and data["uicCode"][5:8] == "408"
    ]

    if not kiss_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (uicCode 6-11 karakter)
    kiss_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in kiss_vehicles:
        uic = v.get("uicCode", "")
        if len(uic) >= 11:
            payaszam = uic[5:8] + " " + uic[8:11] + "-" + (uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚆 Aktív M40 mozdonyok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚆 Aktív M40 mozdonyok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def m41(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés csak uicCode 6-8 karakter "815"
    kiss_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("uicCode") is not None
        and len(data["uicCode"]) >= 8
        and data["uicCode"][5:8] == "418"
    ]

    if not kiss_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (uicCode 6-11 karakter)
    kiss_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in kiss_vehicles:
        uic = v.get("uicCode", "")
        if len(uic) >= 11:
            payaszam = uic[5:8] + " " + uic[8:11] + "-" + (uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚆 Aktív M41 mozdonyok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚆 Aktív M41 mozdonyok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def bvh(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés csak uicCode 6-8 karakter "815"
    kiss_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("uicCode") is not None
        and len(data["uicCode"]) >= 8
        and data["uicCode"][5:8] == "424"
    ]

    if not kiss_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (uicCode 6-11 karakter)
    kiss_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in kiss_vehicles:
        uic = v.get("uicCode", "")
        if len(uic) >= 11:
            payaszam = uic[5:8] + " " + uic[8:11] + "-" + (uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚆 Aktív BVhmot motorvonatok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚆 Aktív BVhmot motorvonatok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def v43(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés csak uicCode 6-8 karakter "815"
    kiss_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("uicCode") is not None
        and len(data["uicCode"]) >= 8
        and (data["uicCode"][5:8] == "430" or data["uicCode"][5:8] == "431" or data["uicCode"][5:8] == "432" or data["uicCode"][5:8] == "433")
    ]

    if not kiss_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (uicCode 6-11 karakter)
    kiss_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in kiss_vehicles:
        uic = v.get("uicCode", "")
        if len(uic) >= 11:
            payaszam = uic[5:8] + " " + uic[8:11] + "-" + (uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚆 Aktív V43 mozdonyok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚆 Aktív V43 mozdonyok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def talent(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés csak uicCode 6-8 karakter "815"
    kiss_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("uicCode") is not None
        and len(data["uicCode"]) >= 8
        and data["uicCode"][5:8] == "425"
    ]

    if not kiss_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (uicCode 6-11 karakter)
    kiss_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in kiss_vehicles:
        uic = v.get("uicCode", "")
        if len(uic) >= 11:
            payaszam = uic[5:8] + " " + uic[8:11] + "-" + (uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚆 Aktív TALENT motorvonatok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚆 Aktív TALENT motorvonatok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def desiro(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés csak uicCode 6-8 karakter "815"
    kiss_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("uicCode") is not None
        and len(data["uicCode"]) >= 8
        and data["uicCode"][5:8] == "426"
    ]

    if not kiss_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (uicCode 6-11 karakter)
    kiss_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in kiss_vehicles:
        uic = v.get("uicCode", "")
        if len(uic) >= 11:
            payaszam = uic[5:8] + " " + uic[8:11] + "-" + (uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚆 Aktív Siemens Desiro motorvonatok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚆 Aktív Siemens Desiro motorvonatok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def bv(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés csak uicCode 6-8 karakter "815"
    kiss_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("uicCode") is not None
        and len(data["uicCode"]) >= 8
        and data["uicCode"][5:8] == "434"
    ]

    if not kiss_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (uicCode 6-11 karakter)
    kiss_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in kiss_vehicles:
        uic = v.get("uicCode", "")
        if len(uic) >= 11:
            payaszam = uic[5:8] + " " + uic[8:11] + "-" + (uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚆 Aktív BVmot motorvonatok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚆 Aktív BVmot motorvonatok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def m43(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés csak uicCode 6-8 karakter "815"
    kiss_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("uicCode") is not None
        and len(data["uicCode"]) >= 8
        and (data["uicCode"][5:8] == "438" or data["uicCode"][5:8] == "439")
    ]

    if not kiss_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (uicCode 6-11 karakter)
    kiss_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in kiss_vehicles:
        uic = v.get("uicCode", "")
        if len(uic) >= 11:
            payaszam = uic[5:8] + " " + uic[8:11] + "-" + (uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚆 Aktív V63 mozdonyok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚆 Aktív V63 mozdonyok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def m44(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés csak uicCode 6-8 karakter "815"
    kiss_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("uicCode") is not None
        and len(data["uicCode"]) >= 8
        and data["uicCode"][5:8] == "448"
    ]

    if not kiss_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (uicCode 6-11 karakter)
    kiss_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in kiss_vehicles:
        uic = v.get("uicCode", "")
        if len(uic) >= 11:
            payaszam = uic[5:8] + " " + uic[8:11] + "-" + (uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚆 Aktív M44 mozdonyok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚆 Aktív M44 mozdonyok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def jenbacher(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés csak uicCode 6-8 karakter "815"
    kiss_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("uicCode") is not None
        and len(data["uicCode"]) >= 8
        and (data["uicCode"][5:8] == "446" or data["uicCode"][4:8] == "5147" or data["uicCode"][3:8] == "247")
    ]

    if not kiss_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (uicCode 6-11 karakter)
    kiss_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in kiss_vehicles:
        uic = v.get("uicCode", "")
        if len(uic) >= 11:
            payaszam = uic[5:8] + " " + uic[8:11] + "-" + (uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚆 Aktív M44 mozdonyok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚆 Aktív M44 mozdonyok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def v46(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés csak uicCode 6-8 karakter "815"
    kiss_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("uicCode") is not None
        and len(data["uicCode"]) >= 8
        and data["uicCode"][5:8] == "460"
    ]

    if not kiss_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (uicCode 6-11 karakter)
    kiss_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in kiss_vehicles:
        uic = v.get("uicCode", "")
        if len(uic) >= 11:
            payaszam = uic[5:8] + " " + uic[8:11] + "-" + (uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚆 Aktív V46 mozdonyok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚆 Aktív V46 mozdonyok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def taurus(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés uicCode alapján
    taurus_vehicles = []
    for vid, data in all_vehicles.items():
        uic = data.get("uicCode")
        if not uic or len(uic) < 8:
            continue
        # Szűrés: 470, 186, 1116 (biztonságosan)
        if uic[5:8] == "470" or uic[5:8] == "182" or (len(uic) >= 8 and uic[4:8] == "1116"):
            taurus_vehicles.append({"vehicleId": vid, **data})

    if not taurus_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (6–11 karakter)
    taurus_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in taurus_vehicles:
        uic = v.get("uicCode", "")
        # Pályaszám formázása: 6-11 karakter, 8 után szóköz
        if len(uic) >= 11:
            payaszam = uic[5:8] + " " + uic[8:11] + ("-" + uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            title = "🚆 Aktív Siemens Taurus mozdonyok (folytatás)" if embeds else "🚆 Aktív Siemens Taurus mozdonyok"
            embed = discord.Embed(
                title=title,
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        title = "🚆 Aktív Siemens Taurus mozdonyok (folytatás)" if embeds else "🚆 Aktív Siemens Taurus mozdonyok"
        embed = discord.Embed(
            title=title,
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def ventus(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés csak uicCode 6-8 karakter "815"
    kiss_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("uicCode") is not None
        and len(data["uicCode"]) >= 8
        and data["uicCode"][4:8] == "4744"
    ]

    if not kiss_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (uicCode 6-11 karakter)
    kiss_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in kiss_vehicles:
        uic = v.get("uicCode", "")
        if len(uic) >= 11:
            payaszam = uic[4:8] + " " + uic[8:11] + "-" + (uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚆 Aktív Siemens Desiro ML motorvonatok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚆 Aktív Siemens Desiro ML motorvonatok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def m47(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés csak uicCode 6-8 karakter "815"
    kiss_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("uicCode") is not None
        and len(data["uicCode"]) >= 8
        and data["uicCode"][5:8] == "478"
    ]

    if not kiss_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (uicCode 6-11 karakter)
    kiss_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in kiss_vehicles:
        uic = v.get("uicCode", "")
        if len(uic) >= 11:
            payaszam = uic[5:8] + " " + uic[8:11] + "-" + (uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚆 Aktív M47 mozdonyok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚆 Aktív M47 mozdonyok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def traxx(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés csak uicCode 6-8 karakter "815"
    kiss_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("uicCode") is not None
        and len(data["uicCode"]) >= 8
        and data["uicCode"][5:8] == "480"
    ]

    if not kiss_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (uicCode 6-11 karakter)
    kiss_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in kiss_vehicles:
        uic = v.get("uicCode", "")
        if len(uic) >= 11:
            payaszam = uic[5:8] + " " + uic[8:11] + "-" + (uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚆 Aktív Traxx mozdonyok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚆 Aktív Traxx mozdonyok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def astride(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés csak uicCode 6-8 karakter "815"
    kiss_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("uicCode") is not None
        and len(data["uicCode"]) >= 8
        and data["uicCode"][5:8] == "490"
    ]

    if not kiss_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (uicCode 6-11 karakter)
    kiss_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in kiss_vehicles:
        uic = v.get("uicCode", "")
        if len(uic) >= 11:
            payaszam = uic[5:8] + " " + uic[8:11] + "-" + (uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚆 Aktív GEC-Alsthom BB36000 mozdonyok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚆 Aktív GEC-Alsthom BB36000 mozdonyok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def m62(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés csak uicCode 6-8 karakter "815"
    kiss_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("uicCode") is not None
        and len(data["uicCode"]) >= 8
        and data["uicCode"][5:8] == "628"
    ]

    if not kiss_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (uicCode 6-11 karakter)
    kiss_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in kiss_vehicles:
        uic = v.get("uicCode", "")
        if len(uic) >= 11:
            payaszam = uic[5:8] + " " + uic[8:11] + "-" + (uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚆 Aktív M62 mozdonyok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚆 Aktív M62 mozdonyok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def v63(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés csak uicCode 6-8 karakter "815"
    kiss_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("uicCode") is not None
        and len(data["uicCode"]) >= 8
        and data["uicCode"][5:8] == "630"
    ]

    if not kiss_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (uicCode 6-11 karakter)
    kiss_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in kiss_vehicles:
        uic = v.get("uicCode", "")
        if len(uic) >= 11:
            payaszam = uic[5:8] + " " + uic[8:11] + "-" + (uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚆 Aktív V63 mozdonyok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚆 Aktív V63 mozdonyok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)

@bot.command()
async def kiss(ctx):
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés csak uicCode 6-8 karakter "815"
    kiss_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("uicCode") is not None
        and len(data["uicCode"]) >= 8
        and data["uicCode"][5:8] == "815"
    ]

    if not kiss_vehicles:
        await ctx.send("Nincsenek aktív járművek az API-ban a megadott uicCode feltétellel.")
        return

    # Rendezés pályaszám szerint (uicCode 6-11 karakter)
    kiss_vehicles.sort(key=lambda v: v["uicCode"][5:11] if len(v["uicCode"]) >= 11 else "000000")

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in kiss_vehicles:
        uic = v.get("uicCode", "")
        if len(uic) >= 11:
            payaszam = uic[5:8] + " " + uic[8:11] + "-" + (uic[11:12] if len(uic) > 11 else "")
        else:
            payaszam = uic

        trip_short = v.get("tripShortName", "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()]) or "Ismeretlen"
        cel = v.get("tripHeadsign") or "Ismeretlen"

        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Vonatszám: {vonatszam}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚆 Aktív STADLER KISS motorvonatok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚆 Aktív STADLER KISS motorvonatok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)

# =======================
# PARANCSOK - Buszok
# =======================

@bot.command()
async def ikarus(ctx):
    """Kiírja az összes nem budapesti, nem BKK-s Ikarus járművet ABC sorrendben típusa és rendszáma szerint."""
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés: csak Ikarus járművek
    ikarus_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("vehicleModel") and "IKARUS" in data["vehicleModel"].upper()
    ]

    # Budapest és BKK kizárása
    ikarus_vehicles = [
        v for v in ikarus_vehicles
        if not (v.get("vehicleId", "").startswith("BKK:"))
        and "Budapest" not in (v.get("tripHeadsign") or "")
        and "Budapest" not in v.get("nextStop", {}).get("stop", {}).get("name", "")
    ]

    if not ikarus_vehicles:
        await ctx.send("Nincsenek Ikarus járművek az API-ban.")
        return

    # Rendszám kivágása
    for v in ikarus_vehicles:
        vehicle_id = v.get("vehicleId", "")
        if "hkir_" in vehicle_id:
            v["reg"] = vehicle_id.split("hkir_")[-1]
        else:
            v["reg"] = vehicle_id

    # Rendezés: először típusa (ABC), majd rendszám szerint
    ikarus_vehicles.sort(key=lambda x: (x.get("vehicleModel", ""), x.get("reg", "")))

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in ikarus_vehicles:
        reg = v["reg"]
        typus = v.get("vehicleModel") or "Ismeretlen"
        vonal = v.get("tripShortName") or "—"
        cel = v.get("tripHeadsign") or "Ismeretlen"
        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{reg}**\n"
            f"Típus: {typus}\n"
            f"Vonal: {vonal}\n"
            f"Célállomás: {cel}\n"
            f"Következő megálló: {next_stop}\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚍 Aktív Ikarus autóbuszok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚍 Aktív Ikarus autóbuszok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def credo(ctx):
    """Kiírja az összes nem budapesti, nem BKK-s Credo járművet ABC sorrendben típusa és rendszáma szerint."""
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés: csak Credo járművek
    credo_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("vehicleModel") and "CREDO" in data["vehicleModel"].upper()
    ]

    # Budapest és BKK kizárása
    credo_vehicles = [
        v for v in credo_vehicles
        if not (v.get("vehicleId", "").startswith("BKK:"))
        and "Budapest" not in (v.get("tripHeadsign") or "")
        and "Budapest" not in v.get("nextStop", {}).get("stop", {}).get("name", "")
    ]

    if not credo_vehicles:
        await ctx.send("Nincsenek Credo járművek az API-ban.")
        return

    # Rendszám kivágása
    for v in credo_vehicles:
        vehicle_id = v.get("vehicleId", "")
        if "hkir_" in vehicle_id:
            v["reg"] = vehicle_id.split("hkir_")[-1]
        else:
            v["reg"] = vehicle_id

    # Rendezés: először típusa (ABC), majd rendszám szerint
    credo_vehicles.sort(key=lambda x: (x.get("vehicleModel", ""), x.get("reg", "")))

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in credo_vehicles:
        reg = v["reg"]
        typus = v.get("vehicleModel") or "Ismeretlen"
        vonal = v.get("tripShortName") or "—"
        cel = v.get("tripHeadsign") or "Ismeretlen"
        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{reg}**\n"
            f"Típus: {typus}\n"
            f"Vonal: {vonal}\n"
            f"Célállomás: {cel}\n"
            f"Következő megálló: {next_stop}\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚍 Aktív Credo autóbuszok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚍 Aktív Credo autóbuszok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def man(ctx):
    """Kiírja az összes nem budapesti, nem BKK-s MAN járművet ABC sorrendben típusa és rendszáma szerint."""
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés: csak MAN járművek
    man_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("vehicleModel") and "MAN" in data["vehicleModel"].upper()
    ]

    # Budapest és BKK kizárása
    man_vehicles = [
        v for v in man_vehicles
        if not (v.get("vehicleId", "").startswith("BKK:"))
        and "Budapest" not in (v.get("tripHeadsign") or "")
        and "Budapest" not in v.get("nextStop", {}).get("stop", {}).get("name", "")
    ]

    if not man_vehicles:
        await ctx.send("Nincsenek MAN járművek az API-ban.")
        return

    # Rendszám kivágása
    for v in man_vehicles:
        vehicle_id = v.get("vehicleId", "")
        if "hkir_" in vehicle_id:
            v["reg"] = vehicle_id.split("hkir_")[-1]
        else:
            v["reg"] = vehicle_id

    # Rendezés: először típusa (ABC), majd rendszám szerint
    man_vehicles.sort(key=lambda x: (x.get("vehicleModel", ""), x.get("reg", "")))

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in man_vehicles:
        reg = v["reg"]
        typus = v.get("vehicleModel") or "Ismeretlen"
        vonal = v.get("tripShortName") or "—"
        cel = v.get("tripHeadsign") or "Ismeretlen"
        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{reg}**\n"
            f"Típus: {typus}\n"
            f"Vonal: {vonal}\n"
            f"Célállomás: {cel}\n"
            f"Következő megálló: {next_stop}\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚍 Aktív MAN autóbuszok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚍 Aktív MAN autóbuszok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def volvo(ctx):
    """Kiírja az összes nem budapesti, nem BKK-s Volvo járművet ABC sorrendben típusa és rendszáma szerint."""
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés: csak Volvo járművek
    volvo_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("vehicleModel") and "VOLVO" in data["vehicleModel"].upper()
    ]

    # Budapest és BKK kizárása
    volvo_vehicles = [
        v for v in volvo_vehicles
        if not (v.get("vehicleId", "").startswith("BKK:"))
        and "Budapest" not in (v.get("tripHeadsign") or "")
        and "Budapest" not in v.get("nextStop", {}).get("stop", {}).get("name", "")
    ]

    if not volvo_vehicles:
        await ctx.send("Nincsenek Volvo járművek az API-ban.")
        return

    # Rendszám kivágása
    for v in volvo_vehicles:
        vehicle_id = v.get("vehicleId", "")
        if "hkir_" in vehicle_id:
            v["reg"] = vehicle_id.split("hkir_")[-1]
        else:
            v["reg"] = vehicle_id

    # Rendezés: először típusa (ABC), majd rendszám szerint
    volvo_vehicles.sort(key=lambda x: (x.get("vehicleModel", ""), x.get("reg", "")))

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in volvo_vehicles:
        reg = v["reg"]
        typus = v.get("vehicleModel") or "Ismeretlen"
        vonal = v.get("tripShortName") or "—"
        cel = v.get("tripHeadsign") or "Ismeretlen"
        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{reg}**\n"
            f"Típus: {typus}\n"
            f"Vonal: {vonal}\n"
            f"Célállomás: {cel}\n"
            f"Következő megálló: {next_stop}\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚍 Aktív Volvo autóbuszok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚍 Aktív Volvo autóbuszok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def mercedes(ctx):
    """Kiírja az összes nem budapesti, nem BKK-s Mercedes járművet ABC sorrendben típusa és rendszáma szerint."""
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés: csak Mercedes járművek
    mercedes_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("vehicleModel") and "MERCEDES" in data["vehicleModel"].upper()
    ]

    # Budapest és BKK kizárása
    mercedes_vehicles = [
        v for v in mercedes_vehicles
        if not (v.get("vehicleId", "").startswith("BKK:"))
        and "Budapest" not in (v.get("tripHeadsign") or "")
        and "Budapest" not in v.get("nextStop", {}).get("stop", {}).get("name", "")
    ]

    if not mercedes_vehicles:
        await ctx.send("Nincsenek Mercedes járművek az API-ban.")
        return

    # Rendszám kivágása
    for v in mercedes_vehicles:
        vehicle_id = v.get("vehicleId", "")
        if "hkir_" in vehicle_id:
            v["reg"] = vehicle_id.split("hkir_")[-1]
        else:
            v["reg"] = vehicle_id

    # Rendezés: először típusa (ABC), majd rendszám szerint
    mercedes_vehicles.sort(key=lambda x: (x.get("vehicleModel", ""), x.get("reg", "")))

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in mercedes_vehicles:
        reg = v["reg"]
        typus = v.get("vehicleModel") or "Ismeretlen"
        vonal = v.get("tripShortName") or "—"
        cel = v.get("tripHeadsign") or "Ismeretlen"
        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{reg}**\n"
            f"Típus: {typus}\n"
            f"Vonal: {vonal}\n"
            f"Célállomás: {cel}\n"
            f"Következő megálló: {next_stop}\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚍 Aktív Mercedes autóbuszok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚍 Aktív Mercedes autóbuszok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def setra(ctx):
    """Kiírja az összes nem budapesti, nem BKK-s Setra járművet ABC sorrendben típusa és rendszáma szerint."""
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés: csak Setra járművek
    setra_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("vehicleModel") and "SETRA" in data["vehicleModel"].upper()
    ]

    # Budapest és BKK kizárása
    setra_vehicles = [
        v for v in setra_vehicles
        if not (v.get("vehicleId", "").startswith("BKK:"))
        and "Budapest" not in (v.get("tripHeadsign") or "")
        and "Budapest" not in v.get("nextStop", {}).get("stop", {}).get("name", "")
    ]

    if not setra_vehicles:
        await ctx.send("Nincsenek Setra járművek az API-ban.")
        return

    # Rendszám kivágása
    for v in setra_vehicles:
        vehicle_id = v.get("vehicleId", "")
        if "hkir_" in vehicle_id:
            v["reg"] = vehicle_id.split("hkir_")[-1]
        else:
            v["reg"] = vehicle_id

    # Rendezés: először típusa (ABC), majd rendszám szerint
    setra_vehicles.sort(key=lambda x: (x.get("vehicleModel", ""), x.get("reg", "")))

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in setra_vehicles:
        reg = v["reg"]
        typus = v.get("vehicleModel") or "Ismeretlen"
        vonal = v.get("tripShortName") or "—"
        cel = v.get("tripHeadsign") or "Ismeretlen"
        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{reg}**\n"
            f"Típus: {typus}\n"
            f"Vonal: {vonal}\n"
            f"Célállomás: {cel}\n"
            f"Következő megálló: {next_stop}\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚍 Aktív Setra autóbuszok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚍 Aktív Setra autóbuszok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def nabi(ctx):
    """Kiírja az összes nem budapesti, nem BKK-s NABI járművet ABC sorrendben típusa és rendszáma szerint."""
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés: csak NABI járművek
    nabi_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("vehicleModel") and "NABI" in data["vehicleModel"].upper()
    ]

    # Budapest és BKK kizárása
    nabi_vehicles = [
        v for v in nabi_vehicles
        if not (v.get("vehicleId", "").startswith("BKK:"))
        and "Budapest" not in (v.get("tripHeadsign") or "")
        and "Budapest" not in v.get("nextStop", {}).get("stop", {}).get("name", "")
    ]

    if not nabi_vehicles:
        await ctx.send("Nincsenek NABI járművek az API-ban.")
        return

    # Rendszám kivágása
    for v in nabi_vehicles:
        vehicle_id = v.get("vehicleId", "")
        if "hkir_" in vehicle_id:
            v["reg"] = vehicle_id.split("hkir_")[-1]
        else:
            v["reg"] = vehicle_id

    # Rendezés: először típusa (ABC), majd rendszám szerint
    nabi_vehicles.sort(key=lambda x: (x.get("vehicleModel", ""), x.get("reg", "")))

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in nabi_vehicles:
        reg = v["reg"]
        typus = v.get("vehicleModel") or "Ismeretlen"
        vonal = v.get("tripShortName") or "—"
        cel = v.get("tripHeadsign") or "Ismeretlen"
        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{reg}**\n"
            f"Típus: {typus}\n"
            f"Vonal: {vonal}\n"
            f"Célállomás: {cel}\n"
            f"Következő megálló: {next_stop}\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚍 Aktív NABI autóbuszok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚍 Aktív NABI autóbuszok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def alfabusz(ctx):
    """Kiírja az összes nem budapesti, nem BKK-s Alfabusz járművet ABC sorrendben típusa és rendszáma szerint."""
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    # Szűrés: csak Alfabusz járművek
    alfabusz_vehicles = [
        {"vehicleId": vid, **data}
        for vid, data in all_vehicles.items()
        if data.get("vehicleModel") and "Alfabusz" in data["vehicleModel"].upper()
    ]

    # Budapest és BKK kizárása
    alfabusz_vehicles = [
        v for v in alfabusz_vehicles
        if not (v.get("vehicleId", "").startswith("BKK:"))
        and "Budapest" not in (v.get("tripHeadsign") or "")
        and "Budapest" not in v.get("nextStop", {}).get("stop", {}).get("name", "")
    ]

    if not alfabusz_vehicles:
        await ctx.send("Nincsenek Alfabusz járművek az API-ban.")
        return

    # Rendszám kivágása
    for v in alfabusz_vehicles:
        vehicle_id = v.get("vehicleId", "")
        if "hkir_" in vehicle_id:
            v["reg"] = vehicle_id.split("hkir_")[-1]
        else:
            v["reg"] = vehicle_id

    # Rendezés: először típusa (ABC), majd rendszám szerint
    alfabusz_vehicles.sort(key=lambda x: (x.get("vehicleModel", ""), x.get("reg", "")))

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in alfabusz_vehicles:
        reg = v["reg"]
        typus = v.get("vehicleModel") or "Ismeretlen"
        vonal = v.get("tripShortName") or "—"
        cel = v.get("tripHeadsign") or "Ismeretlen"
        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{reg}**\n"
            f"Típus: {typus}\n"
            f"Vonal: {vonal}\n"
            f"Célállomás: {cel}\n"
            f"Következő megálló: {next_stop}\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title="🚍 Aktív Alfabusz autóbuszok",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title="🚍 Aktív Alfabusz autóbuszok",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)

# =======================
# PARANCSOK - Egyébbek
# =======================

# @bot.command()
# async def vehhist(ctx, vehicle: str, date: str = None):
#     vehicle = vehicle.upper()  # 🔥 EZ A LÉNYEG

#     day = resolve_date(date)
#     if day is None:
#         return await ctx.send("❌ Hibás dátumformátum. Használd így: `YYYY-MM-DD`")

#     day_str = day.strftime("%Y-%m-%d")
#     veh_file = f"logs/veh/{vehicle}.txt"

#     if not os.path.exists(veh_file):
#         return await ctx.send("❌ Nincs ilyen jármű a naplóban.")

#     entries = []
#     with open(veh_file, "r", encoding="utf-8") as f:
#         for l in f:
#             if not l.startswith(day_str):
#                 continue
#             try:
#                 ts, rest = l.strip().split(" - ", 1)
#                 dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
#                 trip_id = rest.split("ID ")[1].split(" ")[0]
#                 line = rest.split("Vonal ")[1].split(" ")[0]
#                 dest = rest.split(" - ")[-1]
#                 entries.append((dt, line, trip_id, dest))
#             except Exception:
#                 continue

#     if not entries:
#         return await ctx.send(f"❌ {vehicle} nem közlekedett ezen a napon ({day_str}).")

#     entries.sort(key=lambda x: x[0])

#     runs = []
#     current = None

#     for dt, line, trip_id, dest in entries:
#         if not current or trip_id != current["trip_id"] or line != current["line"]:
#             if current:
#                 runs.append(current)
#             current = {
#                 "line": line,
#                 "trip_id": trip_id,
#                 "start": dt,
#                 "end": dt,
#                 "dest": dest
#             }
#         else:
#             current["end"] = dt

#     if current:
#         runs.append(current)

#     lines = [f"🚎 {vehicle} – vehhist ({day_str})"]
#     for r in runs:
#         lines.append(f"{r['start'].strftime('%H:%M')} – {r['line']} / {r['trip_id']} – {r['dest']}")

#     msg = "\n".join(lines)
#     for i in range(0, len(msg), 1900):
#         await ctx.send(msg[i:i + 1900])
        
# @bot.command()
# async def vehicleinfo(ctx, vehicle: str):
#     path = f"logs/veh/{vehicle}.txt"
#     if not os.path.exists(path):
#         return await ctx.send(f"❌ Nincs adat a(z) {vehicle} járműről.")

#     with open(path, "r", encoding="utf-8") as f:
#         lines = [l.strip() for l in f if l.strip()]

#     if not lines:
#         return await ctx.send(f"❌ Nincs adat a(z) {vehicle} járműről.")

#     last = lines[-1]
#     await ctx.send(f"🚊 **{vehicle} utolsó menete**\n```{last}```")
        
@bot.command()
async def vonat(ctx, vonatszam_keres: str):
    """Megkeresi a megadott vonatszámú járművet és kiírja az adatait."""
    all_vehicles = await fetch_mav_vehicles()  # dict {vehicleId: adatok}

    matches = []
    for vid, data in all_vehicles.items():
        trip_short = str(data.get("tripShortName") or "")
        vonatszam = "".join([c for c in trip_short if c.isdigit()])
        if vonatszam == vonatszam_keres:
            matches.append({"vehicleId": vid, **data})

    if not matches:
        await ctx.send(f"Nincs aktív jármű a(z) {vonatszam_keres} vonatszámmal.")
        return

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in matches:
        uic = str(v.get("uicCode") or "Ismeretlen")
        # Pályaszám kiírás
        if len(uic) >= 11:
            payaszam = f"{uic[5:8]} {uic[8:11]}" + (f"-{uic[11]}" if len(uic) > 11 else "")
        else:
            payaszam = uic

        cel = v.get("tripHeadsign") or "Ismeretlen"
        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = round(v.get("speed") or 0.0, 1)
        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{payaszam}**\n"
            f"UIC: {uic}\n"
            f"Célállomás: {cel}\n"
            f"Következő állomás: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title=f"🚆 Vonat {vonatszam_keres} adatai (folytatás)",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        title = f"🚆 Vonat {vonatszam_keres} adatai" if not embeds else f"🚆 Vonat {vonatszam_keres} adatai (folytatás)"
        embed = discord.Embed(
            title=title,
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def all(ctx, vonal: str):
    """Kiírja az adott vonalhoz tartozó járműveket (pl: !all 6454)"""
    all_vehicles = await fetch_mav_vehicles()

    matches = []

    for vid, data in all_vehicles.items():
        trip = data.get("tripShortName")

        # Ne legyen None és tartalmazza a keresett számot
        if trip and vonal in trip:
            matches.append({"vehicleId": vid, **data})

    if not matches:
        await ctx.send(f"Nincs találat erre: {vonal}")
        return

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in matches:
        vehicle_id = v.get("vehicleId", "")

        # Rendszám kivágása (ha van)
        if "hkir_" in vehicle_id:
            reg = vehicle_id.split("hkir_")[-1]
        else:
            reg = vehicle_id

        typus = v.get("vehicleModel") or "Ismeretlen"
        trip = v.get("tripShortName") or "—"
        cel = v.get("tripHeadsign") or "Ismeretlen"
        next_stop = v.get("nextStop", {}).get("stop", {}).get("name", "Ismeretlen")
        speed = v.get("speed", 0.0)

        delay_sec = v.get("nextStop", {}).get("arrivalDelay")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{reg}**\n"
            f"Típus: {typus}\n"
            f"Vonal: {trip}\n"
            f"Célállomás: {cel}\n"
            f"Következő megálló: {next_stop}\n"
            f"Sebesség: {speed} km/h\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embed = discord.Embed(
                title=f"🚌 {vonal} vonal járművei",
                description=description,
                color=0x00A0E3
            )
            embeds.append(embed)
            description = entry
        else:
            description += entry

    if description:
        embed = discord.Embed(
            title=f"🚌 {vonal} vonal járművei",
            description=description,
            color=0x00A0E3
        )
        embeds.append(embed)

    for e in embeds:
        await ctx.send(embed=e)

# =======================
# START
# =======================

@bot.event
async def on_ready():
    print(f"Bejelentkezve mint {bot.user}")

try:
    bot.run(TOKEN)
finally:
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception:
        pass
